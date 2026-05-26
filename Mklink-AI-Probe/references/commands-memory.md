# 内存、VOFA 与 AXF 调试

> 触发词：read-ram、read-reg、vofa、watch、superwatch、hardfault、typeinfo、symbols、memmap
> 返回索引：[SKILL.md](../SKILL.md)

## 内存操作

### 读取 RAM

#### `python -m mklink read-ram --addr <地址> [--size <字节数>] [--port COM6] [--save <文件名>]`
读取目标芯片 RAM 数据，输出十六进制 dump。RAM 读取不需要 FLM 算法。

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
写入数据到 RAM 并自动回读验证。RAM 写入不需要 FLM 算法。

```
python -m mklink write-ram --addr 0x20001000 0xDE 0xAD 0xBE 0xEF
```

### 读取 Flash

#### `python -m mklink read-flash [--addr <地址>] [--size <字节数>] [--port COM6] [--save <文件名>]`
读取 Flash 数据。自动从 `.mklink/` 配置加载 FLM 算法（Flash 读取需要 FLM）。

```
python -m mklink read-flash --addr 0x08000000 --size 128
python -m mklink read-flash --addr 0x08005000 --size 4096 --save flash_dump.bin
```

### VOFA+ 实时变量观测

MKLink 通过 SWD 直接读取目标芯片内存中的变量数据，实时封装为 VOFA+ 协议（JustFloat）经 USB CDC 虚拟串口发送至 PC。**不占用 MCU 串口资源，不侵入业务代码**，可替代 J-Link J-Scope。固件最多一次支持读取 **16 个变量**，最小采样周期 **1us**。

#### 使用方式1：连续读取 float 变量（快速模式）

MKLink 固件支持的 `vofa.send` 命令形式之一，用于读取一段连续内存中的 float 变量。只需指定起始地址和个数，固件将数据以 VOFA+ JustFloat 协议输出。

```
python -m mklink vofa <起始地址> <个数> --period <秒>
```

- `<起始地址>`：第一个 float 变量的内存地址
- `<个数>`：连续读取的 float 数量（1~16）
- `--period`：采样周期（秒），最小 1us（0.000001），设为 0 停止

```
# 从 0x20000030 开始，连续读取 5 个 float，周期 10us
python -m mklink vofa 0x20000030 5 --period 0.00001

# 从 0x20000000 读取 3 个 float
python -m mklink vofa 0x20000000 3 --period 0.001
```

#### 使用方式2：多地址、多类型读取（精确模式）

MKLink 固件支持的 `vofa.send` 命令形式之二，用于读取不同地址、不同类型的变量。每个变量指定地址和类型，固件将数据以 VOFA+ JustFloat 协议输出。

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
- 精确模式 `vofa <addr> <type> ...` 会在 Watch 表显示规范 C 类型和字节数。
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
使用 `arm-none-eabi-readelf --debug-dump=info` 解析 DWARF 类型信息，不引入额外 Python 依赖。

```
python -m mklink typeinfo --source path/to/firmware.axf --var g_appState
python -m mklink typeinfo --source path/to/firmware.axf --struct AppConfig
python -m mklink typeinfo --source path/to/firmware.axf --enum AppMode
```

#### `python -m mklink symbols --source <firmware.axf> [--filter <正则>]`
从 ELF/AXF 列出 RAM 全局变量（需 `arm-none-eabi-readelf`）。`--filter` 为正则，用于缩小符号列表。

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
基于 MKLink `read_ram` 响应中的设备时间戳连续采样，适合同时观察 RAM 变量、`struct.field` 路径和寄存器。变量解析依赖 AXF/DWARF；寄存器可使用内置寄存器表，或通过 `--svd`/Keil Pack 自动发现 CMSIS-SVD 后支持外设寄存器名。未加 `--visualize` 时输出采样 JSON；加 `--visualize` 时启动 Web 看板，可搜索/添加 AXF 符号或寄存器。

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

**Dump Memory 高速模式 (`--dump-mem`)**

使用 Dump Memory 二进制流协议替代逐个 `read_ram` 轮询。设备端一条命令配置所有区域后主动推送二进制帧（64 位时间戳 + CRC32 校验），延迟更低、吞吐更高。

```bash
python -m mklink superwatch g_counter,g_sensor --source path/to/firmware.axf --dump-mem --visualize --period 0.01
```

- 需要固件支持 `dump_mem.start()` / `dump_mem.stop()` 命令
- 不支持时自动回退到 read_ram 轮询模式，无需用户干预

#### `python -m mklink hardfault [--source <firmware.axf>] [--sp <异常栈帧地址>]`
读取 SCB Fault 寄存器并解码 CFSR/HFSR。提供 `--sp` 时再读取 32 字节异常栈帧，并用 `arm-none-eabi-addr2line` 映射 PC/LR。

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
