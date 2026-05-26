"""Demo script: RTT visualization with simulated data.
Run: python demo_rtt_view.py
Then open the URL printed in browser.
"""
import time
import random
import math
import sys
import os

# Add skill to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mklink.rtt_viewer import VisualizationServer
import webbrowser


def main():
    server = VisualizationServer(host="127.0.0.1", port=0, max_points=300)
    port = server.start()
    url = f"http://127.0.0.1:{port}"
    print(f"[OK] RTT View: {url}")
    print(f"[*] 推送模拟 RTT 数据中 (Tick, ADC, NTC, FanSpeed, Voltage) ...")
    print(f"[*] 按 Ctrl+C 停止")
    webbrowser.open(url)

    tick = 0
    t0 = time.time()
    try:
        while True:
            tick += 1
            elapsed = time.time() - t0
            data = {
                "_t": time.time(),
                "Tick": float(tick),
                "ADC": round(2164 + random.uniform(-50, 50), 1),
                "NTC": round(-68 + random.uniform(-3, 3), 1),
                "FanSpeed": round(abs(3200 * math.sin(tick * 0.05) + random.uniform(-100, 100)), 1),
                "Voltage": round(24.0 + random.uniform(-0.5, 0.5), 2),
            }
            server.push_data_point(data)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()
    finally:
        server.stop()
        print(f"[OK] 服务器已关闭 (共 {tick} 个数据点)")


if __name__ == "__main__":
    main()
