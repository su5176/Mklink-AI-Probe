"""Quick test server for SuperWatch UI improvements."""
import math, sys, time, threading
sys.path.insert(0, ".")
from mklink.rtt_viewer import VisualizationServer

# 一个结构体，包含 bitfield
INSPECT_TREE = {
    "heaterState": {
        "name": "heaterState",
        "type": "HeaterState_t",
        "size": 32,
        "address": "0x20000100",
        "children": [
            {"name": "mode", "type": "uint8_t", "size": 1, "offset": 0, "value": "3"},
            {"name": "flags", "type": "uint8_t", "size": 1, "offset": 1,
             "children": [
                 {"name": "enabled", "kind": "bitfield", "bit_offset": 0, "bit_size": 1, "value": "1", "type": "bool"},
                 {"name": "heating", "kind": "bitfield", "bit_offset": 1, "bit_size": 1, "value": "1", "type": "bool"},
                 {"name": "fault", "kind": "bitfield", "bit_offset": 2, "bit_size": 1, "value": "0", "type": "bool"},
                 {"name": "speedMode", "kind": "bitfield", "bit_offset": 3, "bit_size": 2, "value": "2", "type": "uint8_t"},
             ]},
            {"name": "targetTemp", "type": "float", "size": 4, "offset": 4, "value": "45.0"},
            {"name": "currentTemp", "type": "float", "size": 4, "offset": 8, "value": "23.5"},
            {"name": "fanSpeed", "type": "uint16_t", "size": 2, "offset": 12, "value": "1500"},
            {"name": "pumpFreq", "type": "uint16_t", "size": 2, "offset": 14, "value": "5"},
        ],
    },
}

# 搜索返回的变量名
SEARCH_RESULTS = ["heaterState"]

def make_server():
    server = VisualizationServer(
        host="127.0.0.1",
        port=0,
        max_points=500,
        title="SuperWatch UI Test",
        mode="SuperWatch",
        channel_metadata={
            "heaterState": {
                "type": "HeaterState_t",
                "size": 32,
                "source": "struct",
                "address": "0x20000100"
            },
        },
        superwatch_callbacks={
            "search": lambda q: [s for s in SEARCH_RESULTS if q.lower() in s.lower()],
            "inspect": lambda name: INSPECT_TREE.get(name) or INSPECT_TREE.get(name.split(".")[0]),
            "add": lambda name: {
                "name": name,
                "type": "float",
                "size": 4,
                "source": "ram",
                "address": "0x20000100",
                "color": "#40a0e0"
            } if name in SEARCH_RESULTS or name.startswith("heaterState.") else None,
            "remove": lambda name: {"removed": True},
        },
    )
    server._interval = 0.1
    stop = threading.Event()
    counter = [0]

    def poll():
        while not stop.is_set():
            if server.collecting.is_set():
                counter[0] += 1
                n = counter[0]
                # 推送实时数据
                data = {
                    "_t": time.time(),
                    "heaterState.mode": n % 6,  # 0-5 循环
                    "heaterState.targetTemp": 45.0 + math.sin(n * 0.1) * 2,
                    "heaterState.currentTemp": 23.5 + math.sin(n * 0.05) * 5,
                    "heaterState.fanSpeed": 1500 + int(math.sin(n * 0.1) * 200),
                    "heaterState.pumpFreq": 5 + (n % 3),
                    "heaterState.flags.enabled": 1,
                    "heaterState.flags.heating": 1 if n % 20 > 5 else 0,
                    "heaterState.flags.fault": 0,
                    "heaterState.flags.speedMode": n % 4,
                }
                server.push_data_point(data)
            stop.wait(0.1)

    threading.Thread(target=poll, daemon=True).start()
    port = server.start()
    return server, stop, port

def main():
    server, stop, port = make_server()
    url = f"http://127.0.0.1:{port}"
    print(f"\n=== SuperWatch UI Test Server ===")
    print(f"  URL: {url}")
    print(f"\n变量: heaterState (包含 bitfield: flags.enabled, flags.heating, etc.)")
    print(f"\n按 Ctrl+C 停止服务器\n")
    print(f"PORT:{port}", flush=True)

    import webbrowser
    webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
        stop.set()
        server.stop()
        print("Server stopped.")

if __name__ == "__main__":
    main()
