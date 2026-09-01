# 固件下载优先级

> 触发词：下载固件、编译并烧录、Keil、IAR、pyOCD、在线烧录、脱机烧录、FLM
> 返回索引：[SKILL.md](../SKILL.md)

## 强制路由

除 HPM 例外外，自动下载严格按以下顺序选择：

1. **IDE 原生编译和下载**：工程文件存在且对应 IDE 可用时首选。
2. **pyOCD 在线烧录**：IDE 不可用、不适用，或用户只有预编译 HEX/BIN 时使用。
3. **MKLink 脱机下载 API**：前两种能力都不适用，或用户明确要求把任务部署到下载器时使用。

`python -m mklink flash` 是原生 MKLink 串口/FLM 路径，只在用户明确要求该路径、兼容旧流程或诊断时使用，不参与上述自动优先级。

“不可用/不适用”可以进入下一优先级，例如没有 IDE、没有工程文件、只有预编译镜像或 pyOCD 不支持目标。“已经开始但失败”不能静默换后端；先保留日志并报告编译错误、下载错误、目标配置或硬件问题，得到用户同意后再切换。

## Keil 默认流程

先从 `.mklink/project_info.json` 或 `python -m mklink project-init` 获取 `uvprojx_path` 和 `target_name`，再定位 `UV4.exe`。默认执行编译后下载：

先按[工作目录约定](work-files.md)确定并创建本次日志目录，将其绝对路径赋给
`$MklinkLogDir`；不要使用系统临时目录。

```powershell
$BuildLog = Join-Path $MklinkLogDir 'mklink-keil-build.log'
$FlashLog = Join-Path $MklinkLogDir 'mklink-keil-flash.log'

& $Uv4 -b $Uvprojx -t $Target -j0 -o $BuildLog
if ($LASTEXITCODE -ge 2) { throw 'Keil build failed' }

& $Uv4 -f $Uvprojx -t $Target -j0 -o $FlashLog
if ($LASTEXITCODE -ge 2) { throw 'Keil download failed' }
```

Keil 返回码 `0` 表示成功，`1` 表示仅警告，`2` 及以上视为失败。即使返回码可接受，也要检查日志没有错误，并确认预期 HEX/AXF/MAP 已生成或更新。

仅当用户明确说“不要编译，只下载”，且现有产物存在并与目标一致时，跳过 `-b`，直接执行 `-f`。不要根据文件存在就擅自跳过默认编译。

## IAR 和其他 IDE

IAR 工程优先用 `IarBuild.exe <project.ewp> -build <configuration>` 编译。只有项目已经提供并验证过 C-SPY/批处理下载配置时才直接调用 IDE 下载；不要猜测通用 C-SPY 参数。缺少可靠的 IDE 下载入口时，将该能力视为不可用并进入 pyOCD 在线烧录。

其他 IDE 同样要求已知且可验证的命令行编译/下载入口。不要为赶进度临时拼接未验证参数。

## pyOCD 在线烧录

在线烧录使用 `/online-flash` 页面或 `/api/online-flash` REST 工作流，不要用原生串口 `python -m mklink flash` 冒充 pyOCD：

1. 启动 `python -m mklink serve --host 127.0.0.1 --port 8765 --project-root <project>`。
2. `GET /api/online-flash/probes` 选择 MKLink CMSIS-DAP 探针。
3. `GET /api/online-flash/targets` 确认精确器件。
4. `POST /api/online-flash/images/inspect` 上传并检查 HEX/BIN；BIN 必须给基址。
5. `POST /api/online-flash/jobs` 按 `connect, erase/program, verify, reset, disconnect` 启动作业。
6. 轮询作业或 SSE 事件直到 `succeeded`，失败时保留稳定错误码并停止回退。

详细端点和资源冲突处理见 [commands-remote-gui.md](commands-remote-gui.md)。

## 脱机下载兜底

最后使用 `/offline-flash` 页面或 `/api/offline-download` 的 `status`、`detect-model`、`algorithms`、`preview`、`deploy` 和 `trigger` 流程。部署前确认下载器型号、固件顺序、自动次数、IDCODE 超时、SWD 时钟和固件/算法绑定；部署成功不等于目标已经执行，触发后要等待设备输出结束并验证目标结果。

## FLM 来源

自动选择顺序：

1. 发布包内置的精简 Pack 算法；
2. 发布包内置的 DAPLink FLM；
3. 当前用户已安装的 CMSIS-Pack；
4. 已登记的用户自定义 FLM。

自动选择优先使用覆盖固件地址范围且标记为默认的内置算法。只有内置源没有精确器件/地址覆盖时才查询已安装 Pack 和自定义目录。用户显式提供 `--flm` 或在界面选择算法时，显式选择覆盖自动顺序；执行前仍校验文件后缀、范围和摘要。

HPM 型号是固定例外：只使用设备端 HPM ROM API 和 BIN，不发现、下载或加载任何 FLM/Pack。

## 完成信号

IDE/pyOCD/脱机任一路径都必须记录实际使用的后端、产物摘要和目标型号。下载后至少完成 Flash 回读或后端 verify，并通过 RTT、运行计数或用户指定行为证明固件在目标上运行；最后释放探针和服务资源。
