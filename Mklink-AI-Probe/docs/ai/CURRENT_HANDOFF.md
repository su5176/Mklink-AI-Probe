# 当前 AI 交接

> 本文件由 `python scripts/ai_memory.py render` 根据 `project-memory.json` 生成。

## 当前断点

- 更新时间：`2026-09-01T14:01:42+08:00`
- 分支：`codex/v0.1.9-upstream-pr`
- HEAD：`PR #15 plus build-gate fixes, synchronized Web assets, and privacy-safe memory observability are locally validated.`
- 远端 HEAD：`origin/pr/15 still points to the original PR head until the validated integration branch is pushed.`
- 工作树：Keep the checkout clean; build output belongs in MKLINK_BUILD_ROOT or the ignored .build directory.
- 当前任务：完成 PR #15 修补、两个本地备份中的内存可观测性语义迁移、全量软件门禁与 HIL-Infra 只读门禁。
- 状态：`v0.1.9-pr15-integrated-and-validated`

## 里程碑

- **0.1.9 桌面端与 WebGUI** — `complete`。采用 PR #15 的 UF2 固件与 AGENTS.md；修复构建包装器退出码和锁定临时目录清理，稳定 Windows GUI 全量门禁，并同步生产 Web 资源。
- **隐私安全的内存可观测性** — `complete`。从两个备份分支一次性迁移 MCP 私有流、统一观测事件、内存 dump/RTT/SystemView 发布和测试；保留 0.1.9 的 32 位地址、4 KiB 直读、8 区域批写、12 KiB flush 与写后校验边界。
- **PR #15 本地集成门禁** — `complete`。Python、GUI、Go/STCP、Web、Tauri 和 HIL-Infra 只读门禁全部通过；下一步仅为推送 PR 分支并按授权完成远端合并。

## 验证证据

- **Python**：最终全量 1790 passed, 1 skipped，耗时 556.10 秒；观测与原 0.1.9 边界联合定向回归 59 passed。
- **GUI 与 Web**：Vitest 59 files / 626 tests 全部通过；生产构建转换 1945 modules。真实 Chromium 验证配置、仪表盘、脱机烧录、在线烧录路由与版本弹层；无静态资源或 Vue 运行时错误。
- **Go/STCP**：官方 Go 1.25.12 工具链下 go test ./... 通过；当前源码 DLL SHA-256=C49908A030D16629253683C7B3B4837673863081E4602D8A1F25B54AA0B79087。
- **Tauri Release 可执行文件**：Rust 1.95.0、Node 24.15.0 与 Python 依赖检查通过；npx tauri build --no-bundle 成功，16.3 MB EXE SHA-256=138647E4DE52BF8E85A4AAC43CBE39A44AF172250ACA47E3F7258B2CC47E661D。
- **HIL-Infra 硬门禁**：contract_check 61 symbols / SHA 0347576fb2dd8c02...；pytest 380 passed；6 个 capmap 静态一致性零错误零豁免；bench-01、bench-gec1900、运行计划和资源映射通过。
- **HIL 运行时与插件准入**：MKLink、Toomoss、PSU、Camera 的负向运行时拒绝探针通过；MKLink 自动化插件评审 11/11 OK，Camera 11/11 OK；结束后无活动锁。本轮未烧录、复位、供电或发送 CAN。
- **PR #15 固件资产**：按用户决策采用 PR 文件：HPMLink V4.3.8 SHA-256=D295858F3914998ABB7A19F6064157F6695C5B43E362CB00553D7A295A7E3FA8；MicroLink V3.3.8 SHA-256=E213B248A137C6519A9E926EAAC78718CE844C2A20555F3A3D2A48581B099BE8；MicroLink V4.3.9 SHA-256=F29821A6A34B75F3DAE96416595CA572A2DACAAF54C9DCC17521845F212DCEF6。

## 架构决策

- UF2 固件文件和 AGENTS.md 采用 PR #15 版本。
- 两个本地备份包含同一份核心内存可观测性实现；只迁移一次代码，旧交接文档不覆盖当前 0.1.9 记忆，Eternal Chip GUI 分支历史不混入 PR。
- 观测数据的公开事件仅包含安全事实，原始内存、RTT 和 SystemView 内容走有界私有流；发布失败降级但不改变成功读取结果。
- 构建、测试、日志和缓存统一位于 MKLINK_BUILD_ROOT 或主工作区忽略的 .build，并经 scripts/build_workspace.ps1 运行。
- 实际烧录、复位、供电、CAN 发送等不可逆或有外部影响的动作必须获得针对本次操作的明确确认。
- 应用 Release、标签、更新签名和探针固件发布彼此独立，不随 PR 合并自动执行。

## 真机环境

- **probe**：HIL-Infra bench-01 将 MKLink V4 命令口映射为 COM5；交接不依赖端口号，使用台架 selector 与互操作锁。
- **target**：已有 v0.1.9 STM32F103RE 真机报告来自较早源码基线；本轮变更集中在构建门禁、Web 产物与主机侧可观测性，没有重烧目标。
- **permission**：本轮只做 identify/capabilities/health/safe_state 与负向拒绝探针。若要重跑物理烧录闭环，需要用户明确授权目标、固件和写入动作。

## 下一动作

1. 将已验证分支推送到 PR #15 头分支，确认远端提交一致后按授权合并到 master。
2. 若需要重新证明目标侧行为，先获得明确烧录授权，再通过 hil-orchestrator 执行编译、烧录、激励、观测、判定和证据归档闭环。
3. 正式发布另行处理 NSIS、安装验证、Authenticode/更新签名、标签和 Release 资产；不要从本次 PR 合并自动推断。

## 已知限制

- 本轮没有重新执行物理烧录 HIL；docs/verification/v0.1.9-stm32f103re-release-hil.md 的原始日志未在当前工作区找到，因此只能作为既有叙述证据，不能冒充本轮新证据。
- Tauri 本轮只完成 --no-bundle Release 可执行文件构建；未生成或安装 NSIS，未做仅系统 PATH 的安装后运行验证，也未使用更新签名密钥。
- Web 生产构建仍提示 DashboardView 压缩后约 551.11 KiB，超过 500 KiB 建议阈值，但不影响构建成功。
- HIL relay 插件仍有既有 runtime/拒绝探针证据缺口；不影响本次 MKLink 插件 11/11 自动化准入。

## 延续协议

- 开始前校正 Git、台架 selector、目标固件和运行进程；硬件操作保持串行并遵守 HIL 锁。
- 不把环境失败、旧叙述证据或未覆盖场景写成 PASS；关键证据写验证报告，交接只保留结论。
- 结束前更新 project-memory.json、渲染 CURRENT_HANDOFF.md，并保持工作树与目标远端分支同步。
