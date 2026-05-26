# 自然语言触发映射

> 触发词：用户说法、Agent 应执行、CLI 映射
> 返回索引：[SKILL.md](../SKILL.md)

## 通用与调试


| 用户说法 | Agent 应执行 |
|----------|-------------|
| "烧录最新程序" / "下载固件" | `python -m mklink flash` |
| "烧录 IAR 项目" / "IAR 固件烧写" | `python -m mklink flash`（自动检测 IAR 工程） |
| "查看 RTT 输出" / "启动 RTT" | `python -m mklink rtt --duration 10` |
| "RTT View" / "RTT 波形" / "实时图表" | `python -m mklink rtt --visualize --duration 30`（浏览器标题显示 MKLink RTT View + RTT 模式徽章） |
| "读取 RAM" / "读内存" / "查看 RAM 数据" | `python -m mklink read-ram --addr 0x20000000 --size 256` |
| "读寄存器" / "读取 CFSR" / "查看 SCB 寄存器" | `python -m mklink read-reg SCB.CFSR` |
| "HardFault 分析" / "解码 Hard Fault" | `python -m mklink hardfault --source <axf> --sp <异常栈帧地址>` |
| "查看变量类型" / "DWARF 类型" / "结构体布局" | `python -m mklink typeinfo --source <axf> --var <变量>` 或 `--struct <结构体>` |
| "变量快照" / "watch 变量" | `python -m mklink watch var1,var2 --source <axf>` |
| "SuperWatch" / "连续观察变量" / "变量和寄存器实时看板" / "read_ram 时间戳采样" | `python -m mklink superwatch var1,struct.field,SCB.CFSR --source <axf> --visualize --period 0.1` |
| "dump memory" / "二进制流采样" / "SuperWatch 高速模式" | `python -m mklink superwatch var1,var2 --source <axf> --dump-mem --visualize` |
| "内存占用" / "memmap" / "RAM Flash 占用" | `python -m mklink memmap --source <axf>` |
| "写入 RAM" / "写内存" | `python -m mklink write-ram --addr 0x20001000 0xDE 0xAD` |
| "读取 Flash" / "查看 Flash 内容" / "看中断向量表" | `python -m mklink read-flash --addr 0x08000000 --size 128` |
| "VOFA 观测" / "变量观测" / "实时波形" | 需进一步询问变量地址/类型 → `python -m mklink vofa <地址> <类型> [...] --period <秒>` |
| "连续读取 float" / "VOFA 快速模式" / "连续观测 N 个 float" | `python -m mklink vofa 0x20000030 5 --period 0.00001`（方式1） |
| "多变量观测" / "混合类型观测" / "VOFA 精确模式" | `python -m mklink vofa 0x20000030 uint8_t 0x2000154c float --period 0.001`（方式2） |
| "符号解析" / "列出变量" / "解析 AXF" / "查看 AXF 符号" | 先确认 `arm-none-eabi-readelf --version`，再执行 `python -m mklink symbols --source <axf>` |
| "VOFA 可视化" / "VOFA 波形" / "变量实时图表" / "本地看 VOFA" | `python -m mklink vofa <变量参数> --visualize --period 0.01 --names 名称1,名称2` |
| "停止 VOFA" / "停止观测" | `python -m mklink vofa --stop` |
| "连接烧录器" / "测试连接" | `python -m mklink discover` |
| "查看项目配置" | `python -m mklink project-info` |
| "初始化项目" | `python -m mklink project-init` |
| "解析 IAR 工程" / "查看 IAR 配置" | `python -m mklink iar-parse` |
| "解析 Keil 工程" / "查看 Keil 配置" | `python -m mklink keil-parse` |
| "集成 RTT（Keil/IAR）" | `python -m mklink rtt-integrate --project-root .` |
| "拷贝 FLM（Keil）" | `python -m mklink copy-flm` |


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
| "生成协议 profile" / "从 C 结构体生成串口协议" | `python -m mklink serial profile detect --source inc/uart_protocol.h` |
