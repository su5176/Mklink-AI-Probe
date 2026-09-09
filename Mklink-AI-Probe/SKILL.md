---
name: mklink-ai-probe
description: 使用 MKLink/MicroLink 操作目标 MCU：固件烧录、内存与 AXF 调试、RTT/VOFA/SuperWatch/SystemView、串口/Modbus、Web GUI、远程调试及安装排障。不用于维护 MKLink 本体、构建安装包或发布版本。
---

# MKLink 用户入口

本 Skill 只处理设备和用户工具。不要读取仓库交接、Git 状态、维护 Skill、构建或
发布流程；修改用户的目标 MCU 工程仍属于设备使用。仅加载本次任务对应的一个或
少数参考页，不预读全部文档。

## 开始前

- 每个会话首次加载本 Skill 时立即检查版本：调用 MCP
  `ping(force_update_check=True)` 并读取 `update`；没有该参数或 MCP 时，在本
  Skill 根目录运行 `python scripts/skill_update.py check --force --json`。
  不等到首次设备操作，不使用上次会话的 24 小时缓存；本会话后续调用不重复检查。
  离线继续任务，发现新版本时简要提示；安装仍须用户同意，按需读
  [安装与更新](references/install.md)。
- 有 MKLink MCP tool 时优先使用；能力未覆盖时用 `python -m mklink <command>`。
  参数以 tool schema/`--help` 为准，找不到入口再读[操作速查](references/tool-index.md)。
- 首次需要生成脚本、日志、采集或报告时，工作根目录固定为用户指定的非系统盘
  目录；用户未指定时使用目标项目 `.mklink/`。项目在系统盘或没有项目时先询问，
  不写 Skill 目录、AI 客户端目录、桌面或系统临时目录。按需读取
  [工作目录与清理](references/work-files.md)，并报告实际路径。

## 不可绕过的设备边界

- **单探针串行**：先读 `ping.limits`。同一下载器、命令口或目标串口同一时刻只
  运行一个操作；复用一次连接，不并行 tool 调用。停止流后先 `disconnect`，再建立
  普通命令会话。
- **VOFA 与 dump**：`read_memory`/`read_ram` 只做快照；连续曲线用
  `dump_memory`。精确 VOFA 和 dump/SuperWatch 每次最多 **15 个**离散地址或
  region；快速连续 float VOFA 最多 **16 路**；发送给 Pika 的完整命令最多
  **511 UTF-8 字节**。不得用循环 `read_ram` 绕过流边界。
- **flush**：单批总数据最多 **12 KiB**、最多 **8 个地址项**。超额时按批串行，
  每批等待提示符；不得与 dump、VOFA、RTT 或 SystemView 并发。
- **RTT/SystemView**：地址只允许省略或传目标已知可写 RAM 内的 4 字节对齐地址，
  不得拼接 Pika 表达式；V4 通道为 **0~2**，搜索窗口为 **0~65536 字节**且不得
  越出已知目标 RAM。让工具拒绝越界参数，不得改用
  原始命令绕过，也不得对失败的启动循环重试。MCP `rtt_write` 单次最多 **256
  UTF-8 字节**，超限不得自动拆分；文件或日志走 YMODEM/串口专用传输，禁止拆分
  绕过；输入不得包含或跨调用拼接探针保留串 `RTTView.stop()`。`capture_rtt` 的
  `pattern` 是 1~256 UTF-8 字节的字面子串，不是正则表达式。
- **MCP Timeout**：超时后只调用一次 `device_status`，随后结束旧会话并只执行一次
  `disconnect` → `connect` 恢复。任一步失败就停止并请用户重新拔插；禁止自动重试
  超时原调用，也禁止循环发送 stop、`reboot_probe` 或 `reboot()`。
- **AXF/ELF**：默认使用内置 pyelftools；`readelf_available:false` 不阻塞操作。
  只有用户明确指定 `elf_backend=external` 才调用外部 readelf/addr2line。
- **未知 MCU**：先 `detect_mcu_profile` / `mcu-detect`；不能改成 `custom` 绕过匹配。
  多个内部算法候选时请用户选择，缺少算法时停止并说明所需 Pack。
- **HPM**：`HPM*` 只用设备端 ROM API，不找 Pack、不加载 FLM、不追加通用 SWD
  reset；传 `.bin`、精确 `target_part`、`base_address` 及 `board` 或四字
  `hpm_flash_cfg`。
- **供电**：`set_power_on` 每次都先确认 1800/3300/5000 mV 并传
  `confirm_user=True`。5000 mV 还须确认供电路径和负载耐压，并传 `confirm_5v=True`。
- **加锁/解锁**：先调用 `security_status`，只有返回 `supported:true` 的精确型号才可
  继续；不支持的型号必须停止，禁止改通用型号绕过白名单。加锁必须提供刚校验通过
  的完整固件；解锁会永久擦除该型号声明的 Flash/EEPROM/备份数据，必须分别确认
  操作、数据丢失和本次恢复电压。只允许工具内置的可逆保护等级，绝不尝试 RDP2。
  当前 GD32 仅 `GD32F303xE` 512 KiB 容量组完成真机闭环；其安全操作必须使用
  复位下连接和用户确认电压的断电复位。不要用该结果推断其他 GD32 系列也受支持。
  当前 PY32 仅精确型号 `PY32F030K28T6` 完成真机闭环；解锁会擦除全部 64 KiB
  Flash，并强制使用复位下连接和用户确认电压的断电复位。其他 PY32 型号保持禁用。

## 按需路由

| 本次任务 | 只在需要时读取 |
|---|---|
| 下载固件、IDE/pyOCD/脱机后端选择 | [下载优先级](references/firmware-download-priority.md) |
| 烧录、项目初始化、RTT 集成/捕获 | [烧录与 RTT](references/commands-flash-rtt.md) |
| 固定 RTT 控制块地址 | [RTT 静态模式](references/rtt-static-mode.md) |
| SystemView RTOS 跟踪与报告 | [SystemView](references/systemview-rtthread.md) |
| RAM、变量、VOFA/SuperWatch、AXF、HardFault | [内存与符号](references/commands-memory.md) |
| flush-memory、多地址或分块写入 | [静默写边界](references/flush-memory.md) |
| Modbus、RS485、点表 | [Modbus](references/commands-modbus.md) |
| UART、串口 profile | [串口](references/commands-serial.md) |
| VPN/局域网 Site Agent | [直连远程](references/commands-remote.md) |
| 本地 Web GUI/API、桌面应用 | [本地 GUI](references/commands-remote-gui.md) |
| 安装、更新、运行依赖故障 | [安装与更新](references/install.md) |
| U 盘/桌面 HTML 快速入口 | [Web 入口](references/web-entry.md) |
| Windows USB 端口名称 | [端口命名](references/windows-port-names.md) |
| 复杂编排或故障排查 | [工作流](references/workflows.md) |
| 疑似 MKLink 缺陷、授权反馈或跟进修复 | [问题反馈](references/issue-reporting.md) |
