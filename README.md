<div align="center">

# MKLink AI Probe

**面向嵌入式开发的 AI 调试工具**<br>
固件烧录 · Web GUI · RTT/SystemView · SuperWatch · 内存与寄存器 · HardFault · Modbus · 串口 · MCP

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Tauri](https://img.shields.io/badge/Tauri-v2-FFC131?logo=tauri&logoColor=black)](https://tauri.app)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white)](https://vuejs.org)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[最便捷安装](#最便捷安装交给-ai) · [Web GUI](#web-gui-安装与使用) · [U 盘入口](#u-盘单-html-快速启动) · [AI / MCP](#ai-agent--mcp) · [命令速查](#命令速查) · [开发构建](#开发构建)

**代码仓库：** [GitHub](https://github.com/Aladdin-Wang/Mklink-AI-Probe) · [Gitee](https://gitee.com/Aladdin-Wang/Mklink-AI-Probe)

</div>

---

## 这是什么

MKLink AI Probe 把 MKLink/MicroLink 探针、嵌入式工程和 AI Agent 连接起来。既可以让 AI 直接完成烧录、读写内存、采集 RTT、分析 HardFault，也可以使用浏览器 Web GUI 或 Windows 桌面上位机手动操作。

| 功能 | 说明 |
|------|------|
| **固件烧录** | 支持 Keil/IAR 工程、HEX/BIN、pyOCD 在线烧录和 MKLink 脱机下载 |
| **Web GUI** | 浏览器中完成工程配置、在线/脱机烧录、Dashboard 和文件选择 |
| **RTT View / VOFA+** | 实时日志与曲线，支持坐标轴、缩放、拖动、暂停保留和通道发送 |
| **SystemView** | 采集和分析任务切换、ISR、CPU 占用，提供时间轴和报告 |
| **SuperWatch** | 从 AXF 符号中选择变量并实时绘图，原始数据可带时间戳保存 |
| **内存与调试** | RAM、Flash、寄存器读写，断点、暂停、继续和单步控制 |
| **符号与故障分析** | 内置 ELF/DWARF 解析，支持符号、类型、内存布局和 HardFault 定位 |
| **串口与 Modbus** | 通用串口终端、Modbus RTU 扫描/读写/轮询和 Dashboard |
| **AI Agent / MCP** | 提供仓库 Skill、MCP tools 和 CLI，方便不同 AI 编码助手调用 |
| **Windows 桌面端** | Tauri v2 原生窗口，安装包自带后端，不要求用户另装 Python |
| **U 盘 Web 入口** | 一份普通 HTML 可在 Windows、macOS、Linux 上启动已安装的 Web 客户端 |

## 最便捷安装：交给 AI

最省事的方式是把下面任意一个仓库链接交给支持本地文件和终端操作的 AI 编码助手：

- GitHub：<https://github.com/Aladdin-Wang/Mklink-AI-Probe>
- Gitee：<https://gitee.com/Aladdin-Wang/Mklink-AI-Probe>

可以直接这样告诉 AI：

> 请从这个仓库安装 MKLink AI Probe。读取仓库中的 `Mklink-AI-Probe/SKILL.md` 和安装说明，把完整 Skill 安装到你的用户级 Skill 目录，并安装 Web GUI 与 MCP 所需依赖。安装后运行自检，并帮我打开 Web GUI。

AI 会根据自己的产品和操作系统选择正确的 Skill 目录，完成下载、依赖安装和验证。不同 AI 的全局目录并不相同，不建议用户手工猜目录或只复制一个 `SKILL.md` 文件；必须保留 `Mklink-AI-Probe/` 中的 Python 包、GUI 资源和 `references/`。

安装完成后，日常使用只要对 AI 说：

> 打开 MKLink AI Probe 的 Web GUI，并使用我的嵌入式工程。

> **说明：**普通网页聊天模型无法直接安装本机软件。需要使用能够访问本地文件、执行命令的 Codex、Claude Code、Cursor 等编码助手，或由它们连接到相应的本地执行环境。

## Web GUI 安装与使用

Web GUI 是运行在本机的浏览器上位机。后端负责连接探针，浏览器负责显示配置、烧录、RTT、SystemView、SuperWatch、串口和 Modbus 界面。默认只监听本机 `127.0.0.1`。

### 方式一：让 AI 安装并启动（推荐）

把仓库链接和下面这句话交给 AI：

> 安装 MKLink AI Probe 的完整运行环境，包含 GUI 和 MCP 依赖；确认仓库自带的 `gui/dist` 可用，然后为我的工程启动 `python -m mklink gui`。

AI 完成后会打开浏览器。默认地址是：

```text
http://127.0.0.1:8765
```

### 方式二：手动从源码安装

准备 Python 3.9 或更高版本和 Git。正常使用仓库自带的 Web 页面不需要 Node.js 或 Rust。

```bash
# GitHub 与 Gitee 二选一
git clone https://github.com/Aladdin-Wang/Mklink-AI-Probe.git
# git clone https://gitee.com/Aladdin-Wang/Mklink-AI-Probe.git

cd Mklink-AI-Probe/Mklink-AI-Probe
python -m pip install -e ".[gui,mcp]"
python -m mklink gui
```

浏览器会自动打开。关闭运行命令的终端或按 `Ctrl+C` 即可停止服务。

常用启动方式：

```bash
# 把指定工程作为当前工程打开
python -m mklink gui --project-root "/path/to/project"

# 使用其他端口
python -m mklink gui --port 8876

# 只启动服务，不自动打开浏览器
python -m mklink gui --no-browser
```

如果提示缺少前端资源，说明安装内容不完整。请重新下载完整仓库或完整 Skill；只有在需要重新编译前端时才需要 Node.js。

### 第一次打开怎么用

1. 将 MKLink 探针连接电脑和目标板。
2. 打开“配置”，选择工程目录和探针；也可以直接选择本机的 AXF/ELF/OUT 文件。
3. 点击连接，确认目标与符号信息加载成功。
4. 根据需要进入“在线烧录”“脱机烧录”或“Dashboard”。
5. 在 Dashboard 中使用 RTT View、VOFA+、SystemView、SuperWatch、内存、串口或 Modbus。

AXF/ELF/OUT 已包含 SuperWatch 所需的符号和类型信息，也可用于 RTT/SystemView 地址搜索，不需要用户另外加载 MAP 文件。浏览器不能把本机绝对路径直接交给后端，因此网页版选择 AXF/ELF/OUT 时，会把文件上传到当前连接的 Mklink 服务所管理的 `.mklink/uploads/file-sources` 目录。默认本机模式不会把文件发送到互联网；单个文件上限为 256 MiB。Windows 桌面版使用原生文件对话框，不走这一步上传。

RTT View 与 SuperWatch 共用目标调试资源。切换后点击开始时，系统会先停止原来的采集功能，再启动新的功能，避免同时占用探针。

Web GUI 默认只绑定 `127.0.0.1`。普通用户不要把服务改为 `0.0.0.0` 或直接暴露到公网；需要远程集成时请使用 `serve` 的认证接口，并先阅读 [远程服务与 GUI](Mklink-AI-Probe/references/commands-remote-gui.md)。

### Windows 桌面上位机

不想使用 Python 和命令行的 Windows 用户，可以从仓库 Release 页面下载标准 NSIS 安装包：

- [GitHub Releases](https://github.com/Aladdin-Wang/Mklink-AI-Probe/releases)
- [Gitee 仓库](https://gitee.com/Aladdin-Wang/Mklink-AI-Probe)

桌面版已打包 Python 后端和 Web 前端。安装后直接打开 **Mklink AI Probe**，不需要另外启动浏览器服务。Windows 可能对未做 Authenticode 签名的安装包显示“未知发布者”，请从上述官方仓库下载并核对 Release 中的校验信息。

## U 盘单 HTML 快速启动

U 盘中只需保存一份普通 HTML，Windows、macOS 和 Linux 使用同一个文件。受浏览器安全限制，HTML 本身不能携带或启动 Python 程序，所以每台电脑仍需提前安装一次完整 Mklink runtime，并注册用户级启动协议。

### 每台电脑只做一次

可以让 AI 执行：

> 为已安装的 MKLink AI Probe 注册 Web U 盘启动入口，并验证 `web-entry status`。

对应命令：

```bash
python -m mklink web-entry install
```

### 生成 U 盘 HTML

```bash
python -m mklink web-entry html --output "/path/to/usb/启动 Mklink Web.html"
```

也可以注册协议并同时生成：

```bash
python -m mklink web-entry install --html "/path/to/usb/启动 Mklink Web.html"
```

以后用户双击 U 盘中的 HTML，再点击“启动 Web 客户端”即可。入口会复用已有 Mklink Web 服务；停止按钮只结束由这个入口启动的服务，不会关闭 AI/MCP、手动启动的 `serve` 或 Windows 桌面上位机。

| 系统 | 一次性准备 |
|------|------------|
| Windows 10/11 | 安装完整 runtime，执行 `web-entry install`，首次点击时允许浏览器打开协议处理器 |
| macOS | 安装与 Intel/Apple Silicon 匹配的 runtime，执行注册，首次点击时确认打开 Launcher |
| Linux 桌面 | 安装 runtime 和 `xdg-utils`，执行注册，并准备 USB HID/串口权限或 udev 规则 |

Windows 已完成真实协议和 U 盘 HTML 闭环验证；macOS 与 Linux 的注册文件和命令已有自动化测试，但仍需在对应操作系统上完成实机验证。

完整说明与故障排查见 [跨平台 U 盘 Web 启动入口](Mklink-AI-Probe/references/web-entry.md)。

## AI Agent / MCP

仓库中的 [SKILL.md](Mklink-AI-Probe/SKILL.md) 是 AI 能力入口，`references/` 提供按任务拆分的详细说明。支持 MCP 的客户端优先使用结构化 tools，不支持 MCP 时可回退到 CLI。

```bash
cd Mklink-AI-Probe
python -m pip install -e ".[mcp]"
python -m mklink mcp
```

Agent 驱动的固件下载优先使用工程已有 IDE 的原生流程，例如先由 Keil 编译并直接下载；IDE 不可用或不适用时再使用 pyOCD 在线烧录，最后才使用 MKLink 脱机下载 API。某个后端已经开始执行后如果失败，会停止并报告原因，不会静默换后端继续烧录。

## 典型工作流

```bash
# 自动识别 Keil/IAR 工程、MCU 和探针
python -m mklink project-init

# 烧录固件
python -m mklink flash

# 捕获 10 秒 RTT
python -m mklink rtt --duration 10

# 启动浏览器上位机
python -m mklink gui
```

### SystemView RTOS 跟踪

目标固件通过 RTT 上行通道输出 SystemView 事件，Mklink 可以直接解码任务切换、ISR 和 CPU 占用，不依赖 J-Link PC 工具。

```bash
python -m mklink rtt-integrate --project-root .
python -m mklink systemview-integrate --project-root .
python -m mklink systemview --duration 10
python -m mklink systemview-analyze --duration 6
python -m mklink systemview-report --duration 6 --out report.html
```

详细说明见 [SystemView 与 RT-Thread 集成](Mklink-AI-Probe/references/systemview-rtthread.md)。

### 高速内存读写

`dump-memory` 适合连续或大块读取，`flush-memory` 适合不干扰采集流的 RAM 写入。实际边界取决于探针、目标芯片和连接质量，应从较小数据量开始验证。

```bash
python -m mklink dump-memory 0x20000000:16
python -m mklink dump-memory 0x08000000:524288 --save flash.bin
python -m mklink flush-memory 0x20010000:0xDE,0xAD,0xBE,0xEF --verify
python -m mklink flush-memory "0x20008000:0xAA*4096" --verify
```

边界和格式见 [dump-memory](Mklink-AI-Probe/references/commands-memory.md) 与 [flush-memory](Mklink-AI-Probe/references/flush-memory.md)。

## 命令速查

| 命令 | 说明 |
|------|------|
| `project-init` / `project-info` | 初始化或查看工程配置 |
| `flash` | 兼容的一站式在线烧录命令 |
| `gui` | 启动 Web GUI 并打开浏览器 |
| `serve` | 启动 REST、WebSocket 和 Web 静态服务 |
| `web-entry` | 注册 U 盘 HTML 协议、生成 HTML、启动/停止入口服务 |
| `mcp` | 启动供 AI Agent 使用的 MCP server |
| `rtt` / `rtt-integrate` / `rtt-find` | RTT 捕获、源码集成和控制块定位 |
| `vofa` / `watch` / `superwatch` | 变量观测和高速采样 |
| `systemview*` | SystemView 集成、采集、分析和报告 |
| `read-ram` / `write-ram` / `read-flash` / `read-reg` | 内存与寄存器读写 |
| `dump-memory` / `flush-memory` | 高速内存采集与静默写入 |
| `symbols` / `typeinfo` / `memmap` | ELF/DWARF 符号、类型和内存布局 |
| `hardfault` | Cortex-M Fault 寄存器与源码定位 |
| `modbus` / `serial` | Modbus RTU 与通用串口调试 |
| `halt` / `resume` / `step` / `break` | CPU 运行控制与断点 |
| `resources` | 查看或释放本地资源占用 |
| `discover` / `test` / `version` | 发现探针、连接测试和版本查询 |

完整命令文档见 [references 目录](Mklink-AI-Probe/references/)。

## 架构

```text
AI Agent / MCP       浏览器 Web GUI       Windows Tauri 桌面端
        \                  |                  /
         +--------- Python FastAPI ----------+
                  REST / SSE / WebSocket
                            |
                Device / Resource Manager
                            |
                 MKLink (USB CDC / CMSIS-DAP)
                            |
                       目标 MCU
```

三种主要使用方式可以并存：AI 通过 MCP/CLI 调用，用户通过 Web GUI 操作，Windows 用户也可以使用自带 sidecar 的桌面上位机。U 盘 HTML 只是 Web GUI 的快捷入口，不会改变其他模式的生命周期。

## 开发构建

开发目录是包含 `pyproject.toml` 的 `Mklink-AI-Probe/`：

```bash
cd Mklink-AI-Probe
python -m pip install -e ".[gui,mcp,test]"
python -m pytest

cd gui
npm install
npm test
npm run build
```

Tauri 开发与标准 NSIS 构建需要 Node.js、Rust 和 Windows MSVC Build Tools。仓库默认只生成标准 NSIS；详细流程见 [安装说明](Mklink-AI-Probe/references/install.md) 和仓库内 [Tauri 构建 Skill](Mklink-AI-Probe/skills/tauri-gui-builder/SKILL.md)。

内置 `pyelftools` 是默认 ELF/DWARF 后端。GNU Arm 的 `readelf`、`addr2line` 只在用户明确选择外部后端时才需要，不是 Web GUI、符号或 HardFault 功能的默认前置条件。

## 支持范围

- 通过 SWD/JTAG 调试 ARM Cortex 目标，具体型号取决于内置 profile、pyOCD target 或可用 CMSIS-Pack。
- 常用 ST、Nationstech、GD、MM32 等目标已有配置；其他芯片可以补充 profile 或 Pack。
- HPM 目标使用专用 ROM API/BIN 下载流程，不使用 FLM。
- 在线烧录页面只接受 MKLink CMSIS-DAP 探针，不把其他厂商探针列为可选设备。

## 项目结构

```text
Mklink-AI-Probe/                 # Git 仓库根目录
├── README.md                    # 项目首页与用户指南
└── Mklink-AI-Probe/             # Skill、Python 包和 GUI 根目录
    ├── SKILL.md                 # AI Agent 能力入口
    ├── .mcp.json                # MCP server 配置
    ├── mklink/                  # Python 核心、CLI、MCP 与远程服务
    ├── gui/                     # Vue 3 + Tauri，dist 为已构建 Web GUI
    ├── references/              # 安装、命令和工作流说明
    ├── skills/                  # 仓库内维护与构建流程
    ├── docs/                    # 项目记忆和验证资料
    └── _maintainer/testing/     # 自动化测试
```

## License

[MIT License](LICENSE)
