# SystemView RTOS 跟踪（RT-Thread）

## 背景

SEGGER SystemView 是 RTOS 实时跟踪的事实标准：捕获**任务切换、ISR 进出时序、
每任务 CPU 占用、内核对象（信号量/互斥量/队列）事件**，用于定位优先级反转、
饥饿、ISR 延迟、调度抖动。传统上依赖正版 J-Link。

mklink 把 SystemView 做成**原生功能**：RTOS 的 `SEGGER_SYSVIEW` 集成把跟踪包写进
**RTT 上行通道 1**，探针固件的 `SystemView.*` 模块负责读取并转发这个通道，PC 端
用自带解码器（`SystemViewParser`）解出 SEGGER 事件。整条链路**不依赖 J-Link，
也不依赖 SEGGER PC 工具**。

探针内部对 SWD 和 RISC-V/JTAG 使用同一个 SystemView 转发模块，因此 ARM 与 HPM
目标共用同一条上位机实现，不需要按 CPU 架构切换后端。

## 工作原理

```
RT-Thread (SEGGER_SYSVIEW 钩子) ──事件包──▶ RTT 上行通道 1 ("SysView" 缓冲)
                                              │ SWD 或 RISC-V/JTAG
                              MKLink 探针 SystemView 转发 ──CDC──▶ PC
                                                                  │
                                           Recorder 握手 + SystemViewParser
                                                                  │
                   CLI 实时打印 / systemview-analyze / HTML 报告 / GUI 时间轴
```

- **地址发现**：RTT View 与 RTOS Trace 优先从已加载的 AXF/ELF 搜索
  `_SEGGER_RTT`；未手动选择文件时由工程配置自动发现构建产物。两边共享最近一次
  搜索或手工输入的地址，用户不需要额外加载 MAP 文件。
- **数据获取**：PC 端调用 `SystemView.start(addr, search_size, channel=1)`，随后完成
  SEGGER UART Recorder 的 `SV` 握手并发送开始命令。停止时发送 Recorder 停止命令和
  `SystemView.stop()`，让探针恢复命令行。所以目标工程**必须先集成 RTT**。
- **协议解码**：`mklink/systemview_parser.py`，逐字节对照 `SEGGER_SYSVIEW.c`
  （`_SendPacket`/`_EncodeU32`/`_SendSync`）实现；连续模式（无同步头）从任意位置对齐解析。
- **嵌入式端**：用户固件集成 `SEGGER_SYSVIEW` + RT-Thread 钩子（下述）。

## 两条集成路径

### 路径 A：mklink 自动集成（推荐，Keil 手工工程）

```
python -m mklink rtt-integrate --project-root .      # 先集成 RTT（若尚未）
python -m mklink systemview-integrate --project-root .
```

`systemview-integrate` 自动完成：
1. 把打包的 SEGGER 源（`mklink/systemview_sources/`：核心 `SEGGER_SYSVIEW.*` +
   `SEGGER.h` + RT-Thread 适配 `SEGGER_SYSVIEW_RTThread.c` + `Config_RTThread.c`）
   复制到工程内 `segger_systemview/`；
2. 注册 `.c` 到 Keil `SEGGER_SYSVIEW` 文件组 + 把该目录加入 `IncludePath`；
3. `applications/main.c` 注入 `#include "SEGGER_SYSVIEW.h"`（`USE_SYSTEMVIEW` 守卫）；
4. 工程 `<Define>` 追加 `USE_SYSTEMVIEW`。

> RT-Thread 在 `INIT_COMPONENT` 阶段**自动**调用 `rt_trace_init`（`INIT_COMPONENT_EXPORT`）
> 完成 `SEGGER_SYSVIEW_Conf` + 注册调度器/ISR/定时器/空闲钩子 + `Start`，**main.c 无需
> 手写任何 Init 调用**。发布固件时移除 `USE_SYSTEMVIEW` 宏即关闭（钩子全不注册）。

打包的 RT-Thread 适配（`SEGGER_SYSVIEW_RTThread.c`）是 mklink 的**精简版**，做了
RT-Thread 4.x 兼容处理（详见"踩过的坑"），只用 `rt_scheduler_sethook` 等通用钩子，
不依赖 RT-Thread 包系统的 `RTT_TRACE_ID_*`。

### 路径 B：RT-Thread Env/menuconfig（包系统工程）

在 RT-Thread Env 里 `menuconfig` → `RT-Thread online packages` → `tools packages` →
启用 `SystemView`（latest）→ 内核启用 `Enable hook list`。这样用 RT-Thread 官方包的
适配文件（含完整内核对象钩子）。mklink 的 PC 端采集/解码/分析对两种路径都通用。

## 配置（segger_systemview/SEGGER_SYSVIEW_Conf.h）

| 参数 | 默认 | 说明 |
|---|---|---|
| `SEGGER_SYSVIEW_RTT_CHANNEL` | 1 | SystemView 上行通道（mklink 读这个） |
| `SEGGER_SYSVIEW_RTT_BUFFER_SIZE` | 16384 | mklink 已从 SEGGER 默认 1024 调大——降低高事件率丢包 |
| `SYSVIEW_CPU_FREQ` | `SystemCoreClock` | 自动正确（CMSIS 变量） |
| `SYSVIEW_RAM_BASE` | 0x20000000 | STM32 SRAM 基址 |
| `USE_CYCCNT_TIMESTAMP` | 1 | DWT CYCCNT 做时间戳（CM4 有 DWT） |

## PC 端使用

`<REPORT_DIR>` 是[已选工作根目录](work-files.md)下的 `reports/` 绝对路径，
执行前先创建；不要将示例报告写到偶然的当前目录。

```bash
# 实时解码打印（控制台）
python -m mklink systemview --port COM5 --project-root . --duration 10

# 采集 + RTOS 运行态分析报告（CPU%/切换/ISR/异常）
python -m mklink systemview-analyze --duration 6

# 采集 + 自包含 HTML 可视化报告（浏览器打开，可分享/存档）
python -m mklink systemview-report --duration 6 --out "<REPORT_DIR>/report.html"

# 可视化时间轴：GUI Dashboard → "RTOS Trace" Tab
python -m mklink gui
```

GUI 中可直接在 RTOS Trace 页输入 RTT 地址或点击 **Auto Search**，不需要先打开
RTT View。若任一页面搜索或修改了地址，另一页面会立即复用；新打开的页面也会从
持久化设置读取同一地址。

MCP（Agent）等价：`systemview_start` / `capture_systemview` / `systemview_analyze` /
`systemview_report` / `systemview_integrate`。

### 任务名 / µs / 秒率（不依赖开机 INIT 包）

`SEGGER_SYSVIEW` 的 `INIT` 包（带 `cpu_freq`/`ram_base`/`id_shift`）+ `TASK_INFO`（任务名）
只在开机发一次，在高事件率下很快被环形缓冲覆盖。mklink 用三种独立手段绕开：
- **任务名**：`task_id` 已还原成真实 `rt_thread*` 指针，直接读目标的 `rt_thread.name`
  字段（扫描候选偏移 + 严格 C 标识符匹配，版本无关）。
- **µs / 秒率**：读目标 `SystemCoreClock` 变量（与 `SYSVIEW_CPU_FREQ` 同源）做换算（需 axf）。
- **ID 还原**：SEGGER 配置默认 `ram_base=0x20000000` / `id_shift=2`。

## 分析方法论（供 interpretation）

参考 [SEGGER SystemView 用户手册 UM08027](https://doc.segger.com/UM08027_SystemView.html)：
- **CPU 占用**：每任务/ISR 耗时占比——找饥饿（单任务 >90%）或满载（非空闲 >95%）。
- **上下文切换频率**：过高（>2000/s）= 调度开销大。
- **ISR 延迟**：最长 ISR >100µs 提示中断延迟/丢中断风险；ISR 占用 >30% 提示中断过重。
- **优先级反转**：低优任务持资源阻塞高优（时间轴模式；精简适配器未采内核对象事件，
  需补 `rt_object_*_sethook` + `RTT_TRACE_ID_*` 才能精确标定）。

`systemview-analyze` 自动检测并标出上述异常。

## 踩过的坑（mklink 适配器已固化修复）

| 现象 | 根因 | 修复 |
|---|---|---|
| `#error PKG_USING_SYSTEMVIEW` | RT-Thread 包系统宏，手工集成无 | 守卫改为接受 `USE_SYSTEMVIEW` |
| AC5 `#59 function call in constant expression` | `RT_VERSION_CHECK()` 在 4.x 未定义 | 改用 `RT_VERSION >= 5` 整数比较 |
| 链接 `SYSVIEW_X_OS_TraceAPI undefined` | 适配器未提供 OS API | 适配器内置 `_cbGetTime`/`_cbSendTaskList` + 该符号（注意大小写 `TraceAPI`） |
| RTT 控制块被 `###` 覆盖 | RT-Thread `HEAP_END=0x20020000` 越界覆盖静态 RTT 区 | `HEAP_END=0x2001F000` |
| `RTTView.start` 与 Recorder 协议不匹配 | SystemView 需要双向控制命令和握手 | 改用探针 `SystemView.start` + `SV` Recorder 握手 |
| 解码 0 事件 | 连续模式无同步头 | 解码器改为从任意位置对齐解析 |
| CPU% 跨次翻倍 | 32 位 CYCCNT 翻卷 | `abs_time` 单调累加（不掩码） |
| 缓冲裁掉 40KB 喂入 | `_MAX_BUF=32KB` 太小 | 调到 1MB |
| `systemview_stop` 置 bridge ERROR | 流模式无 prompt | raw 写 Recorder Stop + `SystemView.stop()`，固件恢复命令行 |

## 故障排查

| 现象 | 排查 |
|---|---|
| `未找到 RTT 控制块` | 在 RTOS Trace 中 Auto Search；确认 AXF/ELF 已加载或工程构建产物可被自动发现，并检查 `rtt-integrate` 和 `HEAP_END`；必要时重连探针 |
| `Recorder 握手超时/失败` | 确认探针固件包含 `SystemView.*` 转发并已更新；停止旧会话后重试 |
| 启动后一直无数据 | 检查 RTT 地址、通道 1、`USE_SYSTEMVIEW` 和目标是否正在运行；后端会在超时后报告错误，不会永久停在 starting |
| 解码 0 事件 / `未同步` | 确认 `USE_SYSTEMVIEW` 已定义、固件已重烧；`rt_trace_init` 自动注册（看启动 `kprintf`）|
| 任务名全 `0x...` | 检查 axf 已加载；`task_id` 是否还原成真实指针（`ram_base` 对不对）|
| µs/秒率为空 | `SystemCoreClock` 读取失败（axf 未加载或符号缺失）→ 回退 ticks |
| ISR 段为空 | 适配器未注册 `rt_interrupt_*_sethook`（精简版已加；老版本没有）|
| 丢包计数大 | 调大 `SEGGER_SYSVIEW_RTT_BUFFER_SIZE`；降低无关事件率 |
