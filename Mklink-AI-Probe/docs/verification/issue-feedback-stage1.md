# 授权报障与维护队列验收

2026-09-08：开发与反馈入口迁移到 `MicroKeen/Mklink-AI-Probe/main`。
以最新已发布代码 `dbb6077` 为基线，仅增加报障文档、模板、维护队列与反馈流程 CI。
应用仍为 0.2.0，未重建或替换既有正式安装包，未迁移发布资产和更新索引。

- 本地队列及公共 Skill 边界测试：60 项通过，包含幂等记录、两轮预算、等待标签、排除 PR、分页、
  维护者/bot 评论不触发循环、异常账本失败和 shell 文本仅作为数据处理。
- Skill 校验通过；用户参考页可随运行时包发布，队列脚本和维护说明不在公共包白名单。
- 真实 GitHub 队列只读扫描成功，目前为空；未人为制造用户报障，也未宣称已完成真实缺陷的自动修复闭环。
- `Feedback checks` 在 push/PR 时执行队列与 Skill 边界检查，上传 JUnit 报告。它不是全产品或 HIL 验收。
- GitHub Windows runner [首次运行](https://github.com/MicroKeen/Mklink-AI-Probe/actions/runs/34182692110)
  在 `bde9f2f` 上 60 项通过，JUnit 附件已上传；无秘密凭据、仅授予 contents:read。
- 本机每小时任务 `mklink-issues-pr` 已启用，七项 `ai:*` 状态标签已配置。
- 本机定时任务承担分诊和修复 PR；需要本机/Codex 在线、GitHub 登录可用。缺少硬件时只记录待 HIL，
  不继承先前交互测试中的设备写入许可，不自动合并或发布。

应用既有全量与 HIL 证据见 [0.2.0 发布验收](v0.2.0-release-qualification.md)。
新固件尚未做 HIL 的限制继续保留。原始测试 XML 和本地队列状态位于忽略目录 `.build/reports`。
