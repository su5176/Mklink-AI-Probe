# MKLink 源码开发（仅维护者）

本页只用于修改 MKLink 本体，不属于用户 Skill 的安装或调试流程。
维护规则以源目录的 `AGENTS.md` 为准，普通用户不需要读取本页。

## 环境与入口

- Python 后端依赖由 `pyproject.toml` 定义；开发测试依赖按实际检查安装。
- 前端源码位于 `gui`，开发需要 Node/npm；Tauri Windows 构建还需要
  Rust、MSVC Build Tools 和 PyInstaller。已经安装的桌面应用不需要这些工具。
- 开始桌面构建前，按 `skills/tauri-gui-builder/SKILL.md` 使用现有
  `build.py --check` 检查工具，不复制用户安装步骤中的临时打包命令。
- 所有构建/测试均使用 `scripts/build_workspace.ps1`；具体缓存、输出
  和清理约定见 [build-storage.md](build-storage.md)。

## 常用验证（从源目录运行）

```powershell
./scripts/build_workspace.ps1 -Action run -Executable python -ArgumentList @('-m', 'pytest', '-q')
./scripts/build_workspace.ps1 -Action run -WorkingDirectory ./gui -Executable npm -ArgumentList @('test')
./scripts/build_workspace.ps1 -Action run -WorkingDirectory ./gui -Executable npm -ArgumentList @('run', 'build')
```

最后一条会更新受 Git 管理的 `gui/dist`，只在需要更新交付资源时执行。
普通试构建的独立输出示例见构建存储说明。

## 按影响范围验收

- Web：真实浏览器加载生产资源，验证页面行为、网络请求和会话释放。
- 安装器：验证实际安装/升级、快捷方式、用户配置、后端启动和退出；
  隔离夹具只能作快速回归，不能代替实际安装包闭环。
- 设备：验证真实探针和目标，不把模拟接口当作硬件成功。
- 高速数据流：短回归使用 `test_stream_performance.py`，验收场景见
  [high-throughput-streams.md](../verification/high-throughput-streams.md)。

桌面构建使用维护者 builder；默认标准 NSIS，不使用 ad hoc PyInstaller
命令、不默认生成 MSI。正式发布只在获得明确授权后读取
`skills/maintaining-mklink-ai-probe/references/releasing.md`。
