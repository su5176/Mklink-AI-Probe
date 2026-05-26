"""Memory View Demo - launches mock server and opens browser."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "testing" / "tests"))
sys.stdout.reconfigure(line_buffering=True)

from mock_memory_server import make_mock_memory_server
import webbrowser
import time

server, backend = make_mock_memory_server()
port = server.start()
url = f"http://127.0.0.1:{port}"
print(f"[OK] Memory View demo: {url}")
webbrowser.open(url)

print("Mock server running. Memory mutates every 2s for change highlighting demo.")
print("Press Ctrl+C to stop.")
try:
    while True:
        time.sleep(2)
        backend.mutate(4)
except KeyboardInterrupt:
    server.stop()
    print("Stopped.")
