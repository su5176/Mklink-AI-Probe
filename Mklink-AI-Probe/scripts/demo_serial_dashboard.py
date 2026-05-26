"""Mock 数据测试：串口协议帧解析 + Web Dashboard 前端展示。

启动 Dashboard 并注入模拟数据（无需真实串口），浏览器自动打开供查看。
按 Ctrl+C 停止。
"""

from __future__ import annotations

import json
import random
import struct
import threading
import time

from mklink.serial._dashboard import SerialDashboardServer
from mklink.serial._frame import FrameParser, ParsedFrame, crc16_modbus
from mklink.serial._monitor import SerialEvent, SerialMonitor


# ---------------------------------------------------------------------------
# Mock Profile: 模拟 diesel-heater 的 UART 协议
# ---------------------------------------------------------------------------
MOCK_PROFILE = {
    "name": "diesel-heater-uart-mock",
    "version": "1.0",
    "frame": {
        "header": "AA55",
        "tail": "55AA",
        "length_field": {"offset": 2, "size": 1, "includes_header": False},
        "crc": {"algorithm": "crc16_modbus", "offset": -4, "scope": "payload"},
    },
    "fields": [
        {
            "name": "cmd",
            "offset": 3,
            "size": 1,
            "type": "uint8",
            "enum": {"0x01": "HEARTBEAT", "0x02": "SET_TEMP", "0x03": "STATUS", "0x04": "FAN_CTRL"},
        },
        {"name": "temperature", "offset": 4, "size": 2, "type": "int16", "scale": 0.1, "unit": "℃"},
        {"name": "fan_speed", "offset": 6, "size": 2, "type": "uint16", "unit": "RPM"},
        {"name": "power_level", "offset": 8, "size": 1, "type": "uint8"},
        {"name": "voltage", "offset": 9, "size": 2, "type": "uint16", "scale": 0.01, "unit": "V"},
    ],
    "auto_reply": [
        {"match_hex": "AA550103", "reply_hex": "AA55018100AABB55AA", "description": "ACK heartbeat"},
    ],
}


def build_mock_frame(cmd: int, temp: int, fan: int, power: int, voltage: int) -> bytes:
    """构建一个完整的模拟协议帧。"""
    header = bytes.fromhex("AA55")
    tail = bytes.fromhex("55AA")
    payload = struct.pack("<BhHBH", cmd, temp, fan, power, voltage)
    length_byte = struct.pack("<B", len(payload))
    frame_body = length_byte + payload
    crc = crc16_modbus(frame_body)
    crc_bytes = struct.pack("<H", crc)
    return header + frame_body + crc_bytes + tail


class MockSerialMonitor(SerialMonitor):
    """Mock 版本的 SerialMonitor，不打开真实串口，直接注入模拟数据。"""

    def __init__(self, profile: dict | None = None):
        super().__init__(
            ports=[
                {"port": "COM3-MOCK", "baudrate": 115200, "databits": 8, "stopbits": 1, "parity": "N"},
                {"port": "COM4-MOCK", "baudrate": 9600, "databits": 8, "stopbits": 1, "parity": "N"},
            ],
            profile=profile,
            auto_reply_rules=profile.get("auto_reply") if profile else None,
        )
        self._port_statuses = {"COM3-MOCK": "open", "COM4-MOCK": "open"}
        self._mock_thread: threading.Thread | None = None
        self._parser = FrameParser(profile) if profile else None

    def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        self._mock_thread = threading.Thread(target=self._mock_data_loop, daemon=True)
        self._mock_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._mock_thread:
            self._mock_thread.join(timeout=3.0)

    def send(self, port: str, data: bytes) -> bool:
        """模拟发送：直接生成 TX 事件。"""
        evt = SerialEvent(
            timestamp=time.time(),
            port=port,
            direction="TX",
            raw=data,
        )
        self._emit_event(evt)
        return True

    def _mock_data_loop(self) -> None:
        """持续生成模拟数据。"""
        cycle = 0
        base_temp = 200  # 20.0℃

        while not self._stop_event.is_set():
            cycle += 1

            # --- COM3-MOCK: 协议帧数据（diesel heater 状态） ---
            temp = base_temp + random.randint(-20, 50)  # 18.0~25.0℃ 波动
            fan = 800 + random.randint(0, 400) + (cycle % 20) * 50  # 800~1800 RPM
            power = min(8, max(1, (cycle % 8) + 1))
            voltage = 1200 + random.randint(-10, 10)  # 12.00V ± 0.10V
            cmd = random.choice([0x01, 0x01, 0x01, 0x03, 0x03, 0x04])

            frame = build_mock_frame(cmd, temp, fan, power, voltage)

            # 用 FrameParser 解析
            parsed = None
            if self._parser:
                frames = self._parser.feed(frame)
                if frames:
                    parsed = frames[0]
                self._parser.reset()

            evt = SerialEvent(
                timestamp=time.time(),
                port="COM3-MOCK",
                direction="RX",
                raw=frame,
                parsed=parsed,
            )
            self._emit_event(evt)

            # --- COM4-MOCK: ASCII 日志数据 ---
            if cycle % 3 == 0:
                messages = [
                    f"[INFO] System running, cycle={cycle}, uptime={cycle*0.5:.1f}s\n",
                    f"[DEBUG] ADC sample: battery={voltage/100:.2f}V, ntc_raw={random.randint(2000,3000)}\n",
                    f"[INFO] Fan speed adjusted to {fan} RPM (target: {fan+50} RPM)\n",
                    f"[WARN] Temperature approaching limit: {temp/10:.1f}C\n",
                ]
                if cycle % 15 == 0:
                    messages.append(f"[ERROR] Communication timeout on Modbus slave 2, retry #{random.randint(1,3)}\n")

                msg = random.choice(messages)
                evt2 = SerialEvent(
                    timestamp=time.time(),
                    port="COM4-MOCK",
                    direction="RX",
                    raw=msg.encode("utf-8"),
                    parsed=None,
                )
                self._emit_event(evt2)

            # --- 偶尔模拟 TX（自动应答） ---
            if cycle % 7 == 0:
                reply_frame = build_mock_frame(0x81, temp, fan, power, voltage)
                evt_tx = SerialEvent(
                    timestamp=time.time(),
                    port="COM3-MOCK",
                    direction="TX",
                    raw=reply_frame,
                    parsed=None,
                )
                self._emit_event(evt_tx)

            # 每 500ms 生成一组数据
            self._stop_event.wait(0.5)


def main():
    print("=" * 60)
    print("  MKLink Serial Debug — Mock Data Dashboard Test")
    print("=" * 60)
    print()
    print("  测试内容:")
    print("    1. 协议帧解析 (AA55 header, CRC16, 字段解码)")
    print("    2. 多端口数据 (COM3-MOCK: 二进制帧, COM4-MOCK: ASCII 日志)")
    print("    3. Web Dashboard 前端 (SSE 实时推送, 过滤, 发送)")
    print()
    print("  模拟数据:")
    print("    - COM3-MOCK: diesel-heater 协议帧 (温度/风速/功率/电压)")
    print("    - COM4-MOCK: ASCII 日志 (INFO/DEBUG/WARN/ERROR)")
    print()

    monitor = MockSerialMonitor(profile=MOCK_PROFILE)
    dashboard = SerialDashboardServer(
        monitor=monitor,
        host="127.0.0.1",
        port=8765,
        open_browser=True,
    )

    monitor.start()
    url = dashboard.start()

    print(f"  Dashboard URL: {url}")
    print()
    print("  浏览器已自动打开。按 Ctrl+C 停止测试。")
    print("-" * 60)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[*] 正在停止...")
        dashboard.stop()
        monitor.stop()
        print("[OK] 测试结束")


if __name__ == "__main__":
    main()
