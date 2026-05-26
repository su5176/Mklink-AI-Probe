# 烧录、RTT 与工程配置

> 触发词：flash、rtt、project-init、discover、Keil、IAR、copy-flm
> 返回索引：[SKILL.md](../SKILL.md)

## 命令说明

### 连接管理

#### `python -m mklink discover`
发现 MKLink CDC 端口。优先通过 USB 描述符匹配，然后对每个端口执行2步确认探测：
1. **被动监听**：打开串口后等待设备主动上报 "hello microkeen"
2. **主动探测**：发送回车 `\n`，检测 ">>>" 提示符

检测到的端口会自动保存到 `.mklink/config.json`（如果配置已存在）。

**串口操作互斥保护**：同一时刻只有一个进程可以操作串口。如果另一个进程正在使用串口，会提示"串口正被其他进程使用"。

```
[OK] 发现 MKLink CDC 端口: COM6
[AUTO] 已更新配置中的端口为 COM6
```

#### `python -m mklink test --port COM6`
测试连接并获取 IDCODE。

```
[*] 连接 COM6 ...
[OK] 连接成功
IDCODE 响应: idcode = 0X2BA01477
[*] 已断开连接
```

### 烧录操作

#### `python -m mklink flash [--port COM6] [--hex path.hex]`
一站式烧录。`load.hex()` / `load.bin()` 内部按扇区边擦边写，无需单独擦除（独立擦除命令为全片擦除）。自动从 `.mklink/keil_project.json` 读取 HEX 路径和 MCU 配置。

```
[AUTO] 从 project_info.json 自动获取 hex_path: path/to/build/firmware.hex
[OK] IDCODE: 0x2BA01477
[OK] FLM 加载成功
开始烧录 firmware.hex ...
[OK] 烧录成功 (3540ms)
```

**烧录前提条件：**
1. MICROKEEN 磁盘已插入（FLM 文件需在其 FLM 目录中）
2. 已运行 `project-init`（或 `.mklink/` 配置已存在）
3. HEX 文件已编译生成

### RTT 调试

#### `python -m mklink rtt [--port COM6] [--duration 10]`
一站式 RTT 捕获。自动从 `.mklink/rtt_config.json` 读取 RTT 地址。如果 MAP 文件比配置新，会自动更新 RTT 地址。

```
[*] 连接 COM6 ...
[OK] 连接成功
[OK] 从配置读取 RTT 地址: 0x20000e24
[OK] RTT 已启动 (控制块: 0x20000e24)
[*] 读取 RTT 输出 10.0 秒...

[RTT] counter: 75 | adc: 2164 | sensor: 42
[RTT] counter: 76 | adc: 2162 | sensor: 41
...
[OK] RTT 会话结束
```

**RTT 前提条件：**
1. 固件已集成 SEGGER RTT（运行 `rtt-integrate` 可自动集成）
2. 固件已编译并烧录到芯片
3. `.mklink/rtt_config.json` 中有 `rtt_addr`

#### `python -m mklink rtt-integrate --project-root .`
集成 RTT 源码到项目（支持 Keil 和 IAR）。自动完成：
1. 复制 RTT 源文件到项目的 src/ 和头文件目录（自动检测）
2. 将文件添加到 Keil 或 IAR 工程
3. 在 main.c 中添加 USE_RTT 宏保护的 SEGGER_RTT_Init() 调用
4. 在工程中添加 `USE_RTT` 预处理器宏
5. 从 MAP 文件查找并保存 _SEGGER_RTT 地址

**初始化验证：** 集成后会自动验证 main.c 中的 RTT 代码是否正确写入。如果验证失败，会明确报告失败原因。

**USE_RTT 宏：** 所有 RTT 代码被 `#ifdef USE_RTT` 宏保护。生产固件时只需从工程定义中移除 `USE_RTT` 即可禁用所有 RTT 输出，无需修改代码。
- Keil: Options → C/C++ → Preprocessor Symbols → Define 中管理 `USE_RTT`
- IAR: Project → Options → C/C++ Compiler → Preprocessor → Defined symbols 中管理 `USE_RTT`

**头文件目录自动检测：** 自动从 Keil（IncludePath）或 IAR（CCIncludePath2）工程配置中提取头文件搜索路径，将 RTT 头文件放到正确的位置。

**运行后需重新编译项目。**

#### `python -m mklink rtt-find <map_path>`
从 .map 文件解析 RTT 地址。

```bash
python -m mklink rtt-find "path/to/build/Project.map"
# [OK] _SEGGER_RTT 地址: 0x20000e24
```

### RTT View（Web 仪表盘）

#### `python -m mklink rtt --visualize [选项]`
启动 Web 仪表盘，在浏览器中实时显示 RTT 数据的趋势图表。自动解析 RTT 输出中的数值数据，以 Chart.js 折线图展示。

```
python -m mklink rtt --visualize
```

自动完成：发现端口 → 连接 → 启动 RTT → 启动 Web 服务器 → 打开浏览器 → 实时绘图

**RTT View 选项：**

| 选项 | 说明 |
|------|------|
| `--host 127.0.0.1` | HTTP 服务器绑定地址（默认 127.0.0.1） |
| `--port-http 0` | HTTP 端口（默认 0 = 随机可用端口） |
| `--no-browser` | 不自动打开浏览器 |
| `--max-points 500` | 浏览器最大数据点数（默认 500） |
| `--parser auto` | 解析策略：`auto`、`kv`、`csv`、`regex` |
| `--regex-pattern "..."` | 正则解析模式（含命名分组） |
| `--csv-headers "col1,col2"` | CSV 列名，逗号分隔 |
| `--duration 30` | 运行时长（秒，默认 10） |

**解析策略说明：**
- `kv`（键值对）：自动解析 `name: value | name2: value2` 格式，也支持 `key=value` 格式
- `csv`：解析逗号/制表符/空格分隔的数值
- `regex`：使用自定义正则表达式（含 Python 命名分组 `(?P<name>...)`）
- `auto`（默认）：自动检测前几行输出来选择策略

**仪表盘功能：**
- 实时折线图，每个变量独立曲线
- 变量选择芯片，点击切换显示/隐藏
- 统计面板：当前值、最小值、最大值、平均值
- 按 `Space` 暂停/恢复更新
- 按 `L` 显示/隐藏原始日志面板
- 支持多个浏览器标签页同时连接

**RTT 仪表盘 HTML 加载优先级（由高到低）：**

1. `.mklink/rtt_viewer.html` — **完全自定义 HTML**（需自行通过 SSE `/stream` 端点获取数据）
2. `.mklink/rtt_viewer_template.html` — **用户模板**（保留 `__MAX_POINTS__` 占位符，服务器自动注入，其余可自由修改）
3. 内置模板 `_rtt_viewer_template.html`（默认）

**自定义样式推荐方式：** 拷贝内置模板到项目目录后修改 CSS 即可：

```bash
cp <mklink安装目录>/mklink/_rtt_viewer_template.html .mklink/rtt_viewer_template.html
```

模板中必须保留的占位符（服务器启动时自动替换为实际值）：
- `__MAX_POINTS__` — 图表最大数据点数（注入到 `var MAX_POINTS = ...`）

> **Agent 行为指引：** 当用户要求"RTT View"且未指定自定义方式时，直接使用默认内置模板启动即可。如果用户提到"自定义样式"或"调整布局"，告知用户可拷贝模板到 `.mklink/` 下修改。

**使用示例：**

```bash
# RTT View（自动检测格式）
python -m mklink rtt --visualize

# 指定解析器和时长
python -m mklink rtt --visualize --parser kv --duration 60

# 自定义正则解析（命名分组对应曲线名）
python -m mklink rtt --visualize --regex-pattern "counter:\s*(?P<counter>\d+).*adc:\s*(?P<adc>\d+)"

# CSV 格式，指定列名
python -m mklink rtt --visualize --parser csv --csv-headers "counter,adc,sensor"

# 固定端口，不打开浏览器（用于远程查看）
python -m mklink rtt --visualize --port-http 8888 --no-browser
```

### MICROKEEN 磁盘管理

#### `python -m mklink copy-flm`
自动将项目对应的 FLM 文件从 Keil 安装目录拷贝到 MICROKEEN 磁盘的 FLM 目录。

**仅适用于 Keil 项目**。IAR 项目使用内置 .board flash loader，不需要 FLM 文件。

`project-init` 会自动执行此操作，通常不需要手动运行。

### 配置管理

#### `python -m mklink project-init`
初始化项目配置。自动检测 IAR/Keil 工程类型，解析对应工程文件、匹配 MCU、自动发现 COM 口、拷贝 FLM（仅 Keil）。

#### `python -m mklink project-info`
显示当前项目配置状态（COM 口、MCU、IDE 类型、HEX/MAP 路径、RTT 配置等）。

#### `python -m mklink iar-parse`
解析并显示 IAR .ewp 工程详细信息。

#### `python -m mklink keil-parse`
解析并显示 Keil .uvprojx 工程详细信息。

---

## 自动配置检查

所有硬件操作命令（`flash`、`rtt`、`test`）执行前会自动检查 `.mklink/` 配置：
- `config.json` — COM 口、MCU 类型、IDE 类型
- `project_info.json` — HEX/MAP/OUT 文件路径（IDE-agnostic）
- `rtt_config.json` — RTT 地址
- MICROKEEN 磁盘 FLM 文件（仅 Keil 需要）

如果配置缺失或无效，会提示运行 `python -m mklink project-init`。
