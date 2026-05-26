# 安装与可选依赖

> 触发词：pip、ensurepip、readelf、arm-none-eabi、winget
> 返回索引：[SKILL.md](../SKILL.md)

## 安装步骤

在使用本 Skill 之前，必须先安装 `mklink` Python 包：

```bash
# 1. 如果 Python 没有 pip，先引导安装
python -m ensurepip --upgrade

# 2. 从本 Skill 目录安装 mklink 包（ editable 模式）
python -m pip install -e .

# 3. 如果使用 Modbus 功能，确保安装 pymodbus（已在依赖中自动安装）
pip install pymodbus>=3.0
```

安装完成后，`python -m mklink` 命令即可正常使用。


## GNU Arm readelf（符号解析与 AXF 调试）

当用户要执行以下功能时，必须提供 `arm-none-eabi-readelf`：

- `python -m mklink symbols --source <firmware.axf>`
- `python -m mklink vofa <符号名>,... --visualize --source <firmware.axf>`
- `python -m mklink typeinfo --source <firmware.axf> --var <name>`
- `python -m mklink watch` / `superwatch`（使用 `--source <firmware.axf>` 时）
- `python -m mklink hardfault --source <firmware.axf> --sp <stack_pointer>`

先检查依赖是否已经可用：

```powershell
arm-none-eabi-readelf --version
```

若命令不存在，优先使用 winget 安装官方 GNU Arm Embedded Toolchain：

```powershell
winget install --id Arm.GnuArmEmbeddedToolchain -e --accept-package-agreements --accept-source-agreements
```

如果 winget 安装器被 UAC、GUI 或权限问题中断，使用无管理员权限的便携安装方式：

```powershell
$toolsDir = Join-Path $env:USERPROFILE ".local\tools"
$zipPath = Join-Path $toolsDir "arm-gnu-toolchain-14.2.rel1-mingw-w64-i686-arm-none-eabi.zip"
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

curl.exe -L "https://developer.arm.com/-/media/Files/downloads/gnu/14.2.rel1/binrel/arm-gnu-toolchain-14.2.rel1-mingw-w64-i686-arm-none-eabi.zip" -o $zipPath
tar -xf $zipPath -C $toolsDir

# 该 zip 解压后 bin 目录通常位于 $toolsDir\bin
$bin = Join-Path $toolsDir "bin"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $bin) {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
}

# 让当前 PowerShell 立即可用
if (($env:Path -split ";") -notcontains $bin) {
  $env:Path = "$env:Path;$bin"
}
```

如果当前会话或 Python `subprocess.run(["arm-none-eabi-readelf", ...])` 仍找不到命令，可把真实 exe 复制到已在 PATH 中的用户 bin 目录：

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.local\bin" | Out-Null
Copy-Item "$env:USERPROFILE\.local\tools\bin\arm-none-eabi-readelf.exe" "$env:USERPROFILE\.local\bin\arm-none-eabi-readelf.exe" -Force
```

安装后必须验证：

```powershell
arm-none-eabi-readelf --version
python -c "import shutil, subprocess; print(shutil.which('arm-none-eabi-readelf')); subprocess.run(['arm-none-eabi-readelf','--version'], check=True)"
python -m mklink symbols --source path/to/firmware.axf --filter "counter|sensor"
```


## GUI 依赖（Web GUI 与 Tauri 桌面应用）

当用户需要以下功能时，需要安装 GUI 依赖：

- `mklink serve` — 远程调试服务器（REST API + WebSocket JSON-RPC）
- `mklink gui` — 启动 Web GUI（FastAPI 后端 + Vue 3 前端）
- Tauri 桌面应用 — 原生窗口体验

### Python GUI 依赖

先检查是否已安装：

```powershell
python -c "import fastapi, uvicorn; print('GUI deps OK')"
```

若导入失败：

```powershell
pip install -e ".[gui]"
```

### Node.js 依赖

Tauri 桌面应用和 Vue 3 前端需要 Node.js。先检查：

```powershell
node --version
```

若未安装，使用 winget：

```powershell
winget install --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements
```

然后安装前端依赖：

```powershell
cd gui
npm install
```

### Rust 工具链（Tauri 桌面应用）

Tauri v2 桌面应用需要 Rust 编译器。先检查：

```powershell
rustc --version
cargo --version
```

若未安装，分两步：

**步骤 1 — 安装 MSVC Build Tools**（Rust Windows 编译必需）：

```powershell
# 检查是否已有 Visual Studio 或 Build Tools
if (-not (Get-Command cl -ErrorAction SilentlyContinue)) {
    winget install --id Microsoft.VisualStudio.2022.BuildTools -e --accept-package-agreements --accept-source-agreements --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive"
}
```

**步骤 2 — 安装 Rust**：

```powershell
# 下载并静默安装 rustup
$installer = "$env:TEMP\rustup-init.exe"
Invoke-WebRequest -Uri https://win.rustup.rs/x86_64 -OutFile $installer
& $installer -y --default-toolchain stable --default-host x86_64-pc-windows-msvc
Remove-Item $installer -Force

# 刷新当前会话 PATH
$env:Path += ";$env:USERPROFILE\.cargo\bin"
```

验证 Rust 安装：

```powershell
rustc --version
cargo --version
```

### Tauri 桌面应用启动

```powershell
# 开发模式（热重载，需同时手动启动 Python 后端）
cd gui
python -m mklink serve --port 8765 &   # 后端（另一终端）
npx tauri dev                           # Tauri 窗口
```

### Sidecar 打包（发布构建）

发布桌面安装包（MSI/NSIS）前，需将 Python 后端打包为独立可执行文件：

```powershell
pip install pyinstaller

# 打包 Python 后端为 mklink-sidecar.exe
pyinstaller --onefile --name mklink-sidecar --collect-all mklink -p .. mklink\__main__.py

# 将产物放入 Tauri 预期位置
New-Item -ItemType Directory -Force -Path "src-tauri\binaries" | Out-Null
Copy-Item dist\mklink-sidecar.exe "src-tauri\binaries\mklink-sidecar-x86_64-pc-windows-msvc.exe" -Force

# 构建桌面安装包
npx tauri build
```

构建产物位于 `gui/src-tauri/target/release/bundle/`。
