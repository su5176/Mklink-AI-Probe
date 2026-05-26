# 远程服务与 GUI

> 触发词：serve、gui、FastAPI、uvicorn、Tauri、桌面应用、远程调试
> 返回索引：[SKILL.md](../SKILL.md)

## 依赖安装

GUI 与 Tauri 桌面应用依赖安装详见 [references/install.md](install.md) 的「GUI 依赖」章节。


## Web GUI（浏览器模式）

### serve — 远程调试服务器

```powershell
python -m mklink serve --host 127.0.0.1 --port 8765
# 启动 FastAPI 服务器，访问 http://127.0.0.1:8765/docs 查看 API 文档
```

选项：
- `--backend {legacy,fastapi}` — 选择后端（默认 fastapi）
- `--project-root <dir>` — 指定项目根目录

### gui — 一键启动 Web GUI

```powershell
# 一键启动（自动构建前端、启动后端、打开浏览器）
python -m mklink gui

# 指定端口和设备
python -m mklink gui --port 8765 --device-port COM6

# 不自动打开浏览器
python -m mklink gui --no-browser
```

GUI 启动后在浏览器中提供两个页面：
- **配置页** (`/config`) — COM 口选择、MCU 配置、项目初始化
- **仪表盘页** (`/dashboard`) — RTT View、烧录、调试控制、串口、Modbus、SuperWatch


## Tauri 桌面应用（原生窗口）

Tauri v2 将 Vue 3 前端包装为原生桌面应用，内嵌 Python FastAPI sidecar。

### 开发模式

需要两个终端：

```powershell
# 终端 1：启动 Python 后端
python -m mklink serve --port 8765

# 终端 2：启动 Tauri 窗口（自动编译 Rust + 启动 Vite dev server）
cd gui
npx tauri dev
```

开发模式下 Tauri 窗口连接 `http://localhost:8765` 上的 Python 后端。前端热重载通过 Vite dev server (port 5173) 实现。

### 发布构建

```powershell
cd gui

# 1. 打包 Python 后端为 sidecar
pip install pyinstaller
pyinstaller --onefile --name mklink-sidecar --collect-all mklink -p .. ..\mklink\__main__.py
New-Item -ItemType Directory -Force -Path "src-tauri\binaries" | Out-Null
Copy-Item dist\mklink-sidecar.exe "src-tauri\binaries\mklink-sidecar-x86_64-pc-windows-msvc.exe" -Force

# 2. 构建 Tauri 安装包
npx tauri build
```

产物位于 `gui/src-tauri/target/release/bundle/`：
- `msi/` — Windows Installer 包
- `nsis/` — NSIS 安装包


## Dashboard 生命周期

GUI 仪表盘中 RTT / Serial / Modbus / SuperWatch 均以独立子进程启动，通过 iframe 嵌入：

| Dashboard | 端口 | CLI 命令 |
|-----------|------|----------|
| RTT View | 8081 | `mklink rtt --visualize` |
| Serial | 8084 | `mklink serial dashboard` |
| Modbus | 8085 | `mklink modbus dashboard` |
| SuperWatch | 8086 | `mklink superwatch --visualize` |

API 端点：
- `POST /api/dashboard/start` — 启动 Dashboard（body: `{"type": "rtt|serial|modbus|superwatch"}`）
- `POST /api/dashboard/stop` — 停止 Dashboard
- `GET /api/dashboard/status` — 查询所有 Dashboard 运行状态
