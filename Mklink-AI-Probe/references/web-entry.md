# 跨平台 U 盘 Web 启动入口

> 触发词：U 盘 HTML、快速启动 Web、web-entry、自定义 URL 协议、mklink-ai-probe://
> 返回索引：[SKILL.md](../SKILL.md)

## 目标

U 盘或用户桌面只保存一个普通 HTML 文件。用户双击后，页面短暂倒计时并自动
调用电脑上已安装的 Mklink skill/runtime；本地服务健康检查通过后，启动器自动
打开真正的 Web GUI。HTML 在 Windows、macOS 和 Linux 上字节一致，不包含
可执行程序，也不依赖 U 盘盘符。

统一协议：

```text
mklink-ai-probe://web/start
mklink-ai-probe://web/open
mklink-ai-probe://web/stop
```

## 首次安装

每台电脑只需准备一次，U 盘不需要按系统制作不同文件。完整 skill/runtime
必须包含 Python 3.9+、GUI 依赖和已构建的 `gui/dist`；只复制 `SKILL.md`
说明文件不能启动本机进程：

```bash
python -m pip install -e ".[gui]"
python -m mklink web-entry install
```

部署人员应在安装或更新 Mklink runtime 时执行：

```bash
python -m mklink web-entry install --quick-launch
```

该命令先验证核心、Web GUI、MCP 依赖和 `gui/dist`，然后注册协议并自动生成
`MKLink Web GUI.html`。发现卷标为 `MICROKEEN` 的下载器 U 盘时优先写入该盘；
没有下载器时写入当前用户桌面。完成后，普通用户只需双击网页，不再需要 AI、
终端或管理员权限。

协议按当前用户安装，不要求管理员权限：

- Windows：`HKCU\Software\Classes\mklink-ai-probe`
- macOS：`~/Applications/Mklink AI Probe Web Launcher.app`
- Linux：`~/.local/share/applications/mklink-ai-probe-web.desktop`

Windows 协议处理器仍由完整 Skill/runtime 的 Python Web handler 执行；注册时同时
写入 `MKLink Web GUI` 友好名称，浏览器授权框不应显示成“打开 Python”。不要把
协议改绑到 Windows 桌面上位机：U 盘入口必须继续支持未安装桌面程序的 Windows、
macOS 和 Linux 用户。

skill/runtime 更新或安装目录变化后重新执行 `web-entry install`，使协议处理器指向新的绝对路径。

同一份 HTML 在三个系统上的本机准备如下：

| 系统 | 一次性本机准备 | U 盘内容 |
|------|----------------|----------|
| Windows | 安装完整 Mklink runtime，执行 `web-entry install` | `启动 Mklink Web.html` |
| macOS | 安装对应架构 runtime，执行 `web-entry install` | 同一文件 |
| Linux | 安装 runtime、设备权限和 `xdg-utils`，执行 `web-entry install` | 同一文件 |

## 生成 U 盘 HTML

```bash
python -m mklink web-entry html --output "/path/to/usb/启动 Mklink Web.html"
```

也可以安装协议并同时生成：

```bash
python -m mklink web-entry install --html "/path/to/usb/启动 Mklink Web.html"
```

生成结果是一个无外部 CSS、JavaScript、图片或网络链接依赖的 HTML。图标以内嵌 data URI 保存。

页面打开后显示 3 秒自动启动倒计时，也保留“立即启动 Web GUI”按钮。触发后页面
显示最长 50 秒的启动进度；启动器在其中最多等待 45 秒，本地服务健康检查通过后
自动打开 Web GUI。若仍未打开，页面会提示检查浏览器协议授权，并让用户请 AI 从
官方仓库安装或更新完整 Skill、检查 Web GUI 与 MCP 依赖。

Skill 更新或安装位置变化后，应重新执行 `web-entry install`，使协议处理器继续
指向当前 Skill；U 盘 HTML 会常驻显示这项提醒。

## 服务生命周期

```bash
python -m mklink web-entry start
python -m mklink web-entry status
python -m mklink web-entry stop
python -m mklink web-entry uninstall
```

安全边界：

- 服务仅绑定 `127.0.0.1`。
- URI 只接受 `start/open/stop`，不接受命令、路径或查询参数。
- 若端口上已有带 Web 资源的 Mklink 服务，入口直接复用且不取得所有权。
- `stop` 只终止 `web-entry` 自己启动并记录为 `owned=true` 的进程。
- 所有权同时校验 PID 和进程创建身份，陈旧状态不会误停复用后的其他进程。
- 连续点击由用户级操作锁串行处理，不会同时拉起两个后端。
- 新服务在 45 秒内未就绪时，入口终止自己启动的进程、清除状态并显示系统错误；
  快速启动页在 50 秒后显示对应排障提醒。
- 若发现正在运行的 Mklink API 没有 Web 静态资源，入口报错并停止，不启动第二个竞争硬件的后端。
- 现有 `mklink gui`、`mklink serve`、`mklink mcp` 和 Tauri sidecar 生命周期不变。
- 协议安装、HTML 文件和状态目录均为新增旁路，不修改 AI/MCP 配置或 Tauri 启动逻辑。

## 平台要求

### Windows

- Windows 10/11。
- Edge、Chrome 或其他支持自定义协议的浏览器。
- 首次点击时确认“打开 MKLink Web GUI”；若仍显示“打开 Python”，重新执行
  `python -m mklink web-entry install` 刷新当前用户的协议元数据。

### macOS

- Intel 或 Apple Silicon 对应的 Python/runtime。
- 浏览器首次调用时确认打开用户 Applications 中的 Handler。
- 正式分发时应对 Handler 和 runtime 签名、公证。

### Linux

- 带默认浏览器的桌面会话。
- 建议安装 `xdg-utils`；协议通过 `xdg-mime` 注册。
- 用户需具备 USB HID/串口权限，必要时安装 udev 规则或加入 `dialout` 组。

## 故障排查

- 浏览器提示未知协议：重新执行 `python -m mklink web-entry install`。
- 提示缺少 Web assets：确认 skill/runtime 包含构建后的 `gui/dist/index.html`。
- 提示已有 API 但无 Web assets：关闭对应桌面 sidecar/API，或使用原 Windows 上位机。
- HTML 点击无反应：检查浏览器是否拦截外部协议，并确认 Handler 的 Python/runtime 路径仍存在。
- 页面启动超时：请让 AI 从官方仓库安装或更新完整 MKLink AI Probe Skill，并检查
  Web GUI 与 MCP 依赖，然后重新执行 `web-entry install --quick-launch`。
