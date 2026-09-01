# 内存、VOFA 与 AXF 调试

> 触发词：read-ram、read-reg、dump-memory、flush-memory、vofa、watch、superwatch、hardfault、typeinfo、symbols、memmap
> 返回索引：[SKILL.md](../SKILL.md)

## 内存操作

### 读取 RAM

#### `python -m mklink read-ram --addr <地址> [--size <字节数>] [--port COM6] [--save <文件名>]`
读取目标芯片 RAM 数据，输出十六进制 dump。RAM 读取不需要 FLM 算法。
`--size` 限制为 **1..4096 字节**；更大范围改用 `dump-memory`，不要把大段文本
hexdump 返回给 AI。MCP 同样限制 `read_memory` 为 4096B；同时读取多个变量时用
`read_memory_regions`（最多 16 项、总计 4096B），连续或重叠区域会自动合并为一次读取。

```
python -m mklink read-ram --addr 0x20000000 --size 256
python -m mklink read-ram --addr 0x20000000 --size 128 --save ram.bin
```

`--save` 将数据保存到 MKLink U 盘文件，重启下载器后可见。

### 读取内存映射寄存器

#### `python -m mklink read-reg <寄存器名> [--width 32] [--count 1] [--format both] [--port COM6]`
读取外设、SCB、NVIC、CoreDebug 等内存映射寄存器，底层仍是 `cmd.read_ram(<addr>, <size>)`。

```
python -m mklink read-reg SCB.CFSR
python -m mklink read-reg SCB.HFSR --format hex
python -m mklink read-reg --addr 0xE000ED28 --width 32
```

注意：`read-reg` 读取的是内存映射寄存器地址；R0/R1/MSP/PSP/LR/PC 这类 CPU 核心寄存器不是普通内存地址，不能直接用 `cmd.read_ram` 当作地址读取。HardFault 自动栈帧解析需要用户提供异常栈帧地址 `--sp`。

### 写入 RAM

#### `python -m mklink write-ram --addr <地址> <字节1> <字节2> ... [--port COM6]`
写入数据到 RAM 并自动回读验证，单次最多 4096B。CLI 使用 `Device.write_memory` 的
`flush_memory` 分块/重复字节折叠路径，不再直接依赖旧 `cmd.write_ram` 的命令回显。
RAM 写入不需要 FLM 算法。

```
python -m mklink write-ram --addr 0x20001000 0xDE 0xAD 0xBE 0xEF
```

**多地址写入**:PikaScript 不支持 list/tuple 展开为多地址参数,`cmd.write_ram([a,b],[v1,v2])` 会被解释为单 arg,数据写到临时区。**正确方式:多次调用**,每次 47ms 开销:

```python
# ❌ 错误 — list/tuple 语法被忽略,写到 0x0009c5xx 临时区
cmd.write_ram([0x20001080, 0x20001090], [0x11, 0x22])

# ✅ 正确 — 两次连续调用
cmd.write_ram(0x20001080, 0x11)
cmd.write_ram(0x20001090, 0x22)
# 两次共 ~95ms,无串行开销
```

直接调用旧固件 `cmd.write_ram` 时仍受 Pika 位置参数上限约束；CLI 已不走该入口。
V4.3.8 在 STM32H743 上实测旧入口、bytes 字面量、重复字节折叠及 Device 路径均可完成
`11 22 33 44 -> 00 00 00 00` 并回读。为防旧固件或偶发静默失败，自动化写入必须以回读
一致作为成功条件，不能只看 `>>>` 或命令回显。

**避免覆盖活跃内存区**:写入前先核对 `build/keil/List/rt-thread.map` 与 AXF 符号:
- `0x20000000..0x200000DC`: `.mklink_res` (test fixture 控制块, magic 0x4D4B5245 "ERKM")
- `0x20000200..0x20001374`: `.data`
- `0x20001374..0x20001774`: `s_tf_data_buf` (test fixture 数据缓冲,**20ms 周期被覆写**)
- `0x200017F8..0x200018F8`: `rt_thread_stack` (256B, RT-Thread 主线程栈,**绝不可覆盖**)
- `0x20001908+`: `_heap` (RT-Thread 堆起点, **运行时向上增长**, .bss 之后未必真空闲)
- `0x20001774..0x2000FAB0`: `.bss` (含 dbcortex 参数库 1600B 等活跃变量)
- `0x2001F800..0x20020000`: 主线程栈 (2KB)

**安全测试区**:`0x20001000..0x20001374`(.data 之前的空闲区, 896 字节) 是稳定的。

`.bss` 之后的 `0x2000FA10..0x2001F800` (~65KB) **理论上是空闲的**,但**实际不可信**——RT-Thread 堆可向上分配到此处。务必先用 `python -m mklink symbols --source <axf> --filter "heap"` 看运行时堆的实际位置;或在写入前用 `vofa` / `superwatch` 读一段确认未被业务覆写。**血泪教训:不查就直接写,会撞 heap 元数据导致 HardFault → 整机重启**。

#### `python -m mklink dump-memory <region> [<region> ...] [--period SEC] [--frames N] [--duration SEC] [--save FILE] [--json]`
公共高速内存 dump，直接调用固件 `cmd.dump_memory(addr1, size1, ..., period)` 并解析 `MPMDMPMD` 二进制帧。别名：`python -m mklink dump ...`。

`<region>` 格式：`ADDR:SIZE`，可重复传入多个区域。默认 `--period 0 --frames 1 --duration 2`，即只采集 1 个完整样本，避免命令意外长期占用串口流模式。

```
# 单次读取 16 字节 RAM
python -m mklink dump-memory 0x20000000:16

# 同时读取两个区域，逐帧 JSON 输出
python -m mklink dump-memory 0x20000000:16 0x20001000:4 --json

# 连续采样 10 个样本，周期 10ms
python -m mklink dump-memory 0x20000000:16 --period 0.01 --frames 10

# 按时长采集并保存 region payload 到本地文件
python -m mklink dump-memory 0x08000000:256 --frames 0 --duration 1 --save flash_payload.bin
```

- `--save` 保存的是已解析出的 region payload，不是包含 magic/CRC 的原始协议帧。
- **最多 15 个 region**。固件数据结构容量虽为 16，但 16 组 `(addr,size)` 加 period
  正好占满 Pika 的 33 参数上限；V4.3.8 实测会使 REPL 失去响应。不得用 SDK 或串口
  绕过。16 个常见连续变量应合并为一个区域；离散快照用 MCP `read_memory_regions`。
- CLI 还限制 `--duration <= 300`、`--frames <= 100000`；AI 发起的单次采集进一步
  限制为 **30 秒**，需要更长观察时分段并在每段后确认设备状态。
- `total_size <= 2048` 走 OLD 帧；`total_size > 2048` 走 B1 分块帧，CLI 会等到 B1 最后一块后计为 1 个完整样本。
- 单次 `cmd.dump_memory()` 总长度默认 **512 KiB**（固件 V4.3.3 实测整片 Flash 稳定，256 个 B1 块全 `flags=0x0000`）。**老固件**（pre-V4.3.3，BUG-5：>64 KiB 末块截尾 512B）请传 ≤32 KiB 的 `ADDR:SIZE` region 规避。
- 如果没有解析到任何帧，CLI 会打印设备返回的可见文本，常见原因是固件未暴露 `cmd.dump_memory` 或设备仍处于异常流模式。

#### `python -m mklink flush-memory <item> [<item> ...] [--verify] [--repeat N] [--interval-ms MS]`
静默写 RAM,支持多地址多字节(内部调用 PikaScript `cmd.flush_memory()`)。与 `write-ram` 的关键区别:

- **成功无 ACK** — 设备只回显命令 + `>>>`,不输出 hexdump 预览。
- **不得与流并发** — 虽然成功时没有 hexdump，仍必须先停止 dump/VOFA/RTT/SystemView、
  释放连接，再在普通命令会话中写入。
- **多字节 / 多地址** — 单次 PikaScript 调用提交多块写入,远比 `write-ram` 的循环高效。
- **批写支持** — `--repeat N` + `--interval-ms MS` 实现周期写入(压力测试、抖动测试用)。

`<item>` 格式:`ADDR:BYTE,BYTE,...` 或 `ADDR:0xBYTE 0xBYTE ...`(逗号/空格皆可,带不带 0x 前缀皆可)。

##### PikaScript 函数同时支持两种调用协议(实测 2026-06)

新固件的 `cmd.flush_memory` 同时支持两条路径,CLI 会根据 item 数自动选用:

| CLI 形态 | 调用的 PikaScript | 稳定性 | 推荐场景 |
|----------|-------------------|--------|----------|
| 单 item (1 项) | `cmd.flush_memory(0x20010200, 0xA5, 0x5A, 0xDE, 0xAD)` <br> **旧协议**(位置参数) | **100% PASS** 实测 1~16 字节全通过 | **单地址多字节**(默认推荐) |
| 多 item (≥2 项) | `cmd.flush_memory([(0x20010200, bytes([0x11])), (0x20010400, bytes([0x22]))])` <br> **新协议**(list-of-tuples) | 多数 PASS;某些活跃地址会被固件周期覆写 | 多地址写入(唯一选择) |

```
# 单地址单字节(1 项,走旧协议)
python -m mklink flush-memory 0x20010000:0x55

# 单地址多字节(1 项,走旧协议,1~16 字节稳定)
python -m mklink flush-memory 0x20010000:0xDE,0xAD,0xBE,0xEF
python -m mklink flush-memory 0x20010000:0xCA,0xFE,0xBA,0xBE,0x12,0x34,0x56,0x78,0x9A,0xBC,0xDE,0xF0,0x0F,0xED,0xCB,0xA9

# 多地址多字节(≥2 项,走新协议)
python -m mklink flush-memory \
    0x20010000:0x11,0x22,0x33 \
    0x20010100:0x44,0x55,0x66,0x77 \
    0x20010200:0x88

# 回读验证(强烈建议对多地址 / ≥2 字节使用)
python -m mklink flush-memory 0x20010000:0xDE,0xAD,0xBE,0xEF --verify

# 周期写 10 次,每次间隔 50ms
python -m mklink flush-memory 0x20010000:0xA5 --repeat 10 --interval-ms 50
```

##### 已知固件 bug:首次调用 / 偶发裸 `flush fail`
实测 PikaScript `cmd.flush_memory` 在 **同一连接的首次调用** 或某些时机会返回裸 `flush fail` (无 `:` 原因),但 **写入实际上已生效**(可通过 `read-ram` 验证)。**第二次起恢复正常静默成功**。CLI 已识别为 `WARN` 而非 `FAIL`,请用 `read-ram` / `cmd.read_ram(...)` 单独验证。

##### 已知固件 bug:多地址的活跃地址会被周期覆写
固件某些数据(`.bss`/GUI 回调表/heap 元数据/任务栈)会被后台线程周期性刷新。新协议多地址写入在 `0x20010A00` 等活跃地址上的写入**会立即被覆写**——这是固件问题,不是 flush_memory 的 bug。规避:
1. 写入前用 `python -m mklink symbols --source <axf> --filter "heap|stack|GUI"` 排除活跃地址
2. 写入活跃地址后,**不要回读**——读到的是覆写后的状态,不是你的写入
3. 一次性写入 1 个稳定地址,然后立即用 PikaScript 表达式 `print(0x20010000)` 等读(若可用)做"原位验证"

##### 响应解析规则
| 响应 | CLI 判定 | 含义 |
|------|---------|------|
| 空(只回显命令) | OK | 静默成功 |
| `TypeError/NameError/...` | FAIL | 真实异常(参数类型/拼写错) |
| `flush fail: <原因>` | FAIL | 显式原因(如 `data must be bytes or int list` / `item must be (addr, data)`) |
| 裸 `flush fail` | OK + WARN | 已知固件 bug,首次/偶发,写入实际生效 |
| 其他非空 | OK + WARN | 非预期响应,保留原始文本供排查 |

##### 旧拼写向后兼容
旧名 `flush-memroy`(带拼写错误)作为 argparse alias 仍可工作,但会打印一行 deprecation WARN 提醒改用 `flush-memory`。**注意:PikaScript 端的旧函数名 `cmd.flush_memroy` 已被新代码移除**(`NameError`);任何直接调用旧函数名的脚本/工具都需要更新到 `cmd.flush_memory`。

```
# 仍可用(自动转发 + WARN)
python -m mklink flush-memroy 0x20010000:0x55
# → [WARN] 'flush-memroy' 是旧拼写,已自动转发到 'flush-memory',请改用新名
```

##### 验证技巧
PikaScript 端的 `cmd.read_ram` 显示屏对部分地址会**不显示数据行**(返回 `wRamAddr`/`wCount` 但缺第二行)——这是 read 显示 bug,与 flush_memory 无关。**解决方法**:
- 用 `--verify`(CLI 自动 read_ram 每个地址),CLI 会同时打印 hex,数据行缺失也能在调用现场看出
- 用 `cmd.write_ram(addr, byte)` 看它的"AFTER 回显"行(只对单字节有效)
- 用 `python -m mklink vofa <addr> uint8_t --period 0.01`(连续采样)看实际值

##### 📌 边界与分块约束（三类边界务必区分）

`flush-memory` 单批最多 **12 KiB / 8 个地址项**。更大输入必须串行分批并等待提示符。
固件极限、命令串和 Windows 命令行限制见[静默写边界](flush-memory.md)；推荐边界不得
用直接 SDK/Pika 调用绕过。

- 非重复数据 CLI **不自动分块**，超 230B 直接 `FAIL`。
- **重复字节**用紧凑语法 `ADDR:BYTE*N`（如 `flush-memory "0x20008000:0xAA*12288"`），CLI 自动转 `bytes([0xVV])*N` 短表达式，绕开 ②③；AI/MCP 单次最多写 12 KiB。
- **PowerShell**：始终用单引号包裹整个 item（`'0x...:0xAA*N'`），否则逗号会被预处理改写参数。
- 完整边界表与 host 端分块策略见 **[flush-memory.md](flush-memory.md)**。

### 读取 Flash

#### `python -m mklink read-flash [--addr <地址>] [--size <字节数>] [--port COM6] [--save <文件名>]`
读取 Flash 数据。自动从 `.mklink/` 配置加载 FLM 算法（Flash 读取需要 FLM）。

```
python -m mklink read-flash --addr 0x08000000 --size 128
python -m mklink read-flash --addr 0x08005000 --size 4096 --save flash_dump.bin
```

### VOFA+ 实时变量观测

MKLink 通过 SWD 直接读取目标芯片内存中的变量数据，实时封装为 VOFA+ 协议（JustFloat）经 USB CDC 虚拟串口发送至 PC。**不占用 MCU 串口资源，不侵入业务代码**，可替代 J-Link J-Scope。快速连续布局最多读取 **16 路 float**；精确离散布局最多读取 **15 个地址/类型对**。最小采样周期为 **1us**。

#### 使用方式1：连续读取 float 变量（快速模式）

MKLink 固件支持的 `vofa.send` 命令形式之一，用于读取一段连续内存中的 float 变量。只需指定起始地址和个数，固件将数据以 VOFA+ JustFloat 协议输出。

```
python -m mklink vofa <起始地址> <个数> --period <秒>
```

- `<起始地址>`：第一个 float 变量的内存地址
- `<个数>`：连续读取的 float 数量（1~16）
- `--period`：采样周期（秒），最小 1us（0.000001），设为 0 停止

#### dump_memory 流停止与 AI 恢复边界

当前 V4.3.8 中 `period>0` 持续采样，`period=0` 输出一个完整样本后回到 idle，
`period=-1` 用于显式停止。主机停止后至少保留默认 **50 ms** 排空时间；V4 实测把
等待缩到 10 ms 会残留二进制流并污染后续命令。不要快速 start/stop，也不要在同一
下载器上并行发命令。

结束 dump/VOFA/RTT/SystemView 后关闭当前连接；普通 `read_ram` 等命令重新连接后再发。
若 MCP tool 超时，只调用一次 `device_status`，随后结束旧会话并只执行一次
`disconnect` → `connect`。任一步失败就停止并请用户拔插 USB；不得自动重试超时原
调用，也不得循环发送 stop、`reboot_probe` 或 `reboot()`。

```
# 从 0x20000030 开始，连续读取 5 个 float，周期 10us
python -m mklink vofa 0x20000030 5 --period 0.00001

# 从 0x20000000 读取 3 个 float
python -m mklink vofa 0x20000000 3 --period 0.001
```

#### 使用方式2：多地址、多类型读取（精确模式）

MKLink 固件支持的 `vofa.send` 命令形式之二，用于读取不同地址、不同类型的变量。每个变量指定地址和类型，固件将数据以 VOFA+ JustFloat 协议输出。

精确模式最多 **15 个** `(地址, 类型)` 对。16 对再加采样周期会形成 33 个
Pika 位置参数，触及已知会使 REPL 失去响应的边界。主机还会按 UTF-8 字节数校验
完整 `vofa.send(...)` 命令，安全上限为 **511B**；超限请求会在发现端口前拒绝。
快速模式只使用 `起始地址、通道数、周期` 3 个参数，因此保留独立的 **1~16 路**
连续 float 上限，不能把这个通道上限套用到精确模式。

```
python -m mklink vofa <地址1> <类型1> [<地址2> <类型2> ...] --period <秒>
```

```
# 观测 2 个不同地址的变量（混合类型）
python -m mklink vofa 0x20000030 uint8_t 0x2000154c float --period 0.001

# 观测 3 个变量
python -m mklink vofa 0x20000030 uint8_t 0x2000154c uint16_t 0x20001550 float --period 0.00001

# 观测 4 个变量
python -m mklink vofa 0x20000030 int32_t 0x20000034 float 0x20000038 uint16_t 0x2000003c int8_t --period 0.0001
```

**MKLink 固件接受的变量类型字符串：**

| 关键字 | C 类型 | 字节数 | 说明 |
|--------|--------|--------|------|
| `int8_t` / `int8` / `char` | int8_t | 1 | 有符号 8 位 |
| `uint8_t` / `uint8` / `uchar` | uint8_t | 1 | 无符号 8 位 |
| `int16_t` / `int16` / `short` | int16_t | 2 | 有符号 16 位 |
| `uint16_t` / `uint16` / `ushort` | uint16_t | 2 | 无符号 16 位 |
| `int32_t` / `int32` / `int` | int32_t | 4 | 有符号 32 位 |
| `uint32_t` / `uint32` / `uint` | uint32_t | 4 | 无符号 32 位 |
| `float` / `fp32` | float | 4 | 单精度浮点 |
| `bool` / `boolean` | bool | 1 | 布尔类型 |

> 以下类型由 MKLink 固件解析，CLI 将类型字符串原样传递给 `vofa.send()` 命令。

> **对齐警告（MKLink SWD 读取限制）：非 4 字节变量（int8_t、uint8_t、int16_t、uint16_t、bool）必须强制 4 字节对齐，否则 MKLink 固件通过 SWD 32 位读取时会出现数据撕裂。** 在 C 代码中声明变量时使用：
> ```c
> __attribute__((aligned(4))) static volatile uint16_t my_var = 0;
> ```

#### 停止观测

```
python -m mklink vofa --stop
```

#### VOFA+ Web 可视化（--visualize）

启动 Web 仪表盘，在浏览器中实时显示 VOFA+ JustFloat 数据的趋势图表，无需 VOFA+ 桌面软件。

```
python -m mklink vofa <变量参数> --visualize [选项]
```

自动完成：发现端口 → 连接 → 启动 VOFA 采样 → 解析 JustFloat 二进制帧 → 启动 Web 服务器 → 打开浏览器 → 实时绘图

**使用示例：**

```bash
# 快速模式可视化（3 个连续 float）
python -m mklink vofa 0x20000030 3 --period 0.01 --visualize

# 精确模式可视化（混合类型，自动用地址作通道名）
python -m mklink vofa 0x20000030 uint16_t 0x20000034 float --period 0.01 --visualize

# 自定义通道名（推荐，直观识别每条曲线）
python -m mklink vofa 0x20000030 uint16_t 0x20000034 float --period 0.01 --visualize --names raw_adc,filtered,speed

# 固定端口，不打开浏览器（用于远程查看）
python -m mklink vofa 0x20000030 3 --visualize --port-http 8888 --no-browser

# 限时运行 60 秒
python -m mklink vofa 0x20000030 3 --period 0.01 --visualize --duration 60

# 使用 AXF 符号名 / struct.field（需要 --source）
python -m mklink vofa g_appState uint8_t --source path/to/firmware.axf --visualize
python -m mklink vofa g_config.setpoint float --source path/to/firmware.axf --visualize
```

**可视化选项：**

| 选项 | 说明 |
|------|------|
| `--host 127.0.0.1` | HTTP 服务器绑定地址（默认 127.0.0.1） |
| `--port-http 0` | HTTP 端口（默认 0 = 随机可用端口） |
| `--no-browser` | 不自动打开浏览器 |
| `--max-points 500` | 浏览器最大数据点数（默认 500） |
| `--duration 30` | 运行时长（秒，默认 30） |
| `--names a,b,c` | 通道名称，逗号分隔（如 `ch0,ch1,ch2`） |

**通道命名规则：**
- 使用 `--names`：按指定名称显示（推荐，直观识别每条曲线）
- 快速模式无 `--names`：自动用地址偏移命名，如 `0x20000030`, `0x20000034`, `0x20000038`
- 精确模式无 `--names`：自动用变量地址命名，如 `0x20000030`, `0x20000034`

**VOFA 类型显示：**
- 快速模式 `vofa <addr> <count>` 默认每个通道是 `float`，`Size` 为 `4B`。
- 精确模式 `vofa <addr> <type> ...` 最多 15 路，会在 Watch 表显示规范 C 类型和字节数。
- Watch 表中的 `Type` 是变量 C 类型；`Size` 是该类型字节数；`Unit` 是物理单位（如 `V`、`rpm`、`degC`），没有单位时显示 `-`。
- 支持的类型别名见上文「MKLink 固件接受的变量类型字符串」表格。

**浏览器界面说明：**
- 标题栏显示 **MKLink VOFA Viewer**，RTT 模式显示 **MKLink RTT View**
- 左上角 **VOFA** / **RTT** 模式徽章，区分当前数据来源
- 实时折线图，每条曲线独立颜色，点击通道名切换显示/隐藏
- 统计面板：当前值、最小值、最大值、平均值
- 按 `Space` 暂停/恢复，按 `L` 显示/隐藏原始日志

**VOFA 仪表盘 HTML 加载优先级（与 RTT 共用模板）：**

1. `.mklink/vofa_viewer.html` — **完全自定义 HTML**（需自行通过 SSE `/stream` 端点获取数据）
2. `.mklink/vofa_viewer_template.html` — **用户模板**（保留 `__MAX_POINTS__`、`__TITLE__`、`__MODE__` 占位符，服务器自动注入，其余可自由修改）
3. 内置模板 `_rtt_viewer_template.html`（默认，与 RTT 共用）

> **注意**：VOFA 可视化复用 RTT 的 `VisualizationServer`，前端数据格式一致。如需自定义样式，拷贝内置模板到 `.mklink/` 下修改即可。

### AXF/DWARF 调试增强

#### `python -m mklink typeinfo --source <firmware.axf> [--var 名称 | --struct 名称 | --enum 名称 | --list-structs | --list-enums]`
默认使用随 MKLink 内置的 `pyelftools` 解析 DWARF 类型信息，不依赖用户工具链。

```
python -m mklink typeinfo --source path/to/firmware.axf --var g_appState
python -m mklink typeinfo --source path/to/firmware.axf --struct AppConfig
python -m mklink typeinfo --source path/to/firmware.axf --enum AppMode
```

#### `python -m mklink symbols --source <firmware.axf> [--filter <正则>]`
从 ELF/AXF 列出 RAM 全局变量。`--filter` 为正则，用于缩小符号列表。只有用户明确传 `--elf-backend external` 时才调用本机 `readelf`。

```
python -m mklink symbols --source path/to/firmware.axf
python -m mklink symbols --source path/to/firmware.axf --filter "counter|sensor"
```

#### `python -m mklink watch <变量1,变量2> --source <firmware.axf> [--period 秒]`
一次性读取变量快照，支持基础类型和 `struct.field`。周期模式用 Ctrl+C 停止。

```
python -m mklink watch g_counter,g_sensor --source path/to/firmware.axf
python -m mklink watch g_config.setpoint --source path/to/firmware.axf --period 1
```

#### `python -m mklink superwatch <变量/字段/寄存器...> [--source <firmware.axf>] [--svd <device.svd>] [--visualize]`
基于 MKLink `cmd.dump_memory` 二进制帧内的设备时间戳连续采样，适合同时观察 RAM 变量、`struct.field` 路径和寄存器。变量解析依赖 AXF/DWARF；寄存器可使用内置寄存器表，或通过 `--svd`/Keil Pack 自动发现 CMSIS-SVD 后支持外设寄存器名。未加 `--visualize` 时输出采样 JSON；加 `--visualize` 时启动 Web 看板，可搜索/添加 AXF 符号或寄存器。

`read_ram`/`read_memory` 只用于单次 RAM 快照、变量详情和 AI 故障分析，不用于 SuperWatch 曲线采样，也不作为固件或连接不支持二进制流时的后备方案。此时 SuperWatch 会明确报错并停止。

常用参数：
- `--period 0.1`：采样周期，单位秒
- `--duration 30`：运行时长，`0` 表示持续运行到手动停止
- `--port COM6`：指定 MKLink 串口；省略时自动检测
- `--host 127.0.0.1 --port-http 0`：Web 看板监听地址和端口，`0` 表示随机端口
- `--no-browser`：启动 Web 服务但不自动打开浏览器
- `--max-points 500`：图表保留的最大点数

```bash
python -m mklink superwatch g_counter,g_sensor --source path/to/firmware.axf --period 0.1 --duration 30
python -m mklink superwatch g_config.setpoint,SCB.CFSR --source path/to/firmware.axf --visualize --period 0.1
python -m mklink superwatch TIM2.CNT,ADC1.DR --svd path/to/device.svd --visualize --duration 0
```

**Dump Memory 连续采样协议**

SuperWatch 固定使用官方 `cmd.dump_memory(addr1, size1, addr2, size2, ..., period)` 二进制流协议。设备端一条命令配置所有区域后主动推送 `MPMDMPMD` 帧（64 位时间戳 + frame CRC32 校验）。同一协议也可通过公共 CLI `python -m mklink dump-memory ...` 直接使用。旧命令中的 `--dump-mem` 参数继续接受，但不再切换行为。

```bash
python -m mklink superwatch g_counter,g_sensor --source path/to/firmware.axf --visualize --period 0.01
```

- `total_size <= 2048`: OLD 普通帧。
- `total_size > 2048`: B1 分块帧，每块最大 2048B，包含 `block_index` / `block_count` / `block_crc32`。
- SuperWatch 只合并相接或重叠的地址，不跨地址空洞多读；V4.3.8 真机测量显示跨 16B 空洞已降低采样率。
- SuperWatch 最多提交 **15 个离散 region**。固件帧虽可容纳 16 个 region，但 V4.3.8 的 Pika 文本入口在 16 组地址/长度加 period 时会超过安全参数边界。
- `build_dump_mem_command()` 默认允许单次最多 **512 KiB**（V4.3.3 实测整片 Flash 稳定）；老固件请传 ≤32 KiB region；更大范围仍应由 host 分块。
- V4.3.1 官方 API 直测（2026-06-07）：`0x08000000/256`、`0x20010200/32`、`0x08020000/2049` 均 PASS，flags=`0x0000`，B1 为 2048B + 1B 两块。
- 若 flags=`0x0004`，含义是 `Region error`，优先排查目标供电、Vref、SWD、NRST、MCU 运行/低功耗/复位状态；这不是 host parser CRC 失败。

#### `python -m mklink hardfault [--source <firmware.axf>] [--sp <异常栈帧地址>]`
读取 SCB Fault 寄存器并解码 CFSR/HFSR。提供 `--sp` 时再读取 32 字节异常栈帧，并默认通过内置 DWARF line program 映射 PC/LR；显式 external 模式才调用本机 `addr2line`。

```
python -m mklink hardfault --source path/to/firmware.axf --sp 0x20001FF0
```

#### `python -m mklink memmap --source <firmware.axf> [--json]`
解析 AXF section header，输出 Flash/RAM 占用。

```
python -m mklink memmap --source path/to/firmware.axf
python -m mklink memmap --source path/to/firmware.axf --json
```

**JustFloat 二进制解析特性：**
- 自动解析 VOFA+ JustFloat 协议帧（小端 IEEE 754 float + 帧尾 `0x00 0x00 0x80 0x7f`）
- 基于通道数的帧长度校验，防止数据损坏或中途捕获导致的解析错误
- 正确处理通道值为 +Inf（`0x7f800000`）的情况，不与帧尾混淆
- 自动重同步：遇到损坏帧时丢弃并继续解析后续有效帧
- 支持帧尾跨 read 分割、垃圾数据后正常帧恢复等边界场景

```
python -m mklink vofa --stop
```

#### 变量地址查找

变量地址可通过查看 MDK 编译生成的 `.map` 文件或使用 `rtt-find` 命令获取：

```bash
python -m mklink rtt-find "path/to/build/Project.map"
```

---
