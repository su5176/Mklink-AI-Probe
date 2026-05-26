# Modbus RTU 调试

> 触发词：Modbus、RS485、scan、dashboard、pointmap、poll、monitor
> 返回索引：[SKILL.md](../SKILL.md)

## Modbus RTU

MKLink V2/V3 自带 TTL 串口，V4 自带 RS485 接口。两者对电脑来说都是额外的 CDC COM 口，可直接用于 Modbus RTU 通信。

> **注意**：Modbus 串口与 SWD 调试串口是不同的 COM 口。使用 `--port` 指定 Modbus 串口号。

> **铁律：同一 Modbus 串口禁止多线程/多进程并发访问。** 一个 COM 口同一时刻只能有一个 `mklink modbus ...` 命令、dashboard 或外部串口工具持有。不要把 `scan`、`read`、`dashboard`、`poll` 等命令并行执行到同一个 `--port`。串口 `open()` / `close()` 必须成对；异常退出也要释放。`ModbusClient` 使用端口级文件锁阻止 mklink 进程间重复打开同一端口；如果提示端口被占用，先停止 dashboard/poll/其他串口工具，再重试。

> **Agent 行为指引：** 批量或自动化执行 Modbus 命令时必须串行访问同一串口。不要使用 parallel tool 同时访问同一 `COMx`，即使都是只读操作也不允许。

### 硬件说明

| 型号 | 接口 | 说明 |
|------|------|------|
| MKLink V2/V3 | TTL (UART) | 额外一个 CDC UART 口 |
| MKLink V4 | RS485 | 额外一个 CDC RS485 口 |

### 支持的功能码

| 功能码 | 名称 | 命令 |
|--------|------|------|
| FC01 | 读线圈 | `modbus read --fc 1` |
| FC02 | 读离散输入 | `modbus read --fc 2` |
| FC03 | 读保持寄存器 | `modbus read --fc 3` |
| FC04 | 读输入寄存器 | `modbus read --fc 4` |
| FC05 | 写单个线圈 | `modbus write --fc 5` |
| FC06 | 写单个寄存器 | `modbus write --fc 6` |
| FC07 | 读异常状态 | `modbus diag --subfunc exception-status` |
| FC15 | 写多个线圈 | `modbus write --fc 15` |
| FC16 | 写多个寄存器 | `modbus write --fc 16` |
| FC22 | 掩码写寄存器 | `modbus diag --subfunc mask-write` |
| FC23 | 读写多寄存器 | `modbus diag --subfunc read-write` |

### 共用串口参数

所有 modbus 子命令共用以下参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--port` | Modbus 串口（必填） | — |
| `--baud` | 波特率 | 9600 |
| `--parity` | 校验位 (N/E/O) | N |
| `--stopbits` | 停止位 (1/2) | 1 |
| `--timeout` | 响应超时（秒） | 1.0 |
| `--retries` | 重试次数 | 3 |

### 扫描从站

```bash
# 扫描全部地址 (1-247)
python -m mklink modbus scan --port COM7

# 指定范围和波特率
python -m mklink modbus scan --port COM7 --baud 19200 --start 1 --end 50
```

### 读取寄存器/线圈

```bash
# FC03: 读 10 个保持寄存器
python -m mklink modbus read --port COM7 --slave 1 --fc 3 --start 0 --quantity 10

# FC03: 十六进制显示
python -m mklink modbus read --port COM7 --slave 1 --fc 3 --start 0 --quantity 10 --format hex

# FC01: 读 8 个线圈
python -m mklink modbus read --port COM7 --slave 1 --fc 1 --start 0 --quantity 8

# FC04: 读输入寄存器
python -m mklink modbus read --port COM7 --slave 1 --fc 4 --start 0 --quantity 4
```

### 写入寄存器/线圈

```bash
# FC06: 写单个寄存器
python -m mklink modbus write --port COM7 --slave 1 --fc 6 --start 0 100

# FC16: 写多个寄存器
python -m mklink modbus write --port COM7 --slave 1 --fc 16 --start 0 100 200 300

# FC05: 写单个线圈 (ON/OFF)
python -m mklink modbus write --port COM7 --slave 1 --fc 5 --start 0 ON

# FC15: 写多个线圈
python -m mklink modbus write --port COM7 --slave 1 --fc 15 --start 0 ON OFF ON
```

### 轮询寄存器（实时表格）

```bash
# 轮询温度和湿度（每秒刷新）
python -m mklink modbus poll --port COM7 --slave 1 --registers "0:uint16:Temp 1:uint16:Humidity"

# 轮询 float 类型变量
python -m mklink modbus poll --port COM7 --slave 1 --registers "0:float:Voltage" --interval 0.5

# 轮询 10 次后停止
python -m mklink modbus poll --port COM7 --slave 1 --registers "0:uint16:Status" --count 10
```

寄存器规格格式：`地址:类型[:名称]` 空格分隔。支持类型：`uint16`、`int16`、`uint32`、`int32`、`float`。

按 `Ctrl+C` 停止轮询。

### 监控通信流量

```bash
# 解码模式（默认）
python -m mklink modbus monitor --port COM7 --slave 1

# 保存日志到文件
python -m mklink modbus monitor --port COM7 --slave 1 --save modbus_log.txt
```

### 诊断功能

```bash
# FC07: 读异常状态
python -m mklink modbus diag --port COM7 --slave 1 --subfunc exception-status

# FC22: 掩码写寄存器
python -m mklink modbus diag --port COM7 --slave 1 --subfunc mask-write --addr 5 --and-mask 0xFFFE --or-mask 0x0006

# FC23: 读写多寄存器
python -m mklink modbus diag --port COM7 --slave 1 --subfunc read-write --addr 0 --read-count 10 --write-values 1,2,3
```

### Web 可视化仪表盘 (dashboard)

启动一个 Web 仪表盘，提供实时数据可视化、交互控制和参数配置。

```bash
# 启动仪表盘（未指定 --profile 时使用内置示例 profile）
python -m mklink modbus dashboard --port COM7 --slave 1 --baud 57600

# 指定项目生成的寄存器配置文件（推荐）
python -m mklink modbus dashboard --port COM7 --slave 1 --profile .mklink/modbus_profile.json

# 不自动打开浏览器，指定 HTTP 端口
python -m mklink modbus dashboard --port COM7 --slave 1 --no-browser --port-http 8080
```

**功能：**
- **Overview 标签页**：实时数据卡片、控制按钮（依 profile 定义）、报警横幅
- **Charts 标签页**：Canvas 实时折线图、变量选择器、悬浮提示、暂停/恢复
- **Parameters 标签页**：分组参数表格（搜索/过滤）、带范围校验的编辑写入、恢复默认值
- **Alarms 标签页**：报警状态网格（16+3 个报警位）、报警历史
- **调试 标签页**：手动 FC01/02/03/04 读、FC05/06/15/16 写、profile 命令选择器和时间戳结果日志

**安全特性：** CSRF 令牌保护、写操作白名单校验、默认仅允许写入 profile 标记为可写或命令使用的地址；调试页任意地址写入必须显式使用 `--allow-arbitrary-writes`。

**参数：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--slave` | 从站地址 | 1 |
| `--profile` | 寄存器配置文件路径 | 未指定时使用内置示例 profile；生产环境应传 `.mklink/modbus_profile.json` 或自定义 JSON |
| `--host` | HTTP 绑定地址 | 127.0.0.1 |
| `--port-http` | HTTP 端口（0=随机） | 0 |
| `--no-browser` | 不自动打开浏览器 | false |
| `--max-points` | 图表最大数据点 | 500 |
| `--duration` | 运行时长秒（0=无限） | 0 |
| `--html` | 自定义仪表盘 HTML 文件路径 | 自动查找（见下方） |
| `--allow-arbitrary-writes` | 允许调试页写入 profile 之外的任意地址 | false |

### Modbus 点表自动生成工作流

当用户要求“生成 Modbus 点表/寄存器表/profile/dashboard 配置”时，优先从项目 C 源码自动检测，不要默认读取 Markdown/CSV。Markdown/CSV 只在用户明确给出该文件路径时解析。

```bash
# 只检测和汇总，不写文件
python -m mklink modbus pointmap detect --project-root . --json

# 从用户指定的 Markdown/CSV 表解析
python -m mklink modbus pointmap detect --project-root . --source docs/registers.md --format markdown
python -m mklink modbus pointmap detect --project-root . --source points.csv --format csv

# 写入 .mklink/modbus_profile.json 和 docs/modbus_pointmap.md
python -m mklink modbus pointmap generate --project-root .
python -m mklink modbus pointmap generate --project-root . --yes
```

Agent 流程：
1. 先运行 `detect`，默认扫描 `*ModbusRegs.h` / `*ModbusRegs.c`，提取 `ModbusRegs[...]`、`.value/.svalue`、命令/报警 bit、`ModbusParamRanges` 范围默认值。
2. 向用户汇报来源文件、寄存器数量、可写数量、命令数量和警告。
3. 未得到确认前不要生成文件；用户确认后运行 `generate` 或使用 `--yes`。
4. 生成后用 `python -m mklink modbus dashboard --profile .mklink/modbus_profile.json ...` 启动仪表盘。

**仪表盘 HTML 加载优先级（由高到低）：**

1. `--html <路径>` — 指定的 HTML 文件
2. `.mklink/modbus_dashboard.html` — **完全自定义 HTML**（需自行通过 `fetch('/profile')` / `fetch('/csrf-token')` 获取数据）
3. `.mklink/modbus_dashboard_template.html` — **用户模板**（保留 `__PROFILE_JSON__`、`__CSRF_TOKEN__`、`__MAX_POINTS__` 三个占位符，服务器自动注入，其余可自由修改）
4. 内置模板 `_dashboard_template.html`（默认）

**自定义样式推荐方式：** 拷贝内置模板到项目目录后修改 CSS 即可：

```bash
# 拷贝内置模板
cp <mklink安装目录>/mklink/modbus/_dashboard_template.html .mklink/modbus_dashboard_template.html
```

模板中必须保留的 3 个占位符（服务器启动时自动替换为实际值）：
- `__PROFILE_JSON__` — 完整 profile JSON 对象（注入到 `var PROFILE = ...`）
- `__CSRF_TOKEN__` — CSRF 令牌字符串（注入到 `var CSRF = "..."`）
- `__MAX_POINTS__` — 图表最大数据点数（注入到 `var MAX_POINTS = ...`）

> **Agent 行为指引：** 当用户要求"Modbus 可视化"且未指定自定义方式时，Agent 应告知用户以上 3 种自定义方式，让用户选择。如果用户只说"启动仪表盘"，直接使用默认内置模板启动即可。
