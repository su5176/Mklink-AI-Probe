"""Cross-platform HTML entry point for the local MKLink Web client."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


SCHEME = "mklink-ai-probe"
DEFAULT_PORT = 8765
MAX_PORT_ATTEMPTS = 20
APP_NAME = "Mklink AI Probe"
HANDLER_NAME = "Mklink AI Probe Web Launcher"
STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "operation.lock"
WINDOWS_OWNER_VALUE = "Mklink Web Entry Owner"
WINDOWS_HANDLER_VALUE = "Mklink Web Entry Handler"
WINDOWS_OWNER_ID = "com.microkeen.mklink-ai-probe.web-entry"
WINDOWS_FRIENDLY_NAME = "MKLink Web GUI"
WINDOWS_DESCRIPTION = "MKLink AI Probe Web GUI Launcher"
QUICK_LAUNCH_FILE_NAME = "MKLink Web GUI.html"
AUTO_LAUNCH_DELAY_SECONDS = 3
WEB_START_TIMEOUT_SECONDS = 45
LAUNCH_PAGE_TIMEOUT_SECONDS = 50

REQUIREMENT_MODULES: dict[str, tuple[str, ...]] = {
    "core": ("serial", "pymodbus", "elftools", "pycparser", "websockets"),
    "gui": (
        "fastapi", "starlette", "uvicorn", "pyocd", "intelhex", "multipart",
    ),
    "mcp": ("fastmcp", "pydantic"),
}


class WebEntryError(RuntimeError):
    pass


def web_entry_url(port: int, *, root: Path | None = None) -> str:
    """Return a build-specific URL so browsers cannot reuse an old app shell."""
    root = Path(root or repository_root())
    index = root / "gui" / "dist" / "index.html"
    try:
        build = hashlib.sha256(index.read_bytes()).hexdigest()[:12]
    except OSError:
        build = ""
    query = f"?build={build}" if build else ""
    return f"http://127.0.0.1:{int(port)}/{query}#/config"


def parse_protocol_uri(uri: str) -> str:
    parsed = urlparse(str(uri or "").strip())
    path = parsed.path.strip("/")
    if (
        parsed.scheme != SCHEME
        or parsed.netloc != "web"
        or parsed.query
        or parsed.fragment
        or path not in {"start", "open", "stop"}
    ):
        raise WebEntryError("Unsupported MKLink Web entry URI")
    return path


def platform_data_dir(
    *,
    system: str | None = None,
    environment: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    import platform

    system = system or platform.system()
    environment = environment if environment is not None else os.environ
    home = home or Path.home()
    if system == "Windows":
        root = environment.get("LOCALAPPDATA") or environment.get("APPDATA")
        if root:
            return Path(root) / APP_NAME / "web-entry"
        return home / "AppData" / "Local" / APP_NAME / "web-entry"
    if system == "Darwin":
        return home / "Library" / "Application Support" / APP_NAME / "web-entry"
    root = environment.get("XDG_DATA_HOME")
    if root:
        return Path(root) / "mklink-ai-probe" / "web-entry"
    return home / ".local" / "share" / "mklink-ai-probe" / "web-entry"


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def render_handler_script(skill_root: Path) -> str:
    return (
        "from __future__ import annotations\n"
        "import sys\n"
        f"sys.path.insert(0, {str(Path(skill_root).resolve())!r})\n"
        "from mklink.web_entry import protocol_handler_main\n"
        "raise SystemExit(protocol_handler_main(sys.argv[1:]))\n"
    )


def _quoted_command_argument(value: Path | str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _desktop_exec_argument(value: Path | str) -> str:
    escaped = str(value)
    for source, replacement in (
        ("\\", "\\\\"),
        ('"', '\\"'),
        ("`", "\\`"),
        ("$", "\\$"),
        ("%", "%%"),
    ):
        escaped = escaped.replace(source, replacement)
    return f'"{escaped}"'


def windows_registry_command(python_executable: Path, handler_path: Path) -> str:
    return (
        f"{_quoted_command_argument(python_executable)} "
        f"{_quoted_command_argument(handler_path)} \"%1\""
    )


def render_linux_desktop_entry(
    python_executable: Path,
    handler_path: Path,
) -> str:
    command = (
        f"{_desktop_exec_argument(python_executable)} "
        f"{_desktop_exec_argument(handler_path)} %u"
    )
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={HANDLER_NAME}",
        f"Exec={command}",
        "Terminal=false",
        "NoDisplay=true",
        f"MimeType=x-scheme-handler/{SCHEME};",
        "Categories=Development;Utility;",
        "",
    ])


def macos_info_plist() -> dict[str, Any]:
    return {
        "CFBundleName": HANDLER_NAME,
        "CFBundleDisplayName": HANDLER_NAME,
        "CFBundleIdentifier": "com.microkeen.mklink-ai-probe.web-entry",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "1.0",
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "applet",
        "LSUIElement": True,
        "CFBundleURLTypes": [{
            "CFBundleURLName": "MKLink Web Entry",
            "CFBundleURLSchemes": [SCHEME],
        }],
    }


def render_macos_applescript(
    python_executable: Path,
    handler_path: Path,
) -> str:
    command = (
        f"{shlex.quote(str(python_executable))} "
        f"{shlex.quote(str(handler_path))} "
    )
    command = command.replace("\\", "\\\\").replace('"', '\\"')
    return "\n".join([
        "on open location theURL",
        f'  do shell script "{command}" & quoted form of theURL & " >/dev/null 2>&1 &"',
        "end open location",
    ])


def _icon_data_uri() -> str:
    icon = repository_root() / "gui" / "src-tauri" / "icons" / "32x32.png"
    if not icon.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode("ascii")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_web_entry_requirements(
    *,
    root: Path | None = None,
    module_available: Callable[[str], bool] = _module_available,
) -> dict[str, Any]:
    """Check the complete runtime required by Web GUI and MCP entry points."""
    root = Path(root or repository_root())
    missing = [
        f"{group}:{module}"
        for group, modules in REQUIREMENT_MODULES.items()
        for module in modules
        if not module_available(module)
    ]
    if not (root / "gui" / "dist" / "index.html").is_file():
        missing.append("gui:built-web-assets")
    return {"ready": not missing, "missing": missing}


def render_launcher_html(*, icon_data_uri: str | None = None) -> str:
    icon_data_uri = _icon_data_uri() if icon_data_uri is None else icon_data_uri
    icon = (
        f'<img src="{icon_data_uri}" width="40" height="40" alt="">'
        if icon_data_uri else '<div class="mark" aria-hidden="true">MK</div>'
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>启动 Mklink Web</title>
<style>
:root {{ color-scheme: light; font-family: "Segoe UI",system-ui,sans-serif; background:#f3f5f6; color:#16191d; }}
* {{ box-sizing:border-box; }}
body {{ min-height:100vh; margin:0; display:grid; place-items:center; padding:24px; }}
main {{ width:min(520px,100%); border:1px solid #d9dee2; border-radius:8px; background:#fff; box-shadow:0 16px 40px rgba(22,25,29,.12); overflow:hidden; }}
header {{ display:flex; align-items:center; gap:12px; padding:20px 22px 16px; border-bottom:1px solid #e3e7ea; }}
header img,.mark {{ flex:0 0 40px; border-radius:7px; }}
.mark {{ height:40px; display:grid; place-items:center; background:#c96442; color:white; font-weight:700; }}
h1 {{ margin:0; font-size:18px; letter-spacing:0; }}
.sub {{ margin-top:2px; color:#5c6670; font-size:12px; }}
.content {{ padding:20px 22px 22px; }}
.actions {{ display:grid; grid-template-columns:1fr auto; gap:8px; }}
a {{ display:inline-flex; align-items:center; justify-content:center; min-height:38px; padding:0 16px; border:1px solid #cbd2d8; border-radius:6px; color:#16191d; text-decoration:none; font-size:13px; font-weight:600; }}
a.primary {{ border-color:#bd4b2d; background:#bd4b2d; color:white; }}
a:hover {{ border-color:#278075; }}
#status {{ min-height:20px; margin:12px 0 0; color:#4f6268; font-size:12px; }}
#status.running {{ color:#286b63; }}
#status.error {{ color:#b33d2e; }}
.note {{ margin-top:16px; padding-top:14px; border-top:1px solid #e3e7ea; color:#737d86; font-size:11px; line-height:1.6; }}
</style>
</head>
<body>
<main>
  <header>{icon}<div><h1>Mklink AI Probe</h1><div class="sub">Windows / macOS / Linux 通用 Web 启动入口</div></div></header>
  <div class="content">
    <div class="actions">
      <a class="primary" href="{SCHEME}://web/start" data-action="start">立即启动 Web GUI</a>
      <a href="{SCHEME}://web/stop" data-action="stop">停止服务</a>
    </div>
    <div id="status" role="status">即将自动启动 Web GUI...</div>
    <div class="note">首次使用时请允许浏览器打开 Mklink AI Probe。此文件不包含程序，也不会从 U 盘执行脚本。</div>
  </div>
</main>
<script>
var launchInterval = null;
var launchTimeout = null;
var autoLaunchTimer = null;
var autoLaunchDelaySeconds = {AUTO_LAUNCH_DELAY_SECONDS};
var launchTimeoutSeconds = {LAUNCH_PAGE_TIMEOUT_SECONDS};
var startUri = '{SCHEME}://web/start';

function clearLaunchTimers() {{
  if (autoLaunchTimer !== null) window.clearTimeout(autoLaunchTimer);
  if (launchInterval !== null) window.clearInterval(launchInterval);
  if (launchTimeout !== null) window.clearTimeout(launchTimeout);
  autoLaunchTimer = null;
  launchInterval = null;
  launchTimeout = null;
}}

function startLaunchCountdown() {{
  clearLaunchTimers();
  var remaining = launchTimeoutSeconds;
  var status = document.getElementById('status');
  status.className = 'running';
  status.textContent = '正在启动本地服务，最长约 ' + remaining + ' 秒，完成后会自动打开 Web 客户端...';
  launchInterval = window.setInterval(function() {{
    remaining -= 1;
    if (remaining > 0) {{
      status.textContent = '正在启动本地服务，最长约 ' + remaining + ' 秒，完成后会自动打开 Web 客户端...';
    }}
  }}, 1000);
  launchTimeout = window.setTimeout(function() {{
    clearLaunchTimers();
    status.className = 'error';
    status.textContent = '启动超时：请确认浏览器已允许打开 Mklink 启动器。仍无法启动时，请让 AI 从官方仓库安装或更新完整 MKLink AI Probe Skill，并检查 Web GUI 与 MCP 依赖。';
  }}, launchTimeoutSeconds * 1000);
}}

function requestLaunch() {{
  startLaunchCountdown();
  window.location.href = startUri;
}}

function scheduleAutomaticLaunch() {{
  var remaining = autoLaunchDelaySeconds;
  var status = document.getElementById('status');
  status.textContent = remaining + ' 秒后自动启动 Web GUI...';
  var countdown = window.setInterval(function() {{
    remaining -= 1;
    if (remaining > 0) status.textContent = remaining + ' 秒后自动启动 Web GUI...';
    else window.clearInterval(countdown);
  }}, 1000);
  autoLaunchTimer = window.setTimeout(requestLaunch, autoLaunchDelaySeconds * 1000);
}}

document.querySelectorAll('[data-action]').forEach(function(link) {{
  link.addEventListener('click', function() {{
    if (link.dataset.action === 'stop') {{
      clearLaunchTimers();
      var status = document.getElementById('status');
      status.className = '';
      status.textContent = '已请求停止由此入口启动的 Web 服务。';
      return;
    }}
    clearLaunchTimers();
    startLaunchCountdown();
  }});
}});
window.addEventListener('DOMContentLoaded', scheduleAutomaticLaunch);
</script>
</body>
</html>
"""


def write_launcher_html(
    output: Path | str,
    *,
    icon_data_uri: str | None = None,
) -> Path:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(render_launcher_html(icon_data_uri=icon_data_uri).encode("utf-8"))
    return output


def desktop_directory(
    *,
    system: str | None = None,
    environment: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    import platform

    system = system or platform.system()
    environment = environment if environment is not None else os.environ
    home = home or Path.home()
    if system == "Windows":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value = str(winreg.QueryValueEx(key, "Desktop")[0])
            return Path(os.path.expandvars(value))
        except OSError:
            return Path(environment.get("USERPROFILE", str(home))) / "Desktop"
    if system == "Linux":
        configured = environment.get("XDG_DESKTOP_DIR", "").strip()
        if configured:
            return Path(os.path.expandvars(configured.replace("$HOME", str(home))))
    return home / "Desktop"


def find_microkeen_disks(
    *,
    system: str | None = None,
    environment: dict[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Find writable probe volumes without accepting unrelated removable disks."""
    import platform

    system = system or platform.system()
    environment = environment if environment is not None else os.environ
    home = home or Path.home()
    candidates: list[Path] = []
    if system == "Windows":
        import string

        from mklink.discovery import _windows_volume_label

        configured = environment.get("MKLINK_MICROKEEN_DISK", "").strip()
        roots = ([configured.rstrip("\\/") + "\\"] if configured else []) + [
            f"{letter}:\\" for letter in string.ascii_uppercase
        ]
        for root in roots:
            if (
                root not in {str(path) for path in candidates}
                and Path(root).is_dir()
                and (_windows_volume_label(root) or "").casefold() == "microkeen"
            ):
                candidates.append(Path(root))
    elif system == "Darwin":
        candidates = [Path("/Volumes/MICROKEEN")]
    elif system == "Linux":
        candidates = [
            Path("/media") / home.name / "MICROKEEN",
            Path("/run/media") / home.name / "MICROKEEN",
        ]
    return [path for path in candidates if path.is_dir() and os.access(path, os.W_OK)]


def write_quick_launchers(
    *,
    find_probe_disks: Callable[[], list[Path]] = find_microkeen_disks,
    desktop: Path | None = None,
    icon_data_uri: str | None = None,
) -> list[Path]:
    destinations = [Path(path) for path in find_probe_disks()]
    if not destinations:
        destinations = [Path(desktop or desktop_directory())]
    written = []
    for destination in destinations:
        try:
            written.append(write_launcher_html(
                destination / QUICK_LAUNCH_FILE_NAME,
                icon_data_uri=icon_data_uri,
            ).resolve())
        except OSError:
            continue
    if written:
        return written
    fallback = Path(desktop or desktop_directory()) / QUICK_LAUNCH_FILE_NAME
    return [write_launcher_html(fallback, icon_data_uri=icon_data_uri).resolve()]


def gui_server_command(
    *,
    port: int,
    executable: Path | None = None,
    repository_root: Path | None = None,
    project_root: Path | None = None,
) -> list[str]:
    executable = executable or Path(sys.executable)
    repository_root = repository_root or globals()["repository_root"]()
    project_root = project_root or Path(".")
    if getattr(sys, "frozen", False):
        command = [str(executable), "gui"]
    else:
        command = [str(executable), "-m", "mklink", "gui"]
    return command + [
        "--host", "127.0.0.1",
        "--port", str(port),
        "--no-browser",
        "--browser-session-timeout", "15",
        "--project-root", str(project_root),
    ]


def probe_server(port: int, *, timeout: float = 0.6) -> str | None:
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("status") != "ok":
            return None
    except (OSError, ValueError, urllib.error.URLError):
        return None
    try:
        with urllib.request.urlopen(base + "/", timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            body = response.read(4096).decode("utf-8", errors="replace").lower()
        if content_type == "text/html" and ("<html" in body or "<!doctype" in body):
            return "web"
    except (OSError, urllib.error.URLError):
        pass
    return "api"


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILE_NAME


def _load_state(data_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _save_state(data_dir: Path, state: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = _state_path(data_dir)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def _clear_state(data_dir: Path) -> None:
    try:
        _state_path(data_dir).unlink()
    except FileNotFoundError:
        pass


def spawn_gui_process(
    command: list[str],
    *,
    cwd: Path,
) -> subprocess.Popen:
    options: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | 0x00000008  # DETACHED_PROCESS
        )
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def terminate_owned_process(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def get_process_identity(pid: int, *, system: str | None = None) -> str | None:
    """Return a process creation identity so a recycled PID is never killed."""
    import platform

    if pid <= 0:
        return None
    system = system or platform.system()
    if system == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            class FileTime(ctypes.Structure):
                _fields_ = [
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD),
                ]

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(FileTime),
                ctypes.POINTER(FileTime),
                ctypes.POINTER(FileTime),
                ctypes.POINTER(FileTime),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                creation = FileTime()
                exit_time = FileTime()
                kernel = FileTime()
                user = FileTime()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                return f"windows:{value}"
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return None
    if system == "Linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 2:].split()
            return f"linux:{fields[19]}"
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return f"{system.lower()}:{value}" if result.returncode == 0 and value else None


def start_web_entry(
    *,
    data_dir: Path | None = None,
    preferred_port: int = DEFAULT_PORT,
    probe: Callable[[int], str | None] = probe_server,
    port_available: Callable[[int], bool] = port_available,
    spawn: Callable[..., Any] = spawn_gui_process,
    terminate: Callable[[int], None] = terminate_owned_process,
    browser_open: Callable[[str], Any] = webbrowser.open,
    process_identity: Callable[[int], str | None] = get_process_identity,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = WEB_START_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    data_dir = Path(data_dir or platform_data_dir())
    state = _load_state(data_dir)
    if state and state.get("owned") is True:
        state_port = int(state.get("port", 0) or 0)
        state_pid = int(state.get("pid", 0) or 0)
        saved_identity = state.get("process_identity")
        current_identity = process_identity(state_pid)
        if (
            state_port
            and saved_identity
            and current_identity == saved_identity
            and probe(state_port) == "web"
        ):
            browser_open(web_entry_url(state_port))
            return {
                "status": "reused",
                "port": state_port,
                "owned": True,
                "pid": state_pid,
            }
        _clear_state(data_dir)

    selected_port = None
    for port in range(preferred_port, preferred_port + MAX_PORT_ATTEMPTS):
        detected = probe(port)
        if detected == "web":
            browser_open(web_entry_url(port))
            return {"status": "reused", "port": port, "owned": False}
        if detected == "api":
            # An API-only service still occupies the port.  Keep it untouched
            # and continue scanning so the Web GUI can start on the next port.
            continue
        if selected_port is None and port_available(port):
            selected_port = port
    if selected_port is None:
        raise WebEntryError("No local port is available for MKLink Web")

    root = repository_root()
    index = root / "gui" / "dist" / "index.html"
    if not index.is_file():
        raise WebEntryError(
            "MKLink Web assets are missing; reinstall the complete skill/runtime"
        )
    runtime_project_root = data_dir / "workspace"
    runtime_project_root.mkdir(parents=True, exist_ok=True)
    command = gui_server_command(
        port=selected_port,
        repository_root=root,
        project_root=runtime_project_root,
    )
    process = spawn(command, cwd=root)
    pid = int(process.pid)
    identity = process_identity(pid)
    if not identity:
        terminate(pid)
        raise WebEntryError("Unable to verify the started MKLink Web process")
    _save_state(data_dir, {
        "version": 1,
        "owned": True,
        "pid": pid,
        "process_identity": identity,
        "port": selected_port,
        "started_at": time.time(),
        "repository_root": str(root),
        "project_root": str(runtime_project_root),
    })

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(selected_port) == "web":
            url = web_entry_url(selected_port, root=root)
            browser_open(url)
            return {
                "status": "started",
                "port": selected_port,
                "owned": True,
                "pid": pid,
            }
        sleep(0.15)

    terminate(pid)
    _clear_state(data_dir)
    raise WebEntryError("MKLink Web service did not become ready")


def stop_web_entry(
    *,
    data_dir: Path | None = None,
    terminate: Callable[[int], None] = terminate_owned_process,
    process_identity: Callable[[int], str | None] = get_process_identity,
) -> dict[str, Any]:
    data_dir = Path(data_dir or platform_data_dir())
    state = _load_state(data_dir)
    if not state:
        return {"status": "not_running"}
    if state.get("owned") is not True:
        _clear_state(data_dir)
        return {"status": "not_owned", "port": int(state.get("port", 0) or 0)}
    pid = int(state.get("pid", 0) or 0)
    port = int(state.get("port", 0) or 0)
    saved_identity = state.get("process_identity")
    if not saved_identity or process_identity(pid) != saved_identity:
        _clear_state(data_dir)
        return {"status": "stale", "port": port, "pid": pid}
    terminate(pid)
    _clear_state(data_dir)
    return {"status": "stopped", "port": port, "pid": pid}


def web_entry_status(
    *,
    data_dir: Path | None = None,
    process_identity: Callable[[int], str | None] = get_process_identity,
) -> dict[str, Any]:
    data_dir = Path(data_dir or platform_data_dir())
    state = _load_state(data_dir)
    if state and state.get("owned") is True:
        port = int(state.get("port", 0) or 0)
        pid = int(state.get("pid", 0) or 0)
        saved_identity = state.get("process_identity")
        if not saved_identity or process_identity(pid) != saved_identity:
            _clear_state(data_dir)
            return {"status": "stopped", "port": port, "pid": pid, "owned": False}
        detected = probe_server(port) if port else None
        if detected == "web":
            return {"status": "running", "port": port, "pid": pid, "owned": True}
        _clear_state(data_dir)
        return {"status": "stopped", "port": port, "pid": pid, "owned": False}
    detected = probe_server(DEFAULT_PORT)
    if detected == "web":
        return {"status": "running", "port": DEFAULT_PORT, "owned": False}
    return {"status": "stopped", "owned": False}


def protocol_python_executable() -> Path:
    executable = Path(sys.executable)
    if sys.prefix == sys.base_prefix:
        executable = executable.resolve()
    else:
        # Resolving a venv interpreter symlink would escape the environment.
        executable = executable.absolute()
    if os.name == "nt" and executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            return pythonw
    return executable


def _write_handler(data_dir: Path, skill_root: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = data_dir / "handler.py"
    handler.write_text(render_handler_script(skill_root), encoding="utf-8")
    return handler


@contextmanager
def _operation_lock(
    data_dir: Path,
    *,
    timeout: float = 30.0,
    stale_after: float = 60.0,
):
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / LOCK_FILE_NAME
    token = f"{os.getpid()}:{time.time_ns()}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > stale_after
            except FileNotFoundError:
                continue
            if stale:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise WebEntryError("Another MKLink Web entry operation is still running")
            time.sleep(0.1)
            continue
        try:
            os.write(descriptor, token.encode("ascii"))
        finally:
            os.close(descriptor)
        break
    try:
        yield
    finally:
        try:
            if lock.read_text(encoding="ascii") == token:
                lock.unlink()
        except OSError:
            pass


def _install_windows_protocol(
    python_executable: Path,
    handler: Path,
) -> Path:
    import winreg

    base = rf"Software\Classes\{SCHEME}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
            owner = winreg.QueryValueEx(key, WINDOWS_OWNER_VALUE)[0]
    except FileNotFoundError:
        owner = None
    except OSError:
        owner = ""
    if owner not in {None, WINDOWS_OWNER_ID}:
        raise WebEntryError(
            f"The {SCHEME} protocol is already registered by another application"
        )
    command = windows_registry_command(python_executable, handler)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{HANDLER_NAME}")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.SetValueEx(key, "FriendlyTypeName", 0, winreg.REG_SZ, WINDOWS_FRIENDLY_NAME)
        winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, WINDOWS_FRIENDLY_NAME)
        winreg.SetValueEx(key, WINDOWS_OWNER_VALUE, 0, winreg.REG_SZ, WINDOWS_OWNER_ID)
        winreg.SetValueEx(
            key, WINDOWS_HANDLER_VALUE, 0, winreg.REG_SZ, str(handler.resolve()),
        )
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\Application") as key:
        winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, WINDOWS_FRIENDLY_NAME)
        winreg.SetValueEx(key, "ApplicationDescription", 0, winreg.REG_SZ, WINDOWS_DESCRIPTION)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(python_executable))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    return Path(base)


def _install_macos_protocol(
    python_executable: Path,
    handler: Path,
    *,
    home: Path,
    runner: Callable[..., Any],
    compiler: Path,
) -> Path:
    app = home / "Applications" / f"{HANDLER_NAME}.app"
    if not compiler.is_file():
        raise WebEntryError("macOS osacompile is unavailable; install the system developer tools")
    if app.exists():
        shutil.rmtree(app)
    completed = runner(
        [str(compiler), "-o", str(app), "-e", render_macos_applescript(
            python_executable, handler,
        )],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    if getattr(completed, "returncode", 0) != 0:
        detail = str(getattr(completed, "stderr", "") or "").strip()
        raise WebEntryError(f"macOS protocol launcher compilation failed: {detail}")
    contents = app / "Contents"
    info_path = contents / "Info.plist"
    if not info_path.is_file():
        raise WebEntryError("macOS protocol launcher was not created")
    try:
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as error:
        raise WebEntryError(f"macOS protocol launcher metadata is invalid: {error}") from error
    info.update(macos_info_plist())
    with info_path.open("wb") as stream:
        plistlib.dump(info, stream)
    register = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if register.is_file():
        runner([str(register), "-f", str(app)], check=False)
    return app


def _install_linux_protocol(
    python_executable: Path,
    handler: Path,
    *,
    home: Path,
    environment: dict[str, str],
    runner: Callable[..., Any],
) -> Path:
    applications = Path(
        environment.get("XDG_DATA_HOME", str(home / ".local" / "share"))
    ) / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    desktop = applications / "mklink-ai-probe-web.desktop"
    desktop.write_text(
        render_linux_desktop_entry(python_executable, handler),
        encoding="utf-8",
    )
    desktop.chmod(0o755)
    if shutil.which("xdg-mime"):
        runner([
            "xdg-mime", "default", desktop.name,
            f"x-scheme-handler/{SCHEME}",
        ], check=False)
    if shutil.which("update-desktop-database"):
        runner(["update-desktop-database", str(applications)], check=False)
    return desktop


def install_protocol(
    *,
    system: str | None = None,
    data_dir: Path | None = None,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
    python_executable: Path | None = None,
    macos_compiler: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, str]:
    import platform

    system = system or platform.system()
    home = home or Path.home()
    environment = environment if environment is not None else os.environ
    data_dir = Path(data_dir or platform_data_dir(
        system=system, environment=environment, home=home,
    ))
    handler = _write_handler(data_dir, repository_root())
    python_executable = python_executable or protocol_python_executable()
    if system == "Windows":
        registration = _install_windows_protocol(python_executable, handler)
    elif system == "Darwin":
        registration = _install_macos_protocol(
            python_executable,
            handler,
            home=home,
            runner=runner,
            compiler=macos_compiler or Path("/usr/bin/osacompile"),
        )
    elif system == "Linux":
        registration = _install_linux_protocol(
            python_executable,
            handler,
            home=home,
            environment=environment,
            runner=runner,
        )
    else:
        raise WebEntryError(f"Unsupported platform: {system}")
    return {
        "status": "installed",
        "scheme": SCHEME,
        "handler": str(handler),
        "registration": str(registration),
    }


def install_quick_launcher(
    *,
    desktop: Path | None = None,
) -> dict[str, Any]:
    requirements = check_web_entry_requirements()
    if not requirements["ready"]:
        install_hint = f'python -m pip install -e "{repository_root()}[gui,mcp]"'
        raise WebEntryError(
            "Missing complete MKLink requirements: "
            + ", ".join(requirements["missing"])
            + f". Install with: {install_hint}"
        )
    result: dict[str, Any] = dict(install_protocol())
    result["requirements"] = requirements
    result["html"] = [
        str(path.resolve())
        for path in write_quick_launchers(desktop=desktop)
    ]
    return result


def _delete_windows_registry_tree(path: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_windows_registry_tree(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def _windows_protocol_owned_by(handler: Path) -> bool:
    import winreg

    base = rf"Software\Classes\{SCHEME}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
            owner = winreg.QueryValueEx(key, WINDOWS_OWNER_VALUE)[0]
            registered_handler = winreg.QueryValueEx(key, WINDOWS_HANDLER_VALUE)[0]
    except OSError:
        return False
    return (
        owner == WINDOWS_OWNER_ID
        and os.path.normcase(os.path.abspath(registered_handler))
        == os.path.normcase(os.path.abspath(str(handler.resolve())))
    )


def uninstall_protocol(
    *,
    system: str | None = None,
    data_dir: Path | None = None,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, str]:
    import platform

    system = system or platform.system()
    home = home or Path.home()
    environment = environment if environment is not None else os.environ
    data_dir = Path(data_dir or platform_data_dir(
        system=system, environment=environment, home=home,
    ))
    stop_web_entry(data_dir=data_dir)
    if system == "Windows":
        if _windows_protocol_owned_by(data_dir / "handler.py"):
            _delete_windows_registry_tree(rf"Software\Classes\{SCHEME}")
    elif system == "Darwin":
        app = home / "Applications" / f"{HANDLER_NAME}.app"
        if app.exists():
            shutil.rmtree(app)
    elif system == "Linux":
        applications = Path(
            environment.get("XDG_DATA_HOME", str(home / ".local" / "share"))
        ) / "applications"
        desktop = applications / "mklink-ai-probe-web.desktop"
        try:
            desktop.unlink()
        except FileNotFoundError:
            pass
        if shutil.which("update-desktop-database"):
            runner(["update-desktop-database", str(applications)], check=False)
    else:
        raise WebEntryError(f"Unsupported platform: {system}")
    try:
        shutil.rmtree(data_dir)
    except FileNotFoundError:
        pass
    return {"status": "uninstalled", "scheme": SCHEME}


def handle_protocol_uri(uri: str) -> dict[str, Any]:
    action = parse_protocol_uri(uri)
    data_dir = platform_data_dir()
    with _operation_lock(data_dir):
        if action in {"start", "open"}:
            return start_web_entry(data_dir=data_dir)
        return stop_web_entry(data_dir=data_dir)


def _show_error(message: str) -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, HANDLER_NAME, 0x10)
            return
        except Exception:
            pass
    if sys.platform == "darwin" and shutil.which("osascript"):
        subprocess.run([
            "osascript", "-e",
            "on run argv", "-e",
            f'display alert "{HANDLER_NAME}" message (item 1 of argv) as critical',
            "-e", "end run", message,
        ], check=False)
        return
    if shutil.which("zenity"):
        subprocess.run([
            "zenity", "--error", f"--title={HANDLER_NAME}", f"--text={message}",
        ], check=False)


def protocol_handler_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        _show_error("Missing MKLink Web entry URI")
        return 2
    try:
        handle_protocol_uri(argv[0])
    except Exception as exc:
        _show_error(str(exc))
        return 1
    return 0
