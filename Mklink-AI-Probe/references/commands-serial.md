# 串口调试

> 触发词：serial、UART、profile、open、send、dashboard、auto-reply
> 返回索引：[SKILL.md](../SKILL.md)

## 串口调试

通用 UART 串口调试工具，独立于 MKLink 调试器，直接操作 COM 端口。

### 列出可用端口

```bash
python -m mklink serial list
```

自动排除 MKLink 的 `MI_04` Python Console 命令口；保留 `MI_02` USB to UART，
并在 V4 上保留 `MI_06` USB to RS485，供通用串口或 Modbus 使用。

### 交互式终端

```bash
# ASCII 模式
python -m mklink serial open --port COM3 --baud 115200

# HEX 模式 + 协议解析
python -m mklink serial open --port COM3 --baud 115200 --mode hex --profile my_protocol.json

# 带日志和自动应答
python -m mklink serial open --port COM3 --baud 115200 --log data.txt --auto-reply rules.json
```

终端快捷键：Ctrl+Q 退出、Ctrl+H 切换 HEX/ASCII、Ctrl+F 设置过滤、Ctrl+L 清屏。
输入 `>hex AA55010300` 发送 HEX 数据。

### 单次发送

```bash
# 发送 ASCII
python -m mklink serial send --port COM3 --baud 115200 "AT+RST\r\n"

# 发送 HEX，重复 5 次，间隔 0.5 秒
python -m mklink serial send --port COM3 --hex --count 5 --delay 0.5 "AA550103"
```

### 多端口监听

```bash
python -m mklink serial monitor --port COM3 --port COM4 --baud 115200 --log multi.csv
```

### 无头日志模式

```bash
python -m mklink serial log --port COM3 --baud 115200 --output data.csv --format csv --duration 60
```

### Web Dashboard

```bash
python -m mklink serial dashboard --port COM3 --baud 115200 --profile protocol.json
```

浏览器自动打开，提供实时数据流、发送面板、命令队列、过滤器和帧解析视图。

### 本地资源释放（不需要 FastAPI）

Agent 和命令行优先使用本地 `resources` 命令释放串口资源；不需要启动 `mklink serve` 或 FastAPI。

```bash
# 查看本地 MKLink/串口锁状态
python -m mklink resources status --port COM3

# 清理指定串口的 stale 锁，并停止当前进程内的 serial dashboard manager
python -m mklink resources release-serial --port COM3

# 活进程仍占用时仅报告 PID；确认需要终止 Mklink 锁文件记录的占用进程时再显式加 --force
python -m mklink resources release-serial --port COM3 --force
```

默认模式只移除 owner PID 已不存在的 stale lock，不会杀外部串口助手、Keil 或其它仍在运行的进程。若是外部程序占用 COM 口，需要关闭外部程序；`--force` 只应在确认锁文件记录的 owner 可以终止时使用。

### 协议 Profile 管理

```bash
# 从 C 源码自动生成 Profile
python -m mklink serial profile detect --source inc/uart_protocol.h
python -m mklink serial profile generate --source inc/uart_protocol.h --output .mklink/serial_profile.json

# 查看 Profile 内容
python -m mklink serial profile show --profile .mklink/serial_profile.json
```

### Profile JSON 格式

```json
{
  "name": "my-protocol",
  "version": "1.0",
  "frame": {
    "header": "AA55",
    "tail": "55AA",
    "length_field": {"offset": 2, "size": 1, "includes_header": false},
    "crc": {"algorithm": "crc16_modbus", "offset": -2, "scope": "payload"}
  },
  "fields": [
    {"name": "cmd", "offset": 3, "size": 1, "type": "uint8", "enum": {"0x01": "READ"}},
    {"name": "temperature", "offset": 4, "size": 2, "type": "int16", "scale": 0.1, "unit": "℃"}
  ],
  "auto_reply": [
    {"match_hex": "AA5501", "reply_hex": "AA558100", "description": "ACK"}
  ]
}
```

支持 CRC 算法：`crc8`, `crc16_modbus`, `crc16_ccitt`, `crc32`, `checksum8`, `checksum16`

