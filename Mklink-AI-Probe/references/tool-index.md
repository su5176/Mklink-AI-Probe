# MKLink 操作速查

只在查找工具或命令时读取；参数以当前工具 schema 或 CLI `--help` 为准。
无需按顺序执行全部操作。

## MCP tool 速查（按能力域）

| 域 | Tools | 备注 |
|---|---|---|
| 健康 | `ping` | 无需连接，首调确认 server 活着 |
| 项目配置 | `detect_mcu_profile` | 新 MCU 发现、FLM 候选选择、profile 固化 |
| 连接 | `discover_probes` · `connect` · `disconnect` · `device_status` | connect 传 `axf=` 才能读变量 |
| Flash / 探针控制 | `flash` · `erase_chip` · `erase_sector` · `reset` · `set_power_on` · `reboot_probe` | `reset` 复位目标；VCC 任意电压均须逐次确认，5 V 另须耐压确认；`reboot_probe` 会断连 |
| 内存 | `read_memory` · `read_memory_regions` · `write_memory` · `flush_memory` | 快照 regions 最多 16 项/总计 4096B；flush 单批最多 12 KiB/8 项，超额由调用方串行分批 |
| 变量/寄存器 | `read_variable` · `write_variable` · `read_register` | 需先 connect(axf=) 或 load_symbols |
| 调试 | `halt` · `resume` · `step` · `set_breakpoint` · `clear_breakpoint` · `clear_all_breakpoints` · `read_core_registers` | FPB 硬件断点 |
| 符号 | `load_symbols` · `symbols_status` · `memory_map` | DWARF 段表 |
| RTT | `rtt_start`(mode=auto/dynamic/static) · `rtt_read` · `rtt_write` · `rtt_stop` · `capture_rtt` | mode 决策见 [rtt-static-mode.md](rtt-static-mode.md) |
| **SystemView** | `systemview_integrate` · `systemview_start` · `systemview_read` · `systemview_stop` · `capture_systemview` · `systemview_decode` · `systemview_analyze` · `systemview_analyze_events` · `systemview_report` | RTOS 跟踪（任务切换/ISR/CPU%）；集成见 [systemview-rtthread.md](systemview-rtthread.md)；先 rtt-integrate |
| HardFault | `check_hardfault` · `decode_hardfault` | decode 自动 CFSR 展开 + 内置 DWARF 源码回溯 |
| Modbus | `modbus_open` · `modbus_close` · `modbus_read` · `modbus_write` · `modbus_scan` | 独立串口（非探针） |
| 串口 | `serial_list` · `serial_open` · `serial_close` · `serial_send` · `serial_read` | 独立串口（非探针） |

> bytes 在 MCP 经 hex 字符串往返（`read_memory`→hex，`write_memory`/`serial_send`/`flush_memory`←hex）。

## CLI 命令速查（无 MCP 或未覆盖功能）

| 命令 | 说明 |
|------|------|
| `serve` | 本地 REST API + WebSocket JSON-RPC 服务；远程站点另见 commands-remote.md |
| `gui` | 启动 GUI（FastAPI 后端 + Vue 前端） |
| `web-entry` | 安装跨平台 URL Handler、自动生成 U 盘/桌面快速启动页、启动/停止其自有 Web 服务 |
| `mcp` | 启动 MCP server（stdio，供 Claude Code / 其他 MCP client 调用；本 plugin 自动拉起） |
| `remote` | 工程师侧直连 VPN/局域网站点：注册/选择、状态、能力、重连、原子上传与高风险操作 |
| `project-init` | 初始化项目配置（自动检测 IAR/Keil、MCU、COM 口） |
| `mcu-detect` | 发现/固化未知 MCU profile 与 FLM（多候选需选择） |
| `project-info` | 显示项目配置状态 |
| `flash` | 用户显式要求原生 MKLink 串口/FLM 路径时使用；自动下载先走 IDE，再走 pyOCD，最后脱机 API |
| `rtt` | 一站式 RTT 捕获（支持 `--visualize`） |
| `read-ram` | 读取 RAM 数据（十六进制 dump） |
| `read-reg` | 读取内存映射寄存器 |
| `write-ram` | 写入 RAM 并回读验证 |
| `dump-memory` / `dump` | 公共高速内存 dump（最多 **15 个 region**；默认 1 个样本；单次总量上限 **512 KiB**） |
| `flush-memory` | 静默写 RAM；不得与 dump/VOFA/RTT/SystemView 并发。**单批总计 ≤12 KiB、≤8 个地址项**，超额串行分批并等待提示符；详见 [references/flush-memory.md](flush-memory.md) |
| `read-flash` | 读取 Flash 数据 |
| `version` | 读取烧录器自身固件版本（`--all` 显示历史，`--raw` 原始输出） |
| `vofa` | VOFA+ 实时变量观测（快速连续 float 最多 16 路；精确离散地址/类型最多 15 路；Pika 命令最多 511 UTF-8 字节；支持 `--visualize`） |
| `symbols` | 从 ELF/AXF 列出 RAM 变量（默认内置 pyelftools） |
| `typeinfo` | 从 AXF DWARF 查询类型/结构体/枚举 |
| `watch` | 按变量名读取快照（支持 `struct.field`） |
| `superwatch` | 时间戳连续采样（支持 `--visualize`、`--dump-mem`） |
| `hardfault` | 解码 Cortex-M Fault 寄存器与异常栈帧 |
| `memmap` | 分析 AXF 段表（RAM/Flash 占用） |
| `rtt-integrate` | 集成 RTT 源码到 Keil/IAR 项目 |
| `systemview` | 一站式 SystemView RTOS 跟踪（实时解码任务切换/ISR） |
| `systemview-integrate` | 集成 SEGGER_SYSVIEW 到 RT-Thread 项目（先 rtt-integrate） |
| `systemview-analyze` | 采集并打印 RTOS 运行态分析（CPU%/切换/ISR/异常） |
| `systemview-report` | 采集并生成自包含 HTML 可视化分析报告（浏览器打开） |
| `rtt-find <map>` | 从 MAP 文件查找 RTT 地址 |
| `rtt_storage_mode=1` | 静态 RTT 编译（详见 [references/rtt-static-mode.md](rtt-static-mode.md)） |
| `copy-flm` | 拷贝 profile/工程指定的 FLM 到 MICROKEEN 磁盘 |
| `keil-parse` / `iar-parse` | 解析 Keil/IAR 工程文件 |
| `discover` | 发现 MKLink 端口 |
| `test --port COM6` | 测试连接 |
| `modbus` | Modbus RTU 调试（scan/read/write/poll/monitor/dashboard/pointmap） |
| `serial` | 通用 UART/RS485 串口调试；MKLink 仅隐藏 MI_04 命令口 |
| `resources` / `resource` | 本地资源管理（释放 stale 串口/MKLink 锁；不需要 FastAPI） |
