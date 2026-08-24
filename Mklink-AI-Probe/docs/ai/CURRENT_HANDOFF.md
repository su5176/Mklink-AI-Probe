# 当前 AI 交接

> 本文件由 `python scripts/ai_memory.py render` 根据 `project-memory.json` 生成。

## 当前断点

- 更新时间：`2026-08-23T21:17:00+08:00`
- 分支：`master`
- HEAD：`feature/hil-plugin-v02 基于 master fbaac61，新增独立 hil-plugin-json-v1 one-shot 入口；合并后 master 将成为 HIL-Infra 首个 v0.2 自动化准入插件实现。`
- 远端 HEAD：`用户已授权将 master 与 feature/eternal-chip-gui 原子推送到 GitHub origin；两条远端分支与本地验收头同步，未创建标签或 Release。`
- 工作树：功能在隔离 worktree 实现和验证；只提交 mklink/hil_plugin.py、对应测试与项目记忆，DLL、pytest/GUI/Rust 构建缓存和 Vite 哈希产物均不纳入 Git。
- 当前任务：为 HIL-Infra v0.2 增加最小无人值守插件面：lifecycle + observe/debug.read；有人值守既有 MCP/CLI 能力不变，所有 flash/reset/write/control 在协议入口先于设备访问拒绝。
- 状态：`hil_plugin_v02_qualified_local`

## 里程碑

- **产品与分发** — `complete`。Python、Skill、Web GUI 和 Tauri 版本统一为 v0.1.6，标准 NSIS 使用内置 sidecar。
- **烧录与兼容性** — `complete`。在线/脱机烧录、HPM ROM API、Pack/FLM、固件刷新、复合探针和扇区解析已集成。
- **调试与数据流** — `complete`。Memory、RTT、SystemView、VOFA、串口和 Modbus 共用资源仲裁与轻量流通道。
- **多实例与远程** — `complete`。每个桌面实例使用独立后端和探针连接；Site Agent 支持认证直连与 LAN STCP。

## 验证证据

- **HIL-Infra v0.2 插件准入**：真实 MicroKeenV4 上完成 identify/capabilities/health/safe_state、双向 transport 锁互操作和无残留 lease；HIL plugin-review PR-01~11 为 11 OK、0 WARN、0 FAIL，runtime conformance 0 error/0 warning/0 waiver。插件单测 5 passed；补入与 master 本机原生源码制品 SHA-256 758E428F...77C07 的未跟踪 mklink-stcp.dll 并把 pytest basetemp 移到源码树外后，Python 全量 1305 passed、1 skipped；GUI 首轮 2 个套件级 mock 抖动，目标文件 67/67 通过后全量复跑 54 文件/525 项通过；Vite 生产构建及 cargo check 通过。未执行烧录、复位、供电、DUT 写入或激励。
- **僵尸锁与探针电源/重启控制**：Windows PID 判定现同时检查 OpenProcess 与 GetExitCodeProcess；真实已退出子进程仍保留句柄时正确返回死亡，serial_MKLINK_AUTO_CONNECT.lock 回归可自动删除。Device/MCP/REST/GUI 新增 1800/3300/5000 mV 与 probe reboot；5V 在 Device、REST、MCP、GUI 四层要求逐次显式确认，reboot 后释放串口与 HIL 锁，活动 RTT/SystemView 在参数校验通过后先安全停止。最终 Python 1300 passed、1 skipped；GUI 54 文件/525 项、Vite 生产构建、Tauri cargo check 通过。真实 Playwright 验证 3.3V 请求 confirm_5v=false、取消 5V 不发请求、确认 5V 才发送 confirm_5v=true、reboot 需确认。浏览器使用模拟后端，未对硬件输出电压；项目无已确认 bench.yaml，维护者于 2026-08-16 明确豁免本次真机门禁，不得把该豁免表述为真机验证通过。
- **双分支本地合并与隔离**：master 与 feature/eternal-chip-gui 均完成本地快进。立芯分支运行时 c9fc938 通过 Python 全量覆盖（首轮 1297 passed、1 skipped，PyPI 恢复后受影响文件 7/7 通过）、GUI 56 文件/529 项、生产构建、cargo check 和真实 Chromium + mock 验收。两分支 mklink 核心、Skill 与探针控制回归测试无差异；HIL 锁提交分别位于两条祖先链，立芯品牌提交 7ba8f57 不是 master 祖先。用户随后明确授权，两条分支通过 Git 原子推送同步到 GitHub origin。
- **FLM 查找修复选择性合并**：从 origin/master 8f6a094 创建 fix/pdsc-device-algorithm，仅摘取原提交 68d4e4f 为 8da2d2d；差异只有 mklink/mcu_detect.py 与 mklink/mcu_profiles.json，无 GUI 或 HIL 锁文件。真实 D:\Keil_v5\ARM\PACK 中 Keil.STM32F4xx_DFP.pdsc 对 STM32F411CEUx/RETx 均命中 device 级 CMSIS/Flash/STM32F4xx_512.FLM。Python 全量先得 1282 passed、1 skipped，环境性失败随后逐项联网复跑通过；GUI 54 文件/521 项、Vite 生产构建、Tauri cargo check 均通过。Site Agent 打包补入与当前 stcp_bridge 源码哈希一致的本地 DLL 后 3 项通过，DLL 与测试产物均不提交。
- **v0.1.6 运行时**：GUI 518 项、配置页 22 项和连接后端 12 项通过；Python 可比门禁 1262 项通过、1 项跳过。真实 Chrome、独立 Web 后端、下载器和 HPM5301 完成自动搜索、错误端口回退和再次连接闭环。
- **烧录与数据流**：HPM 在线烧录自动运行、重复固件加载、浏览器文件刷新和客户 HEX 解析已验证；串口/RTT 高吞吐与下载器 V2/V3/V4 数据完整性完成真机验证。
- **v0.1.6 正式分发**：七项资产哈希复算通过；正式 NSIS 已覆盖安装，健康与探针接口、内置 sidecar、零 Python 子进程、正常退出和动态端口释放通过。本地 Skill 指向 2f65f92c98；GitHub/Gitee Release、标签和 updates/latest.json 已核对一致。
- **浏览器后端生命周期**：真实 Chrome 双标签验证：关闭一个标签时后端继续运行；关闭最后标签后约 3 秒正常退出，8765 可立即重绑定。关闭前下载器保持连接，随后新后端能重新连接同一下载器，确认 CMD 串口和 Device 已释放。GUI 521 项、生产构建和 Tauri cargo check 通过；Python 1274 项通过、1 项跳过，12 项仅因 Windows 缺少符号链接权限失败。
- **源码与本地 Skill 同步**：Aladdin-Wang GitHub/Gitee master 与 su5176 PR #10 已同步；用户级 Skill、完整 GUI/MCP 依赖导入和 Skill 校验通过，快速启动网页已写入当前 MICROKEEN 卷。su5176 PR #10 于 2026-08-12 合并为 2f8e902。
- **PR #10 合并门禁复核**：GitHub 合并前状态 CLEAN、MERGEABLE，头 4d7d617 相对基线 6360843 前进 35 个提交且未落后；仓库未配置远端状态检查。本机隔离复核通过 GUI 54 文件/521 项、Vite 生产构建、Tauri Rust 12 项与 cargo check。首次 Python 全量得到 1284 passed、1 skipped；3 项仅因隔离 worktree 缺少未入库 mklink-stcp.dll 报错，在补入与 PR 完全相同 stcp_bridge 源码树生成且 SHA-256 一致的 DLL 后定向 3 项全通过。旧 Device 连接测试仍 mock 已移除的 _resolve_port，已改为 mock 当前 load_config 入口以消除本地项目配置依赖；修正后的最终 Python 全量为 1288 passed、1 skipped。PR 中既有真实 Chrome 双标签、下载器重连和 HPM/串口/RTT 真机闭环继续作为实机证据。

## 架构决策

- HIL v0.2 自动化最小面只开放 lifecycle 与 observe/debug.read；CAPABILITIES 可报告物理全能力，但 automation_verbs 只有 debug.read，未开放动作在解析 transport 前 fail-closed。
- safe_state 对 one-shot probe 只证明真实设备身份仍可枚举、互操作 transport 锁可获取并已释放、无持续输出；不声称 DUT 已复位、断电或恢复业务状态。
- Windows 串口锁 owner 只有在进程退出码为 STILL_ACTIVE 时才判定存活；访问拒绝或查询失败保持保守，不自动删除可能属于活动进程的锁。
- VCC 只接受 1800/3300/5000 mV；5000 mV 每次都需显式 confirm_5v=True，GUI 另有危险确认；reset 复位目标 MCU，reboot_probe 重启探针并断开会话。
- 历史端口是软偏好，可回退自动发现；当前会话手选端口首次保持严格约束，失败后切回自动搜索。
- 运行时修改在 fix/feature 分支完成全量测试、生产构建、项目记忆和真机闭环后合并。
- HPM 目标只使用 ROM API；ELF/DWARF 默认使用内置 pyelftools。
- Dashboard 与烧录/调试操作共用资源租约；复位前停止 RTT、SuperWatch、VOFA 和 SystemView。
- 串口和 RTT 终端使用独立 Worker 与有界缓冲；终端模式不维护隐藏日志。
- 每个 Tauri 实例拥有独立 sidecar、动态端口和探针锁；正式发布默认只生成标准 NSIS。
- 由命令主动打开的浏览器 GUI 使用标签页会话租约；最后标签消失后正常关闭后端并释放资源，显式 --no-browser 和 Tauri sidecar 保持常驻。

## 真机环境

- **probe**：维护机可使用 V2/V3/V4 下载器；交接不记录端口或完整设备标识。
- **target**：ARM 与 HPM 真机可用；部分客户芯片仅完成 Pack/HEX 软件验证。
- **permission**：维护者已授权本次 HIL v0.2 插件实现、只读真机识别/健康/debug.read/safe_state、隔离分支提交与本地合并；不授权烧录、擦除、复位、供电、DUT 写入或激励。本次不创建标签、Release，不推送 GitHub/Gitee。历史 VCC/reboot 豁免仍不授权任何 5V 实机输出。

## 下一动作

1. 监控 v0.1.6 用户反馈，运行时修复从新的 fix/feature 分支开始。
2. 下次正式发布前修正发布器的默认 GitHub/Gitee 仓库参数。
3. 需要扩大分发证据时，在干净 Windows 环境复测安装更新和 USB Web Entry。

## 已知限制

- 无人值守面首期没有开放 program/console/bus 写入，也没有为 RTT/UART 只读动作准入；扩大 allowed verbs 必须重新走 HIL plugin-review 和真机安全评审。
- VCC 与探针 reboot 无本次提交对应的真机 HIL 证据；维护者已明确豁免，真实浏览器仅验证了受保护的请求路径。
- 高事件率 SystemView 仍可能溢出目标 RTT 缓冲。
- V4 脱机首次触发的瞬时空失败仍需冷启动复现。
- 部分客户芯片修复缺少物理目标板编程证据。
- 先楫定制店铺尚无权威链接，菜单项保持禁用。
- USB Web Entry 和安装更新仍需更多平台与干净 Windows 验证。
- 发布器默认仓库参数仍含旧备用名；下次发布前应修正或继续显式传入两端 Aladdin-Wang 仓库。

## 延续协议

- 开始前用 Git、进程和硬件状态校正项目记忆。
- 不提交安装包、日志、硬件标识、凭据或构建缓存。
- 结束前执行 render、validate、diff 检查并保持工作树干净。
