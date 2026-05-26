"""Mock RTT View 测试 — 模拟 RTT 数据流并启动 Web 可视化。"""
import sys
import time
import threading
import math
import random

sys.path.insert(0, ".")
from mklink.rtt_viewer import VisualizationServer


def main():
    print("=" * 60)
    print("  MKLink RTT View — Mock Data Test")
    print("=" * 60)
    print()

    channel_metadata = {
        "temperature": {"color": "#e74c3c", "unit": "℃", "min": 15, "max": 80},
        "fan_speed": {"color": "#3498db", "unit": "RPM", "min": 0, "max": 2000},
        "voltage": {"color": "#2ecc71", "unit": "V", "min": 10, "max": 14},
        "power": {"color": "#f39c12", "unit": "W", "min": 0, "max": 500},
    }

    server = VisualizationServer(
        title="Diesel Heater RTT Mock",
        mode="RTT",
        max_points=500,
        channel_metadata=channel_metadata,
    )
    port = server.start()
    url = f"http://127.0.0.1:{port}"
    print(f"  URL: {url}")
    print(f"  浏览器已自动打开。按 Ctrl+C 停止。")
    print("-" * 60)

    # 自动打开浏览器
    import webbrowser
    webbrowser.open(url)

    # 模拟数据生成
    cycle = 0
    try:
        while True:
            cycle += 1
            t = cycle * 0.1  # 100ms 间隔

            # 模拟温度：缓慢上升 + 正弦波动
            temp = 25.0 + t * 0.05 + 3.0 * math.sin(t * 0.3) + random.gauss(0, 0.2)
            # 模拟风速：阶梯式变化
            fan = 800 + 200 * int(t / 5) + random.randint(-20, 20)
            fan = min(fan, 1800)
            # 模拟电压：稳定 + 小波动
            voltage = 12.0 + 0.3 * math.sin(t * 0.1) + random.gauss(0, 0.05)
            # 模拟功率：与温度相关
            power = max(0, (temp - 20) * 15 + random.gauss(0, 5))

            data_point = {
                "temperature": round(temp, 2),
                "fan_speed": round(fan, 0),
                "voltage": round(voltage, 3),
                "power": round(power, 1),
            }

            server.push_data_point(data_point)
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n[*] 正在停止...")
        server.stop()
        print("[OK] 测试结束")


if __name__ == "__main__":
    main()
