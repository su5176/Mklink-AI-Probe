# 自然语言触发映射

> 触发词：用户说法、Agent 应执行、CLI 映射
> 返回索引：[SKILL.md](../SKILL.md)

## 通用与调试


| 用户说法 | Agent 应执行 |
|----------|-------------|
| "烧录最新程序" / "下载固件" | 读取 [firmware-download-priority.md](firmware-download-priority.md)：优先用工程 IDE 原生编译并下载；IDE 不可用/不适用时用 pyOCD 在线烧录；最后才用 MKLink 脱机下载 API |
| "烧录 Keil 项目" / "Keil 固件烧写" | 默认调用 `UV4.exe -b` 编译，再调用 `UV4.exe -f` 下载；仅在用户明确要求只下载且产物已验证时跳过编译 |
| "烧录 IAR 项目" / "IAR 固件烧写" | 用 `IarBuild.exe` 编译；仅使用项目已有且验证过的 IDE 下载配置，否则进入 pyOCD 在线烧录 |
| "使用 mklink flash" / "原生串口烧录" | 用户明确指定后执行 `python -m mklink flash`；这是兼容/诊断入口，不参与自动下载优先级 |
| "查看 RTT 输出" / "启动 RTT" | `python -m mklink rtt --duration 10` |
| "RTT View" / "RTT 波形" / "实时图表" | `python -m mklink rtt --visualize --duration 30`（浏览器标题显示 MKLink RTT View + RTT 模式徽章） |
| "读取 RAM" / "读内存" / "查看 RAM 数据" | `python -m mklink read-ram --addr 0x20000000 --size 256` |
| "读寄存器" / "读取 CFSR" / "查看 SCB 寄存器" | `python -m mklink read-reg SCB.CFSR` |
| "HardFault 分析" / "解码 Hard Fault" | `python -m mklink hardfault --source <axf> --sp <异常栈帧地址>` |
| "查看变量类型" / "DWARF 类型" / "结构体布局" | `python -m mklink typeinfo --source <axf> --var <变量>` 或 `--struct <结构体>` |
| "变量快照" / "watch 变量" | `python -m mklink watch var1,var2 --source <axf>` |
| "SuperWatch" / "连续观察变量" / "变量和寄存器实时看板" / "变量二进制流采样" | `python -m mklink superwatch var1,struct.field,SCB.CFSR --source <axf> --visualize --period 0.1`（仅走 `dump_memory`，不循环 `read_ram`） |
| "dump memory" / "内存二进制 dump" / "高速读取内存" | `python -m mklink dump-memory 0x20000000:16`（公共 `cmd.dump_memory` CLI；按地址/长度直接 dump） |
| "SuperWatch 高速模式" | `python -m mklink superwatch var1,var2 --source <axf> --visualize`（`--dump-mem` 仅为兼容旧命令的空操作参数） |
| "内存占用" / "memmap" / "RAM Flash 占用" | `python -m mklink memmap --source <axf>` |
| "写入 RAM" / "写内存" | `python -m mklink write-ram --addr 0x20001000 0xDE 0xAD` |
| "静默写 RAM" / "无 ACK 写" / "flush 写入" | `python -m mklink flush-memory 0x20010000:0x11,0x22 0x20010100:0x44,0x55`（多地址多字节；先停止并释放所有流式会话） |
| "读取 Flash" / "查看 Flash 内容" / "看中断向量表" | `python -m mklink read-flash --addr 0x08000000 --size 128` |
| "VOFA 观测" / "变量观测" / "实时波形" | 需进一步询问变量地址/类型 → `python -m mklink vofa <地址> <类型> [...] --period <秒>` |
| "连续读取 float" / "VOFA 快速模式" / "连续观测 N 个 float" | `python -m mklink vofa 0x20000030 5 --period 0.00001`（方式1） |
| "多变量观测" / "混合类型观测" / "VOFA 精确模式" | `python -m mklink vofa 0x20000030 uint8_t 0x2000154c float --period 0.001`（方式2） |
| "符号解析" / "列出变量" / "解析 AXF" / "查看 AXF 符号" | 直接执行 `python -m mklink symbols --source <axf>`，默认使用内置 pyelftools；仅在用户明确指定 `elf_backend=external` 时检查 GNU 工具 |
| "VOFA 可视化" / "VOFA 波形" / "变量实时图表" / "本地看 VOFA" | `python -m mklink vofa <变量参数> --visualize --period 0.01 --names 名称1,名称2` |
| "停止 VOFA" / "停止观测" | `python -m mklink vofa --stop` |
| "连接烧录器" / "测试连接" | `python -m mklink discover` |
| "烧录器版本" / "查看固件版本" / "MKLink 版本" / "MicroLink 版本" | `python -m mklink version`（默认仅当前版本；`--all` 看完整历史；`--raw` 看原始响应） |
| "查看项目配置" | `python -m mklink project-info` |
| "初始化项目" | `python -m mklink project-init` |
| "新 MCU" / "未知 MCU" / "STM32H723" / "缺少 FLM" / "profile 不匹配" | 先按内置 Pack、内置 DAPLink FLM、已安装 Pack、用户自定义 FLM 自动解析；仍无精确匹配时执行 `python -m mklink mcu-detect`，多候选再让用户选择并用 `--flm` 固化 |
| "解析 IAR 工程" / "查看 IAR 配置" | `python -m mklink iar-parse` |
| "解析 Keil 工程" / "查看 Keil 配置" | `python -m mklink keil-parse` |
| "集成 RTT（Keil/IAR）" | `python -m mklink rtt-integrate --project-root .` |
| "拷贝 FLM" | 先确认 profile 已存在；必要时 `python -m mklink mcu-detect`，再 `python -m mklink copy-flm` |

## VPN/局域网直连远程调试

> 以下意图统一先读取 [commands-remote.md](commands-remote.md)。现场机只运行
> 官方独立 Site Agent；本 Skill 只在工程师机使用。

| 用户说法 | Agent 应执行 |
|----------|-------------|
| "远程调试" / "VPN 调试现场板" / "局域网远程烧录" / "连接现场机" | 先确认现场机与工程师机已有受管 VPN/局域网直连，再按 `sites add` → `sites use` → `status` → `capabilities` 顺序建立工程师侧上下文 |
| "部署 Site Agent" / "现场机不装 Skill" / "现场服务" | 让现场维护者部署官方 Site Agent ZIP/EXE；默认先在回环地址验证，非回环监听必须显式 `--allow-lan` 并从环境变量或 owner-only secret file 读取 token |
| "注册远程站点" | 从环境变量读取 token，执行 `python -m mklink remote sites add field-a "ws://<VPN_OR_LAN_HOST>:8766" --token-env MKLINK_REMOTE_TOKEN`；不得把 token 放进 URL 或命令参数 |
| "项目使用现场站点" / "切换远程站点" | `python -m mklink remote --project-root . sites use field-a`；用户级默认切换使用 `sites switch field-a` |
| "查看远程状态" / "远程能力" / "远程端口" | 依次使用 `python -m mklink remote --site field-a health`、`status`、`capabilities`、`ports`，先做无探针健康检查再做设备操作 |
| "远程探针重连" | `python -m mklink remote --site field-a reconnect`；该命令重连现场探针，不修复 VPN/局域网链路 |
| "远程上传 AXF/固件" | `python -m mklink remote --site field-a upload <LOCAL_FILE>`；上传完成只返回 inert opaque reference，不会自动修改目标 |
| "远程烧录" / "远程擦除" / "远程写内存" | 先展示站点、目标、文件摘要和影响并取得本地明确授权；烧录使用带 `--yes` 的专用 `flash`，其他 schema 标记的高风险操作使用 `call ... --yes` |
| "停止或替换现场 Agent" | 先取得现场维护者授权；远程停止仅使用 `python -m mklink remote --site field-a stop-agent --yes`，替换文件必须由现场维护者在进程退出后使用已验证的官方包完成 |
| "远程 MCP" / "AI 操作现场站点" | 工程师机按可选 MCP 依赖启动 `mklink-remote-mcp` stdio；高风险 tool 调用必须传 `confirm=True` |


## Modbus

| 用户说法 | Agent 应执行 |
|----------|-------------|
| "Modbus 扫描" / "扫描从站" | `python -m mklink modbus scan --port COM7` |
| "读 Modbus 寄存器" / "读保持寄存器" | `python -m mklink modbus read --port COM7 --slave 1 --fc 3 --start 0 --quantity 10` |
| "写 Modbus 寄存器" / "写保持寄存器" | `python -m mklink modbus write --port COM7 --slave 1 --fc 6 --start 0 100` |
| "Modbus 轮询" / "寄存器监控" | `python -m mklink modbus poll --port COM7 --slave 1 --registers "0:uint16:Temp"` |
| "Modbus 监控" / "通信抓包" | `python -m mklink modbus monitor --port COM7 --slave 1` |
| "Modbus 诊断" / "读异常状态" | `python -m mklink modbus diag --port COM7 --slave 1 --subfunc exception-status` |
| "Modbus 可视化" / "Modbus dashboard" / "Modbus 仪表盘" / "Modbus 监控" | `python -m mklink modbus dashboard --port COM7 --slave 1 --baud 57600` |
| "生成 Modbus 点表" / "生成 Modbus profile" / "Modbus 寄存器配置" | `python -m mklink modbus pointmap detect --project-root .` → 汇报摘要并确认 → `python -m mklink modbus pointmap generate --project-root .` |
| "创建自定义 Modbus 可视化" / "生成 Modbus 仪表盘" / "Modbus 组态" | 先使用 pointmap 生成 `.mklink/modbus_profile.json`；如需自定义 UI，再读取 `mklink/modbus/prompts/modbus_dashboard_prompt.md` 生成 `.mklink/modbus_dashboard.html` |

## 串口调试

| 用户说法 | Agent 应执行 |
|----------|-------------|
| "串口列表" / "列出 COM 口" | `python -m mklink serial list` |
| "打开串口" / "串口终端" | `python -m mklink serial open --port COM3 --baud 115200` |
| "发送串口数据" / "发 HEX" | `python -m mklink serial send --port COM3 --baud 115200 "..."` 或 `--hex` |
| "串口监控" / "多端口监听" | `python -m mklink serial monitor --port COM3 --port COM4 --baud 115200` |
| "串口 dashboard" / "串口 Web 界面" | `python -m mklink serial dashboard --port COM3 --baud 115200` |
| "释放串口" / "串口被占用" / "虚拟串口占用" / "清理串口资源" | `python -m mklink resources release-serial --port COM3`（本地 CLI，不需要启动 FastAPI；只清理 stale 锁，活进程需显式 `--force`） |
| "生成协议 profile" / "从 C 结构体生成串口协议" | `python -m mklink serial profile detect --source inc/uart_protocol.h` |
