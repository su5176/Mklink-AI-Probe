# 本地 Web 服务与 GUI

> 触发词：serve、gui、FastAPI、uvicorn、Tauri、桌面应用、本地 Web GUI/API
> 返回索引：[SKILL.md](../SKILL.md)

VPN/局域网站点、独立 Site Agent、工程师侧 remote CLI/SDK/MCP 请改读
[直连远程调试](commands-remote.md)；本页只描述本地 FastAPI、GUI 和 Tauri。

## 依赖安装

GUI 与 Tauri 桌面应用依赖安装详见 [references/install.md](install.md) 的「GUI 依赖」章节。


## Web GUI（浏览器模式）

### High-rate binary stream data plane

SystemView, VOFA, RTT, and SuperWatch high-rate payloads are delivered through
`/ws/streams/{name}` as versioned binary frames. REST and SSE remain control or
legacy low-rate paths; clients must not use their copied event arrays for the
high-rate render loop. The browser transfers each `ArrayBuffer` to a Worker,
which owns bounded typed rings and reports transport gaps separately from
backend-reported drops.

Visible charts are decimated to a min/max pixel envelope and scheduled at no
more than 30 FPS. Pausing a chart or hiding the document suppresses render work
without stopping acquisition. Use the short transport gate before release:

```powershell
python -m pytest _maintainer/testing/tests/test_stream_performance.py -q
```

This ten-second local gate targets 10,000 aggregate samples/s and is not MKLink
HIL. For physical rate sweeps, 30-minute zero-drop soaks, packaged-app checks,
and the required evidence fields, follow
[`docs/verification/high-throughput-streams.md`](../docs/verification/high-throughput-streams.md).

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

GUI 启动后在浏览器中提供三个主页面：
- **配置页** (`/config`) — COM 口选择、MCU 配置、项目初始化
- **仪表盘页** (`/dashboard`) — RTT View、烧录、调试控制、串口、Modbus、SuperWatch
- **在线烧录页** (`/online-flash`) — MKLink-only 探针、目标/Pack、HEX/BIN 检查与预览、烧录任务和 SSE 日志

浏览器版“配置 > 文件来源”可直接选择本机 AXF/ELF/OUT 和 MAP 文件。浏览器
不会暴露本机绝对路径，因此前端使用 multipart 将文件上传到本机 Mklink 服务的
受控 `.mklink/uploads/file-sources` 目录，再把服务端路径用于连接和符号解析。
单文件上限为 256 MiB；Tauri 桌面版继续使用原生文件对话框，不经过上传。

### web-entry — U 盘单 HTML 快速启动

`web-entry` 为已经安装完整 Mklink skill/runtime 的电脑注册
`mklink-ai-probe://` 用户级协议。U 盘只需保存一个跨 Windows、macOS、Linux
通用的 HTML 文件：

```bash
python -m mklink web-entry install --html "/path/to/usb/启动 Mklink Web.html"
```

入口会复用现有 Web 服务；只停止自己启动的进程，不改变 `serve`、MCP 或 Tauri
sidecar 的所有权。平台安装位置、权限和故障排查见
[跨平台 U 盘 Web 启动入口](web-entry.md)。


## Tauri 桌面应用（原生窗口）

Tauri v2 将 Vue 3 前端包装为原生桌面应用，内嵌 Python FastAPI sidecar。

Windows 标准 NSIS 安装包采用 per-machine 安装，在安装结束后以非阻断方式检查
当前在线的 MKLink V2/V3/V4，并为完整校验通过的 CDC 接口设置可区分的设备管理器
名称。安装时没有连接下载器时，可在“配置 > 本地设备”使用“修改端口名称”；
“恢复名称”会请求 UAC 并恢复 Windows 驱动默认显示。浏览器版不提供注册表操作。

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

## 在线烧录 API

`/online-flash` 页调用以下 `/api/online-flash` 端点：

| 用途 | 端点 |
|------|------|
| 列出 MKLink 探针 | `GET /probes` |
| 搜索目标 | `GET /targets?q=...&vendor=...&installed=...` |
| Pack 状态/更新索引 | `GET /packs/status`、`POST /packs/index/update` |
| 安装/导入/取消/删除 Pack | `POST /packs/install`、`POST /packs/import`、`POST /packs/cancel`、`DELETE /packs/{pack_id}/{version}` |
| 检查与分页预览固件 | `POST /images/inspect`、`GET /images/{image_id}/preview` |
| 启动/查询/停止任务 | `POST /jobs`、`GET /jobs/active`、`GET /jobs/{job_id}`、`POST /jobs/{job_id}/stop` |
| 重放式任务事件 | `GET /jobs/{job_id}/events?after={sequence}` (SSE) |

Pack 索引、已安装 Pack 和临时上传均位于用户数据根目录，Windows 默认为 `%LOCALAPPDATA%\MKLink\pyocd`；可在启动服务前设置 `MKLINK_PYOCD_HOME` 覆盖。这些缓存不是仓库或 Tauri 发布资源，`.pack`、上传文件和测试产物不应进入 Git。更新索引和下载 Pack 继承服务进程的 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 环境；断网时可用最后一份有效索引和已安装 Pack。

在线烧录会申请 `TARGET_DEBUG` 资源，与 RTT、SystemView、VOFA、SuperWatch 等会话冲突时返回 HTTP 409 及当前 owner/resource；先停止或由用户确认交接冲突会话。`POST /jobs/{job_id}/stop` 只设置协作式取消：运行中的底层操作返回后，任务才进入 `stopped`，执行 disconnect 并释放租约。页面显示“停止中”时不要立即开启新任务或拔除探针。

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

## 资源管理 API

FastAPI 后端维护 `mklink_bridge`、`serial_port`、`modbus_port` 三类资源租约。串口/Modbus dashboard 启动后会登记租约；停止或强制释放时会同时关闭对应后台 manager，避免虚拟串口被占用后无法释放。

注意：REST API 是 GUI/dashboard 的 HTTP 包装层。Agent 或命令行释放本地串口资源时优先使用 CLI，不需要启动 FastAPI：

```powershell
python -m mklink resources status --port COM3
python -m mklink resources release-serial --port COM3
```

常用端点：

- `GET /api/resources/status` — 查询当前资源占用。
- `POST /api/resources/release-serial` — 释放当前 `serial_port` 持有者；用于串口 dashboard 占用虚拟串口时的一键释放。
- `POST /api/resources/release` — 按 owner 或 resource 释放，例如 `{"owner":"user:dashboard:serial"}` 或 `{"resource":"serial_port"}`。
- `POST /api/resources/release-all` — 停止所有已登记 dashboard 并释放全部租约。

示例：

```powershell
curl http://127.0.0.1:8765/api/resources/status
curl -X POST http://127.0.0.1:8765/api/resources/release-serial -H "Content-Type: application/json" -d "{}"
curl -X POST http://127.0.0.1:8765/api/resources/release -H "Content-Type: application/json" -d "{\"resource\":\"serial_port\"}"
```
