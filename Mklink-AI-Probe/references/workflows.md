# 工作流程与错误处理

> 触发词：首次烧录、RTT 集成、故障排查
> 返回索引：[SKILL.md](../SKILL.md)

## 工作流程

### 新项目首次烧录

```bash
# 1. 初始化（只需一次）
python -m mklink project-init

# 2. 烧录
python -m mklink flash
```

### 编译后烧录 + 查看 RTT

```bash
# 1. 烧录最新固件
python -m mklink flash

# 2. 查看 RTT 输出
python -m mklink rtt --duration 15
```

### RTT 首次集成

```bash
# 1. 集成 RTT 源码到项目（自动检测工程类型和头文件路径）
python -m mklink rtt-integrate --project-root .

# 2. 在 Keil/IAR 中重新编译项目（手动）

# 3. 烧录并查看 RTT
python -m mklink flash
python -m mklink rtt
```

**生产固件：** 从工程定义中移除 `USE_RTT` 宏即可禁用所有 RTT 输出。

---

## 错误处理

| 场景 | 处理方式 |
|------|----------|
| COM 口不存在 | `python -m mklink discover` 查找端口 |
| IDCODE 无效 | 检查 SWD 接线和目标板供电 |
| FLM 加载失败（仅 Keil） | `python -m mklink copy-flm` 拷贝 FLM |
| IAR 项目不需要 FLM | IAR 使用内置 .board flash loader，无需 FLM |
| RTT 搜索失败 | 检查固件是否已集成 RTT 并重新编译 |
| RTT 集成验证失败 | 确认 `main()` 在合适位置调用了 `SEGGER_RTT_Init()`（通常在系统初始化之后） |
| 头文件目录不存在 | 检查项目的 Include Path 配置，使用 --inc-dir 指定正确路径 |
| HEX 文件未找到 | 先编译项目，再运行 `python -m mklink project-init` 更新路径 |
| 项目未配置 | `python -m mklink project-init` |
