# 当前 AI 交接

> 本文件由 `python scripts/ai_memory.py render` 根据 `project-memory.json` 生成。

## 当前断点

- 更新时间：`2026-09-02T13:25:00+08:00`
- 分支：`feature/eternal-chip-gui`
- HEAD：`立芯分支已合入 master 的 SystemView 会话生命周期修复，并保留立芯品牌 GUI、主题、About 对话框和演示入口。`
- 远端 HEAD：`origin/master 与 origin/feature/eternal-chip-gui 均包含经验证的 SystemView 会话修复；立芯分支生产资源由其品牌源码独立重建。`
- 工作树：最终提交与推送后保持干净；测试、浏览器、STCP、Tauri 和 Cargo 产物只保留在忽略或外部构建目录。
- 当前任务：master 的 SystemView 会话生命周期、首会话单次有界恢复、状态可观测性和真机证据已同步到立芯分支；立芯品牌生产资源和分支门禁已独立重建与验证。
- 状态：`eternal-chip-v0.1.9-systemview-session-fixed`

## 里程碑

- **0.1.9 桌面端与 WebGUI** — `complete`。采用 PR #15 的 UF2 固件与 AGENTS.md；修复构建包装器退出码和锁定临时目录清理，稳定 Windows GUI 全量门禁，并同步生产 Web 资源。
- **隐私安全的内存可观测性** — `complete`。从两个备份分支一次性迁移 MCP 私有流、统一观测事件、内存 dump/RTT/SystemView 发布和测试；保留 0.1.9 的 32 位地址、4 KiB 直读、8 区域批写、12 KiB flush 与写后校验边界。
- **PR #15 本地集成门禁** — `complete`。Python、GUI、Go/STCP、Web、Tauri 和 HIL-Infra 只读门禁全部通过；PR #15 已以 merge commit 方式合并到 master。
- **SystemView 会话生命周期修复** — `complete`。分离 duration 与单调时钟 idle watchdog，容忍瞬态读取，按设备连接代次仅为第一个会话提供一次自动恢复；重试时清理解析器、历史、统计、任务与 CPU hint，并在 WebGUI 显示恢复代次、原因和停止错误。
- **立芯分支同步 0.1.9** — `complete`。以 merge commit 持续纳入当前 master 核心运行时；可再生 dist 冲突始终从合并后 Vue 源码重建。立芯品牌、六套主题、About 对话框、演示 mock 与专属布局继续保留。

## 验证证据

- **Python**：最终全量 1805 passed, 1 skipped，耗时 657.28 秒；SystemView 状态机定向回归 15 passed。Windows 默认 GBK 会使发布清单夹具解码 Git UTF-8 内容失败，使用明确的 PYTHONUTF8=1 后定向与全量均通过。
- **GUI 与 Web**：Vitest 59 files / 627 tests 全部通过；生产构建转换 1945 modules。真实 Chromium 连接 STM32F411CE 目标验证 SystemView 持续约 70 秒达到 260744 events、Sync Ready，浏览器零 error/零 warning，停止、断连和进程退出正常。
- **Go/STCP**：master 集成证据中官方 Go 1.25.12 的 go test ./... 已通过；立芯分支复用与当前 go.mod/go.sum/main.go/main_test.go 逐文件同哈希的 x64 DLL，SHA-256=758E428FE9ED6BCBD9489D6DB1990FCE5A18680887137EA3CA6D348C27A77C07。
- **Tauri Release 可执行文件**：Rust 1.95.0、Node 24.15.0 与 Python 依赖检查通过；npx tauri build --no-bundle 成功，16.3 MB EXE SHA-256=86D3F036D828699737610C0AB7A1129D04431F47234236C804A6A2401355D072。
- **HIL-Infra 硬门禁**：contract_check 61 symbols / SHA 0347576fb2dd8c02...；pytest 380 passed；6 个 capmap 静态一致性零错误零豁免；bench-01、bench-gec1900、运行计划和资源映射通过。
- **HIL 运行时与插件准入**：MKLink runtime 拒绝探针与自动化插件评审 11/11 OK；run-verify RV-01..RV-10 全部 OK，run-doctor 零孤儿、结束后无活动锁。本轮未烧录目标、未执行 OTA、未改变供电或发送 CAN。
- **SystemView 真机链路**：原生 CLI 在 COM5、channel 1、RTT 控制块 0x20010d40 上运行 10 秒并持续输出有效事件；最终生产 WebGUI 会话约 70 秒达到 260744 events。既有故障故事中的首会话自动恢复实测曾持续 80 秒达到 165967 events 且零错误。
- **远端同步**：origin/master 与 origin/feature/eternal-chip-gui 均采用非强制推送同步本次修复；未同步 gitee 或 MicroKeen，也未创建标签、Release 或发布资产。

## 架构决策

- UF2 固件文件和 AGENTS.md 采用 PR #15 版本。
- 立芯分支通过双父 merge commit 持续纳入 master 核心运行时；冲突策略固定为保留立芯品牌与主题交互、采用 master 的 SystemView 与其他公共能力，并从合并后 Vue 源码重建 dist。
- 观测数据的公开事件仅包含安全事实，原始内存、RTT 和 SystemView 内容走有界私有流；发布失败降级但不改变成功读取结果。
- SystemView 自动恢复限定为每次 Device 连接的第一个会话最多一次；恢复状态显式暴露，后续会话和持续失败不会进入隐式重试循环。
- 构建、测试、日志和缓存统一位于 MKLINK_BUILD_ROOT 或主工作区忽略的 .build，并经 scripts/build_workspace.ps1 运行。
- 实际烧录、复位、供电、CAN 发送等不可逆或有外部影响的动作必须获得针对本次操作的明确确认。
- 应用 Release、标签、更新签名和探针固件发布彼此独立，不随 PR 合并自动执行。

## 真机环境

- **probe**：HIL-Infra bench-01 将 MKLink V4 命令口映射为 COM5；交接不依赖端口号，使用台架 selector 与互操作锁。
- **target**：本轮使用 ec_s100_watch_V2.6_tony 的 STM32F411CE + FreeRTOS + LVGL 既有固件，通过 RTT 控制块 0x20010d40 验证 SystemView；未重烧目标。
- **permission**：本轮执行 SystemView 启停、读取和一次探针重启以复现冷启动问题；未执行目标烧录、OTA、供电变更或 CAN 激励。后续写入动作仍需针对目标和固件单独确认。

## 下一动作

1. 另行定位探针冷启动后连续复位或停流问题；保持主机侧单次有界恢复，避免用无限重试掩盖探针固件故障。
2. 后续 master 再推进时，继续以语义 merge 同步 feature/eternal-chip-gui，并在立芯品牌表面与 master 新功能上重跑完整门禁。
3. 正式发布另行处理 NSIS、安装验证、Authenticode/更新签名、标签和 Release 资产；不要从本次分支同步自动推断。

## 已知限制

- 本轮没有重新执行物理烧录 HIL；docs/verification/v0.1.9-stm32f103re-release-hil.md 的原始日志未在当前工作区找到，因此只能作为既有叙述证据，不能冒充本轮新证据。
- Tauri 本轮只完成 --no-bundle Release 可执行文件构建；未生成或安装 NSIS，未做仅系统 PATH 的安装后运行验证，也未使用更新签名密钥。
- Web 生产构建仍提示 DashboardView 压缩后约 551.11 KiB，超过 500 KiB 建议阈值，但不影响构建成功。
- 探针冷启动后曾出现每次 SystemView start 都复位或停流的独立固件状态，增加到多次主机重试仍不能恢复；本次主机修复保持一次有界恢复，不掩盖该固件问题。经原生 CLI 正向控制后，同一目标的最终生产 WebGUI 长流验证通过。
- 当前机器没有可调用的 Go 工具链；STCP 源码未改且复用 DLL 与其输入源码哈希一致，若需要新的 Go 证据应安装工具链后重跑 go test ./... 并重建 DLL。
- 当前 GitHub 身份 su5176 对 MicroKeen/Mklink-AI-Probe 没有写权限；本轮按用户指定只同步 origin，不尝试覆盖或新建 MicroKeen 分支。
- HIL relay 插件仍有既有 runtime/拒绝探针证据缺口；不影响本次 MKLink 插件 11/11 自动化准入。

## 延续协议

- 开始前校正 Git、台架 selector、目标固件和运行进程；硬件操作保持串行并遵守 HIL 锁。
- 不把环境失败、旧叙述证据或未覆盖场景写成 PASS；关键证据写验证报告，交接只保留结论。
- 结束前更新 project-memory.json、渲染 CURRENT_HANDOFF.md，并保持工作树与目标远端分支同步。
