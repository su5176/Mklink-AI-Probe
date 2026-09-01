# Windows USB 串口命名

仅在用户需要辨认、修改或恢复设备管理器名称时读取。


- Windows 桌面安装包以管理员权限安装，并在安装结束后对当时在线的 MKLink
  执行同一套严格身份校验和端口命名。未连接设备、枚举失败或改名失败不得阻断
  主程序安装；用户可以稍后在“配置 > 本地设备”中操作。
- Tauri 桌面版“本地设备”提供“修改端口名称”和“恢复名称”按钮。按钮会明确
  说明操作并请求 UAC；恢复仅清除脚本写入且仍精确匹配目标格式的 FriendlyName，
  让 Windows 回到驱动默认显示，不修改未被 MKLink 命名的设备。
- 本地 Windows 首次实际使用 MKLink 时，AI 可以自动执行只读检查：
  `pwsh -NoProfile -File <skill-root>/scripts/win_usb_rename.ps1 -Json`。
  默认模式不得修改注册表，也不需要管理员权限。
- 脚本只接受当前在线且整组身份一致的 MKLink：精确匹配
  `VID_0D28/PID_0202`、复合设备父节点产品与序列号、ContainerId、Parent、MI 和
  固件上报接口描述。V2/V3 必须有 `MI_02/MI_04`，V4 必须另有 `MI_06`。
- 预览有变更时，向用户展示 `CurrentName -> TargetName` 映射并取得本次明确确认。
  未确认不得运行 `-Apply`，也不得把用户先前对其他操作的授权视为重命名授权。
- 确认后在管理员 PowerShell 中追加 `-Apply`；恢复使用 `-Restore`。脚本会在
  写入前重新枚举设备、先
  备份全部目标注册表项，再设置 FriendlyName 并回读验证；UAC 被拒绝或设备状态
  变化时停止，不得绕过检查。
