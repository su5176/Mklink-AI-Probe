---
name: flush-memory-boundary
description: |
  flush-memory / cmd.flush_memory 实测使用边界、推荐分块策略、校验建议。
  触发：flush-memory 边界、12 KiB 分块、单地址字节上限、varargs 字节上限、CDC 异常、flush fail。
---

# flush-memory 边界约束

> 适用命令：`python -m mklink flush-memory`
> 适用对象：MKLink 固件 PikaPython REPL 中的 `cmd.flush_memory` API
> 返回索引：[SKILL.md](../SKILL.md) · [commands-memory.md](commands-memory.md)

## 1. 接口定位

`cmd.flush_memory` 是 MKLink 固件中的 PikaPython REPL API，**不是** `python -m mklink` 的 CLI 子命令。
它通过 MKLink 设备的 Python shell/CDC 串口发送命令，向目标 RAM 写入数据。

`python -m mklink flush-memory` CLI 是这个 REPL API 的封装者（`mklink/cli.py:_cli_flush_memory`）。

## 2. 基本用法

### 2.1 老 varargs 形式

```python
cmd.flush_memory(0x20002000, 0x11, 0x22, 0x33, 0x44)
```

适合少量字节写入（≤20 字节稳定）。

### 2.2 单地址 bytes/list 写入

```python
cmd.flush_memory((0x20002000, bytes([0x11, 0x22, 0x33, 0x44])))
cmd.flush_memory((0x20002000, [0x11, 0x22, 0x33, 0x44]))
```

适合中大块连续数据写入；AI/MCP 每项和单次总量都不得超过 12 KiB。

### 2.3 单地址 batch 形式

```python
cmd.flush_memory([
    (0x20002000, bytes([0x11, 0x22, 0x33, 0x44]))
])
```

边界与单地址 tuple 形式一致。

### 2.4 多地址多数据写入

```python
cmd.flush_memory([
    (0x20001080, bytes([0x11, 0x22, 0x33])),
    (0x20002000, bytes([0x44, 0x55, 0x66, 0x77])),
    (0x20003000, bytes([0x88])),
])
```

适合一次写入多个离散 RAM 地址（≤8 个地址项推荐）。

### 2.5 重复数据或 pattern 数据

```python
# 重复单字节
cmd.flush_memory([
    (0x20002000, bytes([0x5A]) * 1024)
])

# 16 字节 pattern 循环
cmd.flush_memory([
    (
        0x20002000,
        bytes([
            0x01, 0x05, 0x00, 0x01,
            0x00, 0x01, 0x5D, 0xCA,
            0x10, 0x20, 0x30, 0x40,
            0x55, 0xAA, 0x7E, 0x81,
        ]) * 64
    )
])
```

### 2.6 CLI 紧凑语法 `ADDR:BYTE*N`（绕开 Windows 命令行长度限制）

直接在 shell 上写 `flush-memory 0x20008000:0xAA,0xAA,...` 逐字节展开，长字面量会先撞 **④ Windows cmdline 限制**。CLI 提供紧凑写法，内部自动转 `bytes([0xVV])*N` 短表达式，命令串极短：

```powershell
# 单地址重复字节（清零 / 填 0xFF / 大块填充）
python -m mklink flush-memory "0x20008000:0xAA*12288" --verify   # 12 KiB，AI/MCP 单次硬上限
python -m mklink flush-memory "0x20008000:0xFF*8192" --verify    # 8 KiB 填充
python -m mklink flush-memory "0x20008000:0x00*4096"             # 4 KiB 清零

# byte 接受 0xAA / AA，count 为十进制；可与其他 item 一起多地址提交
python -m mklink flush-memory "0x20008000:0xAA*1024" "0x20009000:0x11,0x22"
```

> ⚠️ **`*N` 只解决输入长度，不抬高主机安全上限。** 本地 MCP 对每个 item 和单次调用总量都硬限制为 **12288B**，并在设备发现或 I/O 前拒绝超限请求。早期接近 16 KiB 的固件极限实验只用于风险定位，曾触发超时和 CDC 会话扰动，不能作为 AI 或自动化请求值。更大区域必须按 §5 拆成多个调用，并等待上一调用完成。

规则：`*N` 形式必须是单一 `BYTE*COUNT` token，不能与逐字节列表混排（如 `0x11 0xAA*5` 会被拒绝）。多字节 pattern 重复（`PATTERN*N`）暂不支持，留作后续。

## 3. 实测使用边界（三类边界务必区分）


flush_memory 同时受**三类独立边界**约束，排查时务必先分清撞的是哪一类：

| 边界类型 | 含义 | 实测值（V4.3.3） |
|---|---|---|
| **① AI/MCP 主机硬限制** | 自动化调用在任何设备发现或 I/O 前执行的安全门 | 每项 ≤ **12288B**；单次总量 ≤ **12288B**；≤ **8 项** |
| **② 固件协议边界** | `cmd.flush_memory` PikaScript API 的实验室特性 | 接近 16 KiB 存在超时和 CDC 扰动悬崖；只作风险记录，不是可用请求上限 |
| **③ PC CLI 命令阈值** | CLI 为防 PIKA_LINE_BUFF 溢出（REPL 死锁）设置 | 单条命令串 ≤ **230 字符**；多地址 ≤ **8 项/批** |
| **④ Windows 命令行长度限制** | 逐字节在 shell 上展开的字面量长度上限 | 长字面量可能先被 Windows 拒绝；使用紧凑语法仍不得超过 12 KiB 安全值 |

### 3.1 固件实验极限不是主机可用值

历史固件压线实验曾验证比 12 KiB 更大的重复字节写入，也发现更高位置会导致 10 秒超时和 CDC 端口短暂消失。这个结果用于解释故障，不用于生成命令。AI、MCP 和无人值守自动化必须只采用 **每项及每次总量 ≤12288B**；紧凑语法只能缩短文本，不能绕过这条规则。

### 3.2 多地址离散：≤8 项稳定

| 地址项数量 | 每项 1B | 说明 |
|---:|---|---|
| ≤6 | PASS | 当前 CLI 230B 阈值下用户可直接用的稳定项数 |
| 8 | PASS | 临时放宽主机阈值后仍稳定（固件边界） |
| 12 | FAIL | 发到设备后 10s 超时，且会扰动会话（后续 version 空响应） |

> **结论**：推荐 **≤8 项**；12 项不稳定，不要当作可用能力。

### 3.3 接口形态与边界汇总

| 接口形式 | 推荐 | 受限因素 |
|---|---:|---|
| `cmd.flush_memory(addr, b0, b1, ...)` 老 varargs | `≤ 20 bytes` | ① 固件参数个数上限（`addr + 20B = 21` 参数可用，22 异常） |
| `cmd.flush_memory((addr, data))` 单地址 | `≤ 12 KiB` | ① MCP 每项及总量硬限制；CLI 逐字节展开还受命令长度限制 |
| `cmd.flush_memory([(addr, data)])` batch | 同上 | 同上 |
| `cmd.flush_memory([(a1,d1), ...])` 多地址 | `≤ 8 项且总计 ≤12 KiB` | ① MCP 项数/总量硬限制；固件更多项可能超时 |
| `bytes([0xVV]) * N` / `ADDR:BYTE*N` 重复填充 | `≤ 12 KiB` | 短表达式只缩短命令文本，不提高 MCP 每项/总量上限 |

## 4. 推荐实际使用边界

```text
单次 flush_memory 批次:
- 老 varargs 接口数据字节 ≤ 20 bytes
- 单地址/单块数据量 ≤ 12 KiB（12288B，MCP 硬限制）
- 地址项数量 ≤ 8
- 多地址总数据量 ≤ 12 KiB（12288B，MCP 硬限制）

不要把固件实验极限换算为更大的 AI 请求。超过上述任一数据边界时，主机必须在接触设备前拒绝；调用方应按 §5 分块。
```

## 5. 超额分块策略

如需写入超过推荐边界的数据，建议上层（host 端脚本 / CLI 用户）分块：

```text
大块连续数据:
- 每块 ≤ 12 KiB（12288B）
- 等待 REPL 返回 >>> 后再发送下一块

离散多地址数据:
- 每批 ≤ 8 个地址项
- 每批总数据量 ≤ 12 KiB（12288B）
- 等待 REPL 返回 >>> 后再发送下一批
```

伪代码示例（仅参考；重复字节超过 12 KiB 时也必须分块，`ADDR:BYTE*N` 不能绕过总量限制）：

```python
# 大块连续数据分块
chunk_size = 12 * 1024
data = open("payload.bin", "rb").read()
for i in range(0, len(data), chunk_size):
    chunk = data[i:i+chunk_size]
    send(f"cmd.flush_memory((0x{0x20002000+i:08X}, bytes([{','.join(f'0x{b:02X}' for b in chunk)}])))")
    wait_for_prompt(">>>")
```

## 6. 校验建议

写入后建议读取头部和尾部数据确认：

```python
cmd.read_ram(0x20002000, 16)
cmd.read_ram(0x20002000 + N - 16, 16)
```

对于大块数据，建议额外读取中间位置：

```python
cmd.read_ram(0x20002000 + N // 2, 16)
```

## 7. 注意事项

- `cmd.flush_memory` 成功时通常只返回 `>>>`，不会打印成功文本。
- 失败时可能打印 `flush fail`，也可能导致 CDC 端口异常或短暂消失，需要复位或重插设备后继续。
- 通过 PC 串口 REPL 直接发送超长 `bytes([...])` 字面量会受 `PIKA_LINE_BUFF_SIZE` 和 PikaPython 解析能力限制。
- 大数据优先使用短表达式，例如 `bytes([0x5A]) * N` 或短 pattern 乘法；CLI 侧用 `ADDR:BYTE*N`（§2.6）。
- **PowerShell 坑**：不要写未加引号的 `0xAA,0xAA,...`——PowerShell 会预处理逗号导致参数被改写（实测目标低字节被写成 `0x70`）。始终用**单引号**包裹整个 item，例如 `flush-memory '0x20008000:0xAA*12288'`。
- 完整 256 字节列表表达式如 `bytes([0, 1, ..., 255]) * k` 在当前测试固件中可能触发 `SyntaxError`。
- 测试地址必须确认是目标 RAM 空闲区，避免覆盖目标程序栈、堆、RTOS 对象、DMA 缓冲或显示缓冲。
- 边界与固件版本、`PIKA_LINE_BUFF_SIZE`、目标 RAM 布局、下载器状态有关，升级固件后应复测。

## 8. 与 CLI `flush-memory` 的关系

`python -m mklink flush-memory` 是 `cmd.flush_memory` 的 CLI 封装。

**重要约束**：

- CLI 对**非重复数据不自动分块**——超出 230B 命令串时直接 `FAIL` 并提示改用 `ADDR:BYTE*N` 或分块。重复字节用 `ADDR:BYTE*N`（§2.6），CLI 自动转短表达式，不受此限。
- 多地址超出 8 项、或单地址非重复数据超长时，CLI 不会自动降级，会返回 `FAIL`。
- MCP 额外硬限制每项 ≤12288B、每次总量 ≤12288B，并在设备发现或 I/O 前拒绝；大数据必须由调用方拆成多个串行 tool call。
- 写入前请自行遵守本文档第 4 章「推荐实际使用边界」与第 5 章「超额分块策略」。
- CLI 的响应解析规则（静默成功 / `flush fail` 兼容 / WARN 降级）见 [commands-memory.md](commands-memory.md) 的 `flush-memory` 章节。
