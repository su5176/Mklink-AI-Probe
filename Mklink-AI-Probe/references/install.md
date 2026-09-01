# 安装与更新（使用者）

按用户选择安装桌面应用或 AI Skill，不需要维护仓库或编译 MKLink。

## 选择安装方式

- **桌面应用**：使用[官方发布页](https://github.com/Aladdin-Wang/Mklink-AI-Probe/releases)
  的安装包。Python 后端与 Web 资源已经打包，不要求另装 Python、Node、Rust 或 MSVC。
- **AI Skill / Python CLI / Web GUI**：使用官方发布的完整 Skill ZIP 和对应摘要，
  校验来源与 SHA-256，解压到当前 AI 客户端支持的用户 Skill 目录。不要仅复制
  `SKILL.md`，也不要把整个开发仓库作为用户 Skill 安装；发布包只含运行所需文件。
  选择满足包内 `pyproject.toml` 要求的 Python，后续命令从完整 Skill 根目录执行。
- **远程现场机**：使用独立 Site Agent 包；部署按[直连远程说明](commands-remote.md)。

## 完整 Skill 安装与自检

若 Python 缺少 pip，先执行 `python -m ensurepip --upgrade`。安装时始终使用同一
Python 环境；完整安装包含 Web GUI 与 MCP：

```powershell
python -m pip install -e ".[gui,mcp]"
python -c "import serial, pymodbus, elftools, pycparser, websockets, fastapi, starlette, uvicorn, pyocd, intelhex, multipart, fastmcp, pydantic; print('MKLink dependencies OK')"
python -m mklink web-entry install --quick-launch
```

最后一条会检查依赖和已打包的 `gui/dist`，再注册用户级 URL 启动器和 HTML 入口。
缺依赖则修复对应环境；缺 Web assets 则重新取得完整官方包，不引导用户构建前端。
失败时不能仅复制网页就宣称安装成功。

快速启动页优先写入卷标 `MICROKEEN` 的下载器 U 盘，未检测到时写入用户桌面。
报告自检与文件位置，不必立即打开 GUI。双击入口会等待本地服务健康后打开页面，
详细行为与排障见 [Web 入口](web-entry.md)。

MCP 使用 `python -m mklink mcp`（stdio），客户端按其插件/MCP 配置加载包内
`.mcp.json`。安装 Skill 文本本身不保证客户端已启用 MCP；以实际可调用工具为准。
没有 MCP 时使用 CLI。工具参数以当前 schema 为准，不依赖固定工具数量。

## 更新

首次实际使用时读 MCP `ping.update` 或运行检查脚本，二选一。检查缓存 24 小时，
不占用探针，离线不阻塞调试：

```powershell
python scripts/skill_update.py check --json
```

发现新版本先说明当前版本、最新版本与发布说明；只有用户明确同意后才执行：

```powershell
python scripts/skill_update.py install --yes --json
```

更新器从公开 `updates/latest.json` 获取版本化桌面安装包和 Skill ZIP，校验大小与
SHA-256。桌面应用与本地服务须先关闭，不打断正在进行的设备操作。可选
`--skill-only` / `--app-only` 限定更新范围；更新 Skill 后重启 AI 客户端或开新会话。
Git checkout 不会被覆盖。旧复制式安装没有更新脚本时，重新安装完整官方 Skill 包。

## ELF/AXF 解析后端

MKLink 默认使用内置 `pyelftools`，以下功能不需要用户安装 Keil、GNU Arm 或系统 binutils：

- `symbols`、`typeinfo`、`watch`、`superwatch` 和 VOFA 变量名解析
- `memmap`、函数名断点和符号目录
- HardFault PC/LR 源码行定位
- CLI、MCP、REST API 和桌面上位机的 AXF 重解析

后端选择优先级：命令/API 显式参数、`MKLINK_ELF_BACKEND`、项目
`.mklink/toolchain.json` 的 `elf_backend`，最后默认 `builtin`。

```powershell
python -m mklink symbols --source path/to/firmware.axf
python -m mklink hardfault --source path/to/firmware.axf --sp 0x20001FF0
```

### 可选 GNU 兼容后端

只有用户明确指定 `external` 时，MKLink 才会调用本机 `readelf` / `addr2line`。
仅设置 `MKLINK_READELF`、`MKLINK_ADDR2LINE` 或工具路径不会自动启用 external，
内置解析失败时也不会静默回退。

```powershell
$env:MKLINK_ELF_BACKEND = "external"
$env:MKLINK_READELF = "C:\tools\arm-gnu\bin\arm-none-eabi-readelf.exe"
$env:MKLINK_ADDR2LINE = "C:\tools\arm-gnu\bin\arm-none-eabi-addr2line.exe"

python -m mklink symbols --source path/to/firmware.axf --elf-backend external
```

项目级配置：

```json
{
  "elf_backend": "external",
  "readelf": "C:/tools/arm-gnu/bin/arm-none-eabi-readelf.exe",
  "addr2line": "C:/tools/arm-gnu/bin/arm-none-eabi-addr2line.exe"
}
```

需要安装 GNU Arm 工具链时可执行：

```powershell
winget install --id Arm.GnuArmEmbeddedToolchain -e --accept-package-agreements --accept-source-agreements
```

MCP `ping` 和 REST `/api/health` 会同时报告 `elf_backend`、
`builtin_elf_available`、`external_elf_available`、`readelf_available` 和
`addr2line_available`。后两个字段只描述可选 GNU 后端，不再决定 AXF 功能是否可用。
