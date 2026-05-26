<div align="center">

# MKLink AI Probe

**嵌入式一站式调试工具** — 固件烧录 · RTT 可视化 · 内存读写 · HardFault 解码 · Modbus RTU · 远程 GUI

[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Tauri](https://img.shields.io/badge/Tauri-v2-FFC131?logo=tauri&logoColor=black)](https://tauri.app)
[![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white)](https://vuejs.org)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](#features) · [快速开始](#快速开始) · [命令速查](#命令速查) · [架构](#架构) · [开发构建](#开发构建)

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
| **HardFault 解码** | Cortex-M Fault 寄存器自动解码，addr2line 源码定位 |
| **Modbus RTU** | 完整 Modbus 调试：扫描、读写、轮询、点表生成、Web Dashboard |
| **串口调试** | 通用 UART 终端，支持自定义协议 Profile |
| **远程 GUI** | FastAPI 后端 + Vue 3 SPA，浏览器即用 |
| **Tauri 桌面** | Rust 桌面应用，Python sidecar，原生窗口体验 |
| **AI Agent 集成** | Claude / OpenAI Agent 可通过 CLI 直接操控硬件 |

<p align="center">
  <img src="skill1.png" width="400" />
  <img src="skill2.png" width="400" />
  <img src="skill3.png" width="400" />
</p>

## 快速开始

### 安装

```bash
# 安装 Python 包（可编辑模式）
pip install -e .

# 安装 GUI 依赖（可选）
pip install -e ".[gui]"
```

### 三步上手

```bash
# 1. 初始化项目（自动检测 Keil/IAR 工程、MCU 型号、COM 口）
python -m mklink project-init

# 2. 烧录固件
python -m mklink flash

# 3. 捕获 RTT 实时数据
python -m mklink rtt --duration 10
```

### 启动 GUI

```bash
# 浏览器模式
python -m mklink gui

# Tauri 桌面应用（需额外依赖，见开发构建）
cd gui && npx tauri dev
```

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

## 开发构建

### Python 包

```bash
pip install -e .                    # 基础安装
pip install -e ".[gui]"             # 含 GUI 依赖
pip install -e ".[e2e]"             # 含 E2E 测试依赖
python -m mklink serve --port 8765  # 启动 API 服务
```

### Tauri 桌面应用

```bash
cd gui
npm install                         # 安装前端依赖
npm run dev                         # Vite 开发服务器（仅前端）
npx tauri dev                       # 完整 Tauri 开发模式
npx tauri build                     # 生产构建（MSI + NSIS）
```

> Tauri 构建需要 Rust 工具链和 MSVC Build Tools，详见 [references/install.md](references/install.md)。

### 测试

```bash
# Python 单元测试
pytest

# 前端单元测试
cd gui && npm test

# GUI E2E 测试（Playwright + Mock API）
cd gui && npm run build
pytest _maintainer/testing/tests/e2e/gui -q --run-e2e

# HIL 硬件测试（需 MKLink 探针 + 目标 MCU）
pytest _maintainer/testing/tests/e2e/hil -q --run-hil
```

## 可选依赖

| 工具 | 用途 | 安装 |
|------|------|------|
| `arm-none-eabi-readelf` | AXF 符号解析、DWARF 类型查询、HardFault 源码定位 | `winget install Arm.GnuArmEmbeddedToolchain` |
| Node.js | Tauri 桌面应用、Vue 前端构建 | `winget install OpenJS.NodeJS.LTS` |
| Rust | Tauri v2 桌面应用编译 | [rustup.rs](https://rustup.rs) |

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

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
