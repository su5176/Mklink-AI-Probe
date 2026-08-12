# 当前 AI 交接

> 本文件由 `python scripts/ai_memory.py render` 根据 `project-memory.json` 生成。

## 当前断点

- 更新时间：`2026-08-12T11:53:00+08:00`
- 分支：`master`
- HEAD：`master 已包含 06bbd7a 的 GitHub 优先更新检查、Gitee 回退和发布器官方仓库默认值修复。`
- 远端 HEAD：`Aladdin-Wang GitHub master 已包含 06bbd7a；本次未发布新标签、Release 或 updates/latest.json。`
- 工作树：GitHub 优先更新检查修复已快进合并并推送，正在完成最终交接。
- 当前任务：GitHub 优先、Gitee 回退的 24 小时更新检查与发布器默认仓库修复已合并并推送官方 GitHub master。
- 状态：`github_first_update_check_pushed`

## 里程碑

- **产品与分发** — `complete`。Python、Skill、Web GUI 和 Tauri 版本统一为 v0.1.6，标准 NSIS 使用内置 sidecar。
- **烧录与兼容性** — `complete`。在线/脱机烧录、HPM ROM API、Pack/FLM、固件刷新、复合探针和扇区解析已集成。
- **调试与数据流** — `complete`。Memory、RTT、SystemView、VOFA、串口和 Modbus 共用资源仲裁与轻量流通道。
- **多实例与远程** — `complete`。每个桌面实例使用独立后端和探针连接；Site Agent 支持认证直连与 LAN STCP。

## 验证证据

- **v0.1.6 运行时**：GUI 518 项、配置页 22 项和连接后端 12 项通过；Python 可比门禁 1262 项通过、1 项跳过。真实 Chrome、独立 Web 后端、下载器和 HPM5301 完成自动搜索、错误端口回退和再次连接闭环。
- **烧录与数据流**：HPM 在线烧录自动运行、重复固件加载、浏览器文件刷新和客户 HEX 解析已验证；串口/RTT 高吞吐与下载器 V2/V3/V4 数据完整性完成真机验证。
- **v0.1.6 正式分发**：七项资产哈希复算通过；正式 NSIS 已覆盖安装，健康与探针接口、内置 sidecar、零 Python 子进程、正常退出和动态端口释放通过。本地 Skill 指向 2f65f92c98；GitHub/Gitee Release、标签和 updates/latest.json 已核对一致。
- **浏览器后端生命周期**：真实 Chrome 双标签验证：关闭一个标签时后端继续运行；关闭最后标签后约 3 秒正常退出，8765 可立即重绑定。关闭前下载器保持连接，随后新后端能重新连接同一下载器，确认 CMD 串口和 Device 已释放。GUI 521 项、生产构建和 Tauri cargo check 通过；Python 1274 项通过、1 项跳过，12 项仅因 Windows 缺少符号链接权限失败。
- **源码与本地 Skill 同步**：Aladdin-Wang GitHub/Gitee master 与 su5176 PR #10 已同步；用户级 Skill、完整 GUI/MCP 依赖导入和 Skill 校验通过，快速启动网页已写入当前 MICROKEEN 卷。
- **GitHub 优先更新检查**：Skill 更新器与运行时/MCP 检查保持 24 小时缓存，默认先读取官方 GitHub updates/latest.json，失败才回退 Gitee；回归测试覆盖优先级、回退和发布器官方仓库默认值。聚焦测试 25 项通过，GUI 521 项通过、生产构建通过；真实强制检查从 GitHub 返回 v0.1.6。Python 全量为 1279 通过、1 跳过，12 项仅因 Windows 账户缺少目录符号链接特权失败。

## 架构决策

- 历史端口是软偏好，可回退自动发现；当前会话手选端口首次保持严格约束，失败后切回自动搜索。
- 运行时修改在 fix/feature 分支完成全量测试、生产构建、项目记忆和真机闭环后合并。
- HPM 目标只使用 ROM API；ELF/DWARF 默认使用内置 pyelftools。
- Dashboard 与烧录/调试操作共用资源租约；复位前停止 RTT、SuperWatch、VOFA 和 SystemView。
- 串口和 RTT 终端使用独立 Worker 与有界缓冲；终端模式不维护隐藏日志。
- 每个 Tauri 实例拥有独立 sidecar、动态端口和探针锁；正式发布默认只生成标准 NSIS。
- 由命令主动打开的浏览器 GUI 使用标签页会话租约；最后标签消失后正常关闭后端并释放资源，显式 --no-browser 和 Tauri sidecar 保持常驻。
- Skill 更新器和运行时/MCP 更新检查保持 24 小时缓存，优先官方 GitHub 更新清单，只有 GitHub 不可用时回退 Gitee。

## 真机环境

- **probe**：维护机可使用 V2/V3/V4 下载器；交接不记录端口或完整设备标识。
- **target**：ARM 与 HPM 真机可用；部分客户芯片仅完成 Pack/HEX 软件验证。
- **permission**：维护者已明确授权本次 v0.1.6 GitHub/Gitee 正式发布；破坏性烧录仍需单独授权。

## 下一动作

1. 下个正式版本从当前 master 构建并发布后，公共 Skill ZIP 与 updates/latest.json 才会向其他用户分发 GitHub 优先逻辑。
2. 监控 v0.1.6 用户反馈，运行时修复从新的 fix/feature 分支开始。
3. 需要扩大分发证据时，在具备符号链接权限的干净 Windows 环境复测安装更新和 USB Web Entry。

## 已知限制

- 高事件率 SystemView 仍可能溢出目标 RTT 缓冲。
- V4 脱机首次触发的瞬时空失败仍需冷启动复现。
- 部分客户芯片修复缺少物理目标板编程证据。
- 先楫定制店铺尚无权威链接，菜单项保持禁用。
- USB Web Entry 和安装更新仍需更多平台与干净 Windows 验证。
- 当前 Windows 测试账户不能创建目录符号链接，因此完整 Python 套件中的 12 个上传路径重定向安全测试无法在本机建立前置条件。

## 延续协议

- 开始前用 Git、进程和硬件状态校正项目记忆。
- 不提交安装包、日志、硬件标识、凭据或构建缓存。
- 结束前执行 render、validate、diff 检查并保持工作树干净。
