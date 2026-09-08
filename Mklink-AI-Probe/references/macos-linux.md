# macOS/Linux 与 Keil 虚拟机

MKLink 留在 Mac 时，由 Mac 上的 CLI/MCP/WebGUI 操作；Parallels Windows 只运行
Keil 编译，通过共享目录提供 HEX/BIN 和 AXF/MAP。不要同时让两套系统操作同一探针。
USB 分配给虚拟机后，Mac 无法继续访问该设备，这是 USB 所有权限制。
见 [Parallels USB 说明](https://kb.parallels.com/en/122993)。

Apple Silicon 上 Windows 11 ARM 的 x86/x64 应用仿真不等于驱动兼容；内核驱动需要
ARM64 版本。Keil 无法发现设备时先检查虚拟机设备管理器、USB 是否已分配和所选
调试驱动，不能仅凭现象认定 MKLink 或 Keil 均不支持。
见 [Microsoft Windows on Arm FAQ](https://learn.microsoft.com/en-us/windows/arm/faq)。

- 原生串口/FLM 烧录需要系统已挂载 MICROKEEN：Mac `/Volumes/MICROKEEN`，
  Linux 通常 `/media/<user>/MICROKEEN` 或 `/run/media/<user>/MICROKEEN`。
  普通同名文件夹不算设备；多个卷时停止自动选择，用 `MKLINK_MICROKEEN_DISK`
  指定已挂载的 MICROKEEN 根目录。pyOCD 在线烧录不依赖 U 盘挂载。
- Mac 有多个 `cu.usbmodem*` 是复合设备接口，不能固定猜测尾号；自动连接优先使用
  USB 接口信息，元数据缺失时进行身份确认。Linux 端口权限不足需要系统管理员
  配置串口访问权限，不用 root 身份长期运行服务。
- 工程初始化不要求下载器连接。优先相对工程路径，修改宿主机可访问的固件/符号路径
  后重新初始化。`mcu_key` 是旧兼容字段，不是必填；SW-DP ID 不是完整 MCU 型号。
- 共享目录同步可能保留时间戳。若 Keil 增量编译未更新，执行 Rebuild
  （`UV4 -r <project.uvprojx> -t <target> -o <log>`），检查产物摘要和编译日志。
  见 [Keil 命令行](https://www.keil.com/support/man/docs/uv4cl/uv4cl_commandline.htm)。
- 固件复制与 AXF/DWARF 缓存按 SHA-256 内容校验；文件名、长度和时间戳未变化也不
  复用旧内容。原生烧录向 MICROKEEN 写文件后执行同步和主机端内容核对；这不等同于
  MCU Flash 校验，烧录仍应启用 verify。没有下载完成响应或返回错误时不报告成功。
- WebGUI 的本机路径来源会检查内容变化：HEX/BIN 重新检查并刷新预览；AXF/MAP
  变化会停止依赖它们的采集、重载符号并更新 RTT 地址。确认目标已下载对应固件后再
  手动启动 RTT/SuperWatch，不能仅凭主机文件变化推断板上程序也更新了。
  浏览器授予文件访问权限时，可以在当前页面会话内监测所选固件及 AXF/ELF；普通文件上传只是一份快照，
  无权追踪原文件，编译后须重新选择，或使用桌面版。
  在线烧录统一通过“选择 BIN / HEX”加载；桌面原生选择及获授权的浏览器文件句柄
  自动跟踪后续内容变化，不需要另外填写文件路径。桌面版应选择宿主机可访问的共享文件。
- WebGUI 和另一个 CLI 进程争用串口时，先停止采集并断开连接，再交给另一个工具；
  不需要为切换所有权强行杀进程。HTTP 500 表示请求失败，不等于服务进程崩溃。
- STM32H750 的 GPIOA 基址为 `0x58020000`，不是 `0x5C2xxxxx`。
  寄存器地址及外设时钟/电源域应按精确型号确认，不做自动地址纠正。
  见 [ST 官方 H750 头文件](https://github.com/STMicroelectronics/cmsis-device-h7/blob/master/Include/stm32h750xx.h)。
- M7 的缓存一致性需要目标固件配合：RTT 控制块/缓冲区使用适合的内存属性，或按
  固件设计维护缓存。主机不自动关闭整个 D-cache。RAM build ID/心跳是运行证据，
  不能代替完整固件回读校验。

“首次烧录没有进度、第二次才生效”与“请求失败后后端退出”需要原始命令响应、
服务退出日志及精确探针版本复现；没有这些证据时不能归因于探针缓存，也不自动烧两次。
本轮报告提供 MicroKeenV4 最新版，未提供可定位的版本号、失败工程和日志。
