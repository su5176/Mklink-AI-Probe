<div align="center">

# MKLink AI Probe

**嵌入式一站式调试工具** — 固件烧录 · RTT 可视化 · 内存读写 · HardFault 解码 · Modbus RTU · 远程 GUI

[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Tauri](https://img.shields.io/badge/Tauri-v2-FFC131?logo=tauri&logoColor=black)](https://tauri.app)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white)](https://vuejs.org)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/Aladdin-Wang/Mklink-AI-Probe/blob/master/LICENSE)

[English](#features) · [快速开始](#快速开始) · [命令速查](#命令速查) · [架构](#架构) · [安装与依赖](#安装与依赖)

</div>

---

## Features

| 功能 | 说明 |
|------|------|
| **固件烧录** | 一键烧录 Keil/IAR 工程产物（HEX/BIN），自动检测 MCU 与 FLM |
| **RTT 实时捕获** | SEGGER RTT 数据流捕获，内置波形可视化（RTT View / VOFA+） |
| **SuperWatch** | 高频变量连续采样与实时 Web 波形图 |
| **内存读写** | RAM / Flash / 寄存器 读写操作，十六进制查看器 |
| **符号与类型** | 通过 DWARF/ELF 解析 AXF 符号表、结构体、枚举定义 |
| **HardFault 解码** | Cortex-M Fault 寄存器自动解码，内置 DWARF 源码定位 |
| **Modbus RTU** | 完整 Modbus 调试：扫描、读写、轮询、点表生成、Web Dashboard |
| **串口调试** | 通用 UART 终端，支持自定义协议 Profile |
| **远程 GUI** | FastAPI 后端 + Vue 3 SPA，浏览器即用 |
| **U 盘 Web 入口** | 单个 HTML 通过用户级 URL 协议启动已安装的 Web runtime，Windows/macOS/Linux 通用 |
| **Tauri 桌面** | Rust 桌面应用，Python sidecar，原生窗口体验 |
| **AI Agent 集成** | 通过可用的 MKLink MCP 或 CLI 操作硬件 |

## 快速开始

### 安装

桌面版使用官方安装包；AI Skill 使用完整发布 ZIP。安装步骤、运行依赖和自检见
[安装与更新](references/install.md)。AXF 符号、变量与 HardFault 源码定位默认使用
内置 `pyelftools`，不需要另装 GNU Arm 工具链。

### 三步上手

```bash
# 1. 初始化项目（自动检测 Keil/IAR 工程、MCU 型号、COM 口）
python -m mklink project-init

# 2. 显式使用原生 MKLink 烧录路径
python -m mklink flash

# 3. 捕获 RTT 实时数据
python -m mklink rtt --duration 10
```

AI 自动处理下载任务时，先按[下载优先级](references/firmware-download-priority.md)
选择 IDE、pyOCD 或脱机 API；HPM 使用 ROM API，不加载 FLM。

### 启动 GUI

```bash
# 浏览器模式
python -m mklink gui

```

桌面版安装完成后，从开始菜单或桌面快捷方式启动，无需开发工具。

### 生成跨平台 U 盘 Web 入口

电脑首次安装完整 Mklink skill/runtime 后检查完整依赖、注册用户级协议，并自动
把统一启动页写入 `MICROKEEN` 下载器 U 盘或用户桌面（只复制 `SKILL.md` 不足以
运行 Web 服务）：

```bash
python -m pip install -e ".[gui,mcp]"
python -m mklink web-entry install --quick-launch
```

生成一份可放入任意 U 盘、三个桌面系统通用的单文件 HTML：

```bash
python -m mklink web-entry html --output "/path/to/usb/启动 Mklink Web.html"
```

HTML 短暂倒计时后调用 `mklink-ai-probe://web/start`，不会从 U 盘执行程序；
服务健康后会自动打开 Web GUI。入口复用现有
Mklink Web 服务且不取得所有权；停止按钮只结束由入口自身启动的服务。详见
[跨平台 U 盘 Web 启动入口](references/web-entry.md)。

### High-throughput GUI streams

SystemView, VOFA, RTT, and SuperWatch use an authenticated, versioned binary
WebSocket data plane. A Web Worker owns fixed-capacity typed buffers, while a
shared scheduler limits visible canvas work to 30 FPS. Pausing rendering or
hiding a tab does not pause acquisition; backend and frontend loss counters
remain independent.

Display refresh rate is not a guarantee of probe throughput. Actual acquisition
limits depend on the target firmware, probe, transport, and host.

### 在线烧录与脱机烧录

- **在线烧录**：打开 GUI 的 `/online-flash` 页，由主机通过 pyOCD/CMSIS-DAP 实时控制 **MKLink** 探针；支持目标搜索、CMSIS-Pack 按需下载、HEX/BIN 预览、擦除、编程、校验和复位。在线探针列表只接受 MKLink CMSIS-DAP，不会把其他厂商的 CMSIS-DAP 当作可选设备。
- **脱机烧录**：打开与“在线烧录”并列的 `/offline-flash` 页，配置有序的多个 HEX/BIN 固件、BIN 基地址、多个 FLM 及其 Flash/RAM 基地址、固件与算法绑定、自动烧录次数、IDCODE 超时和 SWD 速率。FLM 可来自本地文件、项目/Keil 配置或已安装的 CMSIS-Pack，配置可预览并直接部署到 MKLink U 盘。型号通过 `cmd.get_version()` 识别；V2/V3 固定生成 `python/offline_download.py`，V4 支持安全的自定义 `.py` 文件名并由下载器屏幕选择。

在线烧录首次使用某个 MCU 时，先更新 Pack 索引，搜索并下载对应 DFP。Pack 只在需要时下载，安装后缓存在当前用户的 `%LOCALAPPDATA%\MKLink\pyocd` 下（可用 `MKLINK_PYOCD_HOME` 改变根目录）；离线时可继续使用已缓存的索引和 Pack。在 GUI 中可取消当前 Pack 操作或删除未被任务使用的指定版本；不要把 `.pack` 文件放入 Git 或发布资源。索引更新和 Pack 下载使用启动 `mklink serve`/GUI 进程时的 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 环境；本地缓存命中不需要网络。

HEX 固件使用文件内的绝对地址；BIN 没有地址信息，必须明确填写与目标 Flash 匹配的基地址（例如 `0x08000000`）。在线烧录与 RTT、SystemView、VOFA、SuperWatch 等调试会话共用目标调试资源；冲突时应先停止或确认交接当前会话。点击“停止”是协作式取消，当前底层操作返回后才会断开并释放资源，不要在等待期间拔除探针。

> **验证边界：**“Pack 可用”只表示 pyOCD 能解析目标和 Flash Algorithm，不等于该 MCU 已通过 MKLink 真机烧录验证。没有实际验证证据的组合应视为未验证，且在线烧录不支持非 MKLink 探针。

## 命令速查

| 命令 | 说明 |
|------|------|
| `project-init` | 初始化项目配置（自动检测 Keil/IAR、MCU、COM 口） |
| `flash` | 一站式烧录（连接 → IDCODE → FLM → 烧录） |
| `rtt` | RTT 实时捕获（支持 `--visualize`） |
| `read-ram` | 读取 RAM 数据（十六进制 dump） |
| `write-ram` | 写入 RAM 并回读验证 |
| `read-flash` | 读取 Flash 数据 |
| `read-reg` | 读取内存映射寄存器 |
| `vofa` | VOFA+ 实时变量观测 |
| `watch` | 按变量名读取快照（支持 `struct.field`） |
| `superwatch` | 高频连续采样（支持 `--visualize`） |
| `symbols` | 从 AXF/ELF 列出 RAM 变量符号 |
| `typeinfo` | DWARF 类型查询（结构体/枚举） |
| `hardfault` | Cortex-M Fault 寄存器解码 |
| `memmap` | AXF 段表分析（RAM/Flash 占用） |
| `modbus` | Modbus RTU 调试（scan/read/write/poll/dashboard） |
| `serial` | 通用串口调试 |
| `serve` | 远程调试 REST API 服务器 |
| `gui` | 启动 Web GUI（FastAPI + Vue） |
| `web-entry` | 安装跨平台 HTML 启动协议、生成 HTML、管理入口自有 Web 服务 |
| `discover` | 发现 MKLink 探针端口 |

完整命令文档见 [references/](references/) 目录。

## 架构

```
┌─────────────────────────────────────────────┐
│            Tauri 桌面应用 / 浏览器 GUI         │
│          (Vue 3 + TypeScript SPA)           │
├─────────────────────────────────────────────┤
│         FastAPI 服务 (REST + SSE + WS)       │
│               port 8765                     │
├─────────────────────────────────────────────┤
│         Device / DeviceDispatcher            │
│      MKLinkSerialBridge (pyserial)          │
│            进程级 SerialLock                 │
├─────────────────────────────────────────────┤
│         MKLink 探针 (USB CDC)               │
│              SWD / JTAG                     │
├─────────────────────────────────────────────┤
│         目标 MCU (Cortex-M)                 │
└─────────────────────────────────────────────┘
```

**两种使用方式：**

- **CLI 模式** — `python -m mklink <command>`，适合脚本化和 AI Agent 集成
- **GUI 模式** — 浏览器或 Tauri 桌面窗口，可视化操作

**两种服务后端：**

- **FastAPI**（主模式）— REST API + SSE 流 + WebSocket JSON-RPC，托管 Vue SPA
- **Raw Socket**（旧版）— 仅 WebSocket JSON-RPC

## 安装与依赖

普通使用请选择官方桌面安装包或完整 Skill ZIP，见[安装与更新](references/install.md)。
桌面版自带后端；Python Skill 安装 `.[gui,mcp]` 并使用包内 Web 资源。
运行 MKLink 不需要 Node、Rust、MSVC 或源码构建。

AXF/ELF 默认由内置 `pyelftools` 解析，无需安装 GNU Arm 工具链。
仅用户显式选择外部兼容后端时才需要 `readelf` / `addr2line`。
烧录目标 MCU 工程所需的 IDE/工具链由对应工程决定，与编译 MKLink 本体不同。

## 支持的 MCU

通过 `mklink/mcu_profiles.json` 管理，支持主流 Cortex-M 系列：

- Nationstech N32G435/G455/G457
- ST STM32F103/F407/F429/H743
- GD32F103/F407/E230
- MM32F327X
- 更多持续添加中...

## 项目结构

```
mklink-flash/
├── mklink/                  # 核心 Python 包
│   ├── bridge.py            # 串口通信核心
│   ├── device.py            # 设备抽象层
│   ├── cli.py               # CLI 命令调度
│   ├── flash.py             # 固件烧录
│   ├── rtt.py               # RTT 功能
│   ├── superwatch.py        # 高频变量监控
│   ├── hardfault.py         # Fault 解码
│   ├── remote/              # FastAPI 远程服务
│   ├── modbus/              # Modbus RTU
│   └── serial/              # 串口通信
├── gui/                     # Vue 3 + Tauri GUI
│   ├── src/                 # 前端源码
│   └── src-tauri/           # Rust 后端
├── references/              # 命令文档
├── scripts/                 # 示例脚本
└── agents/                  # AI Agent 配置
```

## License

MIT License
