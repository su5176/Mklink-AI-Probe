# 当前 AI 交接

> 本文件由 `python scripts/ai_memory.py render` 根据 `project-memory.json` 生成。

## 当前断点

- 更新时间：`2026-09-01T16:33:11+08:00`
- 分支：`feature/eternal-chip-gui`
- HEAD：`立芯分支以双父 merge commit 044568948783fb954ca7d5648761dda24b294dc4 纳入 origin/master 的完整 0.1.9，并保留立芯品牌 GUI、主题、About 对话框和演示入口。`
- 远端 HEAD：`origin/feature/eternal-chip-gui 已推送验证提交 a5af67e651c3e65fa6df0779d3d45574b0c26a86；MicroKeen/Mklink-AI-Probe 仍只有既有 main，使用当前 su5176 凭据创建 master 被 GitHub 以 403 拒绝。`
- 工作树：最终提交与推送后保持干净；STCP DLL、egg-info、测试运行目录和 Tauri/Cargo 输出均为忽略或外部构建产物。
- 当前任务：origin/master 的完整 0.1.9 已语义合并并推送到 feature/eternal-chip-gui；采用 master 的 UF2 与 AGENTS.md，保留立芯专属界面，并完成全量软件、真实 Chromium 与 HIL-Infra 门禁。MicroKeen master 镜像仅因目标仓库写权限待处理。
- 状态：`eternal-chip-v0.1.9-synced-microkeen-permission-pending`

## 里程碑

- **0.1.9 桌面端与 WebGUI** — `complete`。采用 PR #15 的 UF2 固件与 AGENTS.md；修复构建包装器退出码和锁定临时目录清理，稳定 Windows GUI 全量门禁，并同步生产 Web 资源。
- **隐私安全的内存可观测性** — `complete`。从两个备份分支一次性迁移 MCP 私有流、统一观测事件、内存 dump/RTT/SystemView 发布和测试；保留 0.1.9 的 32 位地址、4 KiB 直读、8 区域批写、12 KiB flush 与写后校验边界。
- **PR #15 本地集成门禁** — `complete`。Python、GUI、Go/STCP、Web、Tauri 和 HIL-Infra 只读门禁全部通过；PR #15 已以 merge commit 方式合并到 master。
- **立芯分支同步 0.1.9** — `complete`。以 merge commit 0445689 同步当前 master；业务源码无文本冲突，交接文档与可再生 dist 冲突按 master 记忆基线和合并后 Vue 源码重建解决。立芯品牌、六套主题、About 对话框、演示 mock 与专属布局继续保留。

## 验证证据

- **Python**：立芯合并提交后的最终全量 1790 passed, 1 skipped，耗时 612.29 秒；先构建/提供 STCP DLL 并启用 Python UTF-8 模式后，先前 4 个环境型失败用例定向复跑 4/4 通过。
- **GUI 与 Web**：Vitest 61 files / 630 tests 全部通过；生产构建转换 1952 modules。真实 Chromium 验证配置、仪表盘、脱机烧录、在线烧录路由，以及立芯 About、六套主题和 0.1.9 版本弹层；控制台 0 error / 0 warning，关键 API 与品牌静态资源均为 200。
- **Go/STCP**：master 集成证据中官方 Go 1.25.12 的 go test ./... 已通过；本轮机器无可调用 Go 工具链，复用与当前 go.mod/go.sum/main.go/main_test.go 逐文件同哈希的 x64 DLL，SHA-256=758E428FE9ED6BCBD9489D6DB1990FCE5A18680887137EA3CA6D348C27A77C07，并由全量 Site Agent 打包与归档审计通过。
- **Tauri Release 可执行文件**：Rust 1.95.0、Node 24.15.0 与 Python 依赖检查通过；npx tauri build --no-bundle 成功，19,225,600-byte EXE SHA-256=37A9C45A456393CFCA3FC555CAB5038082ABE269B00F14E8AEB637258A8C259D。
- **HIL-Infra 硬门禁**：contract_check 61 symbols / SHA 0347576fb2dd8c02...；pytest 380 passed；6 个 capmap 静态一致性均为 0 error / 0 warning / 0 waiver；bench-01、bench-gec1900、两份运行计划和资源语义映射通过。
- **HIL 运行时与插件准入**：MKLink、Toomoss、PSU、Camera 的 5 个负向运行时拒绝探针通过；MKLink 自动化插件评审 11/11 OK，Camera 11/11 OK。判断层确认 program.erase 为 irreversible、无人值守面仅开放 observe/debug.read、safe_state 不夸大且插件保持联邦接入；结束后无活动锁。
- **PR #15 固件资产**：按用户决策采用 PR 文件：HPMLink V4.3.8 SHA-256=D295858F3914998ABB7A19F6064157F6695C5B43E362CB00553D7A295A7E3FA8；MicroLink V3.3.8 SHA-256=E213B248A137C6519A9E926EAAC78718CE844C2A20555F3A3D2A48581B099BE8；MicroLink V4.3.9 SHA-256=F29821A6A34B75F3DAE96416595CA572A2DACAAF54C9DCC17521845F212DCEF6。
- **远端同步**：origin/feature/eternal-chip-gui 已从 795e83e 推进到 a5af67e。MicroKeen 远端不存在 master；尝试以当前 su5176 HTTPS 凭据创建时收到 403，未覆盖既有 main，也未发生部分写入。

## 架构决策

- UF2 固件文件和 AGENTS.md 采用 PR #15 版本。
- 立芯分支采用双父 merge commit 纳入当前 master，不重放旧 observability WIP；冲突策略固定为保留立芯品牌与主题交互、采用 master 的核心运行时、UF2、AGENTS.md 和 0.1.9 功能。
- 观测数据的公开事件仅包含安全事实，原始内存、RTT 和 SystemView 内容走有界私有流；发布失败降级但不改变成功读取结果。
- 构建、测试、日志和缓存统一位于 MKLINK_BUILD_ROOT 或主工作区忽略的 .build，并经 scripts/build_workspace.ps1 运行。
- 实际烧录、复位、供电、CAN 发送等不可逆或有外部影响的动作必须获得针对本次操作的明确确认。
- 应用 Release、标签、更新签名和探针固件发布彼此独立，不随 PR 合并自动执行。

## 真机环境

- **probe**：HIL-Infra bench-01 通过稳定 selector 与互操作锁寻址实插 MKLink V4；交接不记录端口号或完整探针标识。
- **target**：已有 v0.1.9 STM32F103RE 真机报告来自 master 集成基线；本轮立芯相对 master 的运行时差异仅在 GUI 品牌与主题，没有重烧目标。
- **permission**：本轮只做浏览器只读页面、identify/capabilities/health/safe_state、锁互操作与负向拒绝探针。若要重跑物理烧录闭环，需要用户明确授权目标、固件和写入动作。

## 下一动作

1. 为 su5176 授予 MicroKeen/Mklink-AI-Probe Write 以上权限后，执行 git push microkeen origin/master:refs/heads/master；该操作只新建 master，不修改现有 main。
2. 后续 master 再推进时，继续以语义 merge 同步 feature/eternal-chip-gui，并在立芯品牌表面与 master 新功能上重跑完整门禁。
3. 若需要重新证明目标侧行为，先获得明确烧录授权，再通过 hil-orchestrator 执行编译、烧录、激励、观测、判定和证据归档闭环。
4. 正式发布另行处理 NSIS、安装验证、Authenticode/更新签名、标签和 Release 资产；不要从本次分支同步或 MicroKeen master 镜像自动推断。

## 已知限制

- 本轮没有重新执行物理烧录 HIL；目标侧行为沿用 master 已验证的 0.1.9 基线，不能冒充本轮新烧录证据。
- Tauri 本轮只完成 --no-bundle Release 可执行文件构建；未生成或安装 NSIS，未做仅系统 PATH 的安装后运行验证，也未使用更新签名密钥。
- Web 生产构建仍提示 DashboardView 压缩后约 551.14 KiB，超过 500 KiB 建议阈值，但不影响构建成功。
- 本轮机器没有可调用的 Go 工具链；STCP 源码未改且打包门禁通过，但若需要新的 Go 单测证据，应在安装 Go 后重新执行 go test ./... 并重建 DLL。
- 当前 GitHub 身份 su5176 对 MicroKeen/Mklink-AI-Probe 没有写权限；创建 master 需要该仓库 Write 以上协作者/团队权限，或由有权限身份执行非强制推送。
- HIL relay 插件仍有既有 runtime/拒绝探针证据缺口；不影响本次 MKLink 插件 11/11 自动化准入。

## 延续协议

- 开始前校正 Git、台架 selector、目标固件和运行进程；硬件操作保持串行并遵守 HIL 锁。
- 不把环境失败、旧叙述证据或未覆盖场景写成 PASS；关键证据写验证报告，交接只保留结论。
- 结束前更新 project-memory.json、渲染 CURRENT_HANDOFF.md，并保持工作树与目标远端分支同步。
