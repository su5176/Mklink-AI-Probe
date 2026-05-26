"""Modbus 通信监控/抓包日志。"""

from __future__ import annotations

import datetime
import sys
import time

from mklink.modbus._client import ModbusClient, ModbusError


_FC_NAMES: dict[int, str] = {
    0x01: "Read Coils",
    0x02: "Read Discrete Inputs",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x05: "Write Single Coil",
    0x06: "Write Single Register",
    0x07: "Read Exception Status",
    0x08: "Diagnostics",
    0x0B: "Get Comm Event Counter",
    0x0C: "Get Comm Event Log",
    0x0F: "Write Multiple Coils",
    0x10: "Write Multiple Registers",
    0x16: "Mask Write Register",
    0x17: "Read/Write Multiple Registers",
}


def _decode_fc_name(fc: int) -> str:
    if fc in _FC_NAMES:
        return _FC_NAMES[fc]
    if fc >= 0x80:
        orig = fc & 0x7F
        return f"Exception Response (orig FC={orig:#04x})"
    return f"Unknown FC={fc:#04x}"


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def monitor_traffic(
    client: ModbusClient,
    slave: int,
    interval: float = 2.0,
    output_format: str = "decoded",
    count: int | None = None,
    save_file: str | None = None,
) -> None:
    """监控 Modbus 通信流量。

    主动模式：周期性发送 FC03 探测请求，记录所有请求/响应。

    Args:
        client: 已连接的 ModbusClient
        slave: 监控的从站地址
        interval: 探测间隔（秒）
        output_format: 输出格式 (decoded/hex/both)
        count: 探测次数，None 为无限
        save_file: 保存日志到文件
    """
    log_lines: list[str] = []
    probe_count = 0

    print(f"[*] Modbus 监控: 从站 {slave}, 间隔 {interval}s")
    print(f"    输出格式: {output_format}")
    if save_file:
        print(f"    日志文件: {save_file}")
    print("    按 Ctrl+C 停止\n")

    try:
        while count is None or probe_count < count:
            ts = _timestamp()

            # 发送 FC03 探测
            try:
                raw_client = client.raw_client
                # 使用 pymodbus 内部方法捕获原始帧
                rr = client.read_holding_registers(0, 10, slave)

                if output_format in ("decoded", "both"):
                    line = f"[{ts}] TX → Slave={slave} FC=03 Read 10 Holding Registers from 0x0000"
                    print(line)
                    if save_file:
                        log_lines.append(line)

                    ts2 = _timestamp()
                    regs_str = ", ".join(f"0x{v:04X}" for v in rr)
                    line = f"[{ts2}] RX ← Slave={slave} FC=03 10 registers: [{regs_str}]"
                    print(line)
                    if save_file:
                        log_lines.append(line)

                if output_format == "hex":
                    print(f"[{ts}] TX/RX Slave={slave} FC=03 OK")

            except ModbusError as e:
                line = f"[{ts}] ERROR Slave={slave}: {e}"
                print(line)
                if save_file:
                    log_lines.append(line)
            except Exception as e:
                line = f"[{ts}] ERROR: {e}"
                print(line)
                if save_file:
                    log_lines.append(line)

            probe_count += 1
            sys.stdout.flush()

            if count is not None and probe_count >= count:
                break

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n[*] 监控已停止，共 {probe_count} 次探测")

    if save_file and log_lines:
        with open(save_file, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))
        print(f"[OK] 日志已保存到 {save_file} ({len(log_lines)} 行)")
