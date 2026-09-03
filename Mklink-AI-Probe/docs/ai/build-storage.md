# 构建存储规则

所有 MKLink 构建和测试临时内容集中到一个专用目录。优先使用环境变量
`MKLINK_BUILD_ROOT` 或 `scripts/build_workspace.ps1 -BuildRoot` 显式指定；
未指定时使用 Git 主工作区旁的 `.build`。


`.build/` 已由 Git 忽略，禁止提交或上传 GitHub。工作树通过 Git common
directory 找到主工作区，共享同一目录，不另建缓存。Windows 系统盘
不能用作构建输出或临时目录；不修改系统级 TEMP，也不迁移已安装工具链。

## 目录用途

| 目录 | 用途与保留方式 |
| --- | --- |
| `.build/cache/` | Cargo、pip、npm、PyInstaller、Go、Vite 等可复用缓存；保留复用 |
| `.build/runs/` | 每次命令的临时目录、pytest 环境、PyInstaller 中间文件；结束后清理 |
| `.build/artifacts/` | 待验收构建包；不自动删除，不代表正式发布 |
| `.build/reports/` | 构建日志、磁盘盘点和清理清单；不上传 |

现有 `release/` 的正式发布资产继续保留。源码中的 `gui/dist/` 是交付所需、
已纳入版本管理的 Web runtime，不能作为垃圾删除；普通试构建应指定外部输出，
只有需要同步交付资源时才更新它。`node_modules` 是源工程依赖，不重复安装到
每个临时目录。用户固件、工程配置、上传文件、签名密钥和 SDK 不属于垃圾。

## 统一入口

在源目录 `Mklink-AI-Probe/` 中使用 PowerShell 7：

```powershell
# 显示实际目录；不启动构建
./scripts/build_workspace.ps1 -Action paths

# Python 验证：临时目录、pip 缓存均受管理
./scripts/build_workspace.ps1 -Action run -Executable python -ArgumentList @('-m', 'pytest', '-q')

# GUI 验证
./scripts/build_workspace.ps1 -Action run -WorkingDirectory ./gui -Executable npm -ArgumentList @('test')

# 普通 Web 试构建（不覆盖源码中的交付资源）
./scripts/build_workspace.ps1 -Action run -WorkingDirectory ./gui -Executable npm -ArgumentList @('run', 'build', '--', '--outDir', '../../.build/artifacts/web', '--emptyOutDir')

# 桌面构建：可复用 Cargo 缓存；不创建正式 Release
./scripts/build_workspace.ps1 -Action run -Executable python -ArgumentList @('skills/tauri-gui-builder/scripts/build.py')

# 仅清理临时运行目录，保留缓存、包和报告
./scripts/build_workspace.ps1 -Action clean
```

入口只修改子进程的构建环境，结束后恢复当前进程的环境变量。无论命令成功或
失败，均清理本次临时目录。目录中若有测试创建的联接/符号链接，则保留并明确
报出路径，不能绕过检查强删。异常终止遗留内容用 `-Action clean` 检查处理。
构建入口有互斥锁，不要同时执行共享缓存清理和构建。

`--bundle` 仍需另行签名/发布授权；存储迁移不改变原有门禁。
Site Agent 使用同一入口，并将 `--output` 指向 `.build/artifacts/`。
正式发布也必须明确指定输出目录、核对完整性；不得因为存在文件而宣称验收通过。

## 清理边界

先检查 Git、运行进程、目标绝对路径与目录链接，再删除已完成的测试环境或
可再生成的废弃构建内容。可复用缓存迁入集中目录而不重复保留。重复日志只有
在大小和 SHA-256 完全一致、保留副本位置已记录后才能去重。
不删除唯一的真机记录。权限被拒绝或存在不确定目录链接时，列出路径交给维护者，
不得提权、改 ACL 或使用其他删除手段绕过限制。
