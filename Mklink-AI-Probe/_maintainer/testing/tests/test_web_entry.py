import json
import os
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from mklink import web_entry


def test_protocol_python_executable_keeps_venv_interpreter(tmp_path, monkeypatch):
    executable = tmp_path / ".venv" / "bin" / "python"
    monkeypatch.setattr(web_entry.sys, "executable", str(executable))
    monkeypatch.setattr(web_entry.sys, "prefix", str(executable.parent.parent))
    monkeypatch.setattr(web_entry.sys, "base_prefix", str(tmp_path / "base"))

    def reject_resolve(_path, *_args, **_kwargs):
        raise AssertionError("venv interpreter must not be resolved")

    monkeypatch.setattr(Path, "resolve", reject_resolve)

    assert web_entry.protocol_python_executable() == executable.absolute()


def test_protocol_python_executable_resolves_base_interpreter(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "python"
    resolved = tmp_path / "real" / "python"
    monkeypatch.setattr(web_entry.sys, "executable", str(executable))
    monkeypatch.setattr(web_entry.sys, "prefix", str(tmp_path / "base"))
    monkeypatch.setattr(web_entry.sys, "base_prefix", str(tmp_path / "base"))

    monkeypatch.setattr(Path, "resolve", lambda _path, *_args, **_kwargs: resolved)

    assert web_entry.protocol_python_executable() == resolved


def test_protocol_uri_accepts_only_the_web_entry_actions():
    assert web_entry.parse_protocol_uri("mklink-ai-probe://web/start") == "start"
    assert web_entry.parse_protocol_uri("mklink-ai-probe://web/open") == "open"
    assert web_entry.parse_protocol_uri("mklink-ai-probe://web/stop") == "stop"

    for uri in (
        "https://example.com/web/start",
        "mklink-ai-probe://shell/start",
        "mklink-ai-probe://web/run?command=calc",
        "mklink-ai-probe://web/../../start",
    ):
        with pytest.raises(web_entry.WebEntryError):
            web_entry.parse_protocol_uri(uri)


def test_launcher_html_is_one_offline_file_with_cross_platform_protocol_links(tmp_path):
    output = tmp_path / "MKLink-Web.html"

    web_entry.write_launcher_html(output, icon_data_uri="data:image/png;base64,AA==")

    html = output.read_text(encoding="utf-8")
    assert "mklink-ai-probe://web/start" in html
    assert "mklink-ai-probe://web/stop" in html
    assert "data:image/png;base64,AA==" in html
    assert "autoLaunchDelaySeconds = 3" in html
    assert "launchTimeoutSeconds = 50" in html
    assert "scheduleAutomaticLaunch" in html
    assert "window.location.href = startUri" in html
    assert "启动超时" in html
    assert "让 AI 从官方仓库安装或更新完整 MKLink AI Probe Skill" in html
    assert "Web GUI 与 MCP 依赖" in html
    assert "http://" not in html
    assert "https://" not in html
    assert list(tmp_path.iterdir()) == [output]


def test_launcher_html_is_byte_identical_for_every_user(tmp_path):
    first = tmp_path / "first.html"
    second = tmp_path / "second.html"

    web_entry.write_launcher_html(first, icon_data_uri="data:image/png;base64,AA==")
    web_entry.write_launcher_html(second, icon_data_uri="data:image/png;base64,AA==")

    assert first.read_bytes() == second.read_bytes()
    assert b"\r\n" not in first.read_bytes()


def test_quick_launcher_prefers_microkeen_and_falls_back_to_desktop(tmp_path):
    disk = tmp_path / "MICROKEEN"
    desktop = tmp_path / "Desktop"
    disk.mkdir()

    on_probe = web_entry.write_quick_launchers(
        find_probe_disks=lambda: [disk],
        desktop=desktop,
        icon_data_uri="",
    )
    assert on_probe == [(disk / web_entry.QUICK_LAUNCH_FILE_NAME).resolve()]
    assert on_probe[0].is_file()

    without_probe = web_entry.write_quick_launchers(
        find_probe_disks=lambda: [],
        desktop=desktop,
        icon_data_uri="",
    )
    assert without_probe == [(desktop / web_entry.QUICK_LAUNCH_FILE_NAME).resolve()]
    assert without_probe[0].is_file()


def test_web_entry_requirements_cover_gui_mcp_and_assets(tmp_path):
    available = {
        "serial", "pymodbus", "elftools", "pycparser", "websockets",
        "fastapi", "starlette", "uvicorn", "pyocd", "intelhex", "multipart",
        "fastmcp", "pydantic",
    }
    (tmp_path / "gui" / "dist").mkdir(parents=True)
    (tmp_path / "gui" / "dist" / "index.html").write_text("ok", encoding="utf-8")

    ready = web_entry.check_web_entry_requirements(
        root=tmp_path,
        module_available=lambda name: name in available,
    )
    assert ready == {"ready": True, "missing": []}

    missing = web_entry.check_web_entry_requirements(
        root=tmp_path,
        module_available=lambda name: name not in {"fastmcp", "pyocd"},
    )
    assert missing["ready"] is False
    assert "mcp:fastmcp" in missing["missing"]
    assert "gui:pyocd" in missing["missing"]

    (tmp_path / "gui" / "dist" / "index.html").unlink()
    no_assets = web_entry.check_web_entry_requirements(
        root=tmp_path,
        module_available=lambda _name: True,
    )
    assert "gui:built-web-assets" in no_assets["missing"]


def test_quick_install_rejects_missing_dependencies_before_registration(tmp_path, monkeypatch):
    registered = []
    monkeypatch.setattr(
        web_entry,
        "check_web_entry_requirements",
        lambda: {"ready": False, "missing": ["gui:pyocd", "mcp:fastmcp"]},
    )
    monkeypatch.setattr(
        web_entry,
        "install_protocol",
        lambda: registered.append(True) or {"status": "installed"},
    )

    with pytest.raises(web_entry.WebEntryError, match=r"\[gui,mcp\]"):
        web_entry.install_quick_launcher(desktop=tmp_path)

    assert registered == []


def test_quick_install_registers_protocol_and_reports_launcher(tmp_path, monkeypatch):
    launcher = tmp_path / web_entry.QUICK_LAUNCH_FILE_NAME
    monkeypatch.setattr(
        web_entry,
        "check_web_entry_requirements",
        lambda: {"ready": True, "missing": []},
    )
    monkeypatch.setattr(
        web_entry,
        "install_protocol",
        lambda: {"status": "installed", "scheme": web_entry.SCHEME},
    )
    monkeypatch.setattr(web_entry, "write_quick_launchers", lambda **_kwargs: [launcher])

    result = web_entry.install_quick_launcher(desktop=tmp_path)

    assert result["status"] == "installed"
    assert result["requirements"] == {"ready": True, "missing": []}
    assert result["html"] == [str(launcher.resolve())]


@pytest.mark.parametrize(
    ("system", "environment", "home", "suffix"),
    [
        ("Windows", {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}, Path("C:/Users/test"), "Mklink AI Probe/web-entry"),
        ("Darwin", {}, Path("/Users/test"), "Library/Application Support/Mklink AI Probe/web-entry"),
        ("Linux", {"XDG_DATA_HOME": "/home/test/.data"}, Path("/home/test"), "mklink-ai-probe/web-entry"),
    ],
)
def test_platform_data_directory_is_user_scoped(system, environment, home, suffix):
    path = web_entry.platform_data_dir(system=system, environment=environment, home=home)
    assert str(path).replace("\\", "/").endswith(suffix)


def test_generated_handler_pins_the_installed_skill_root(tmp_path):
    script = web_entry.render_handler_script(tmp_path / "skill root")

    assert repr(str((tmp_path / "skill root").resolve())) in script
    assert "protocol_handler_main" in script
    assert "sys.path.insert" in script


def test_linux_desktop_handler_uses_the_same_uri_scheme(tmp_path):
    desktop = web_entry.render_linux_desktop_entry(
        Path("/opt/mklink/python3"), tmp_path / "handler.py",
    )

    assert "MimeType=x-scheme-handler/mklink-ai-probe;" in desktop
    assert "Terminal=false" in desktop
    assert "%u" in desktop


def test_linux_desktop_handler_escapes_field_codes_and_special_characters(tmp_path):
    desktop = web_entry.render_linux_desktop_entry(
        PurePosixPath("/opt/MKLink $runtime/python3"),
        PurePosixPath("/home/user/100% handler.py"),
    )

    assert '"/opt/MKLink \\$runtime/python3"' in desktop
    assert "100%% handler.py" in desktop


def test_macos_info_plist_registers_the_same_uri_scheme():
    document = web_entry.macos_info_plist()

    assert document["CFBundleIdentifier"] == "com.microkeen.mklink-ai-probe.web-entry"
    schemes = document["CFBundleURLTypes"][0]["CFBundleURLSchemes"]
    assert schemes == ["mklink-ai-probe"]


def test_macos_launcher_handles_url_apple_events_and_shell_quotes_paths(tmp_path):
    script = web_entry.render_macos_applescript(
        PurePosixPath("/Users/O'Brien/MKLink $runtime/python3"),
        PurePosixPath("/Users/test/handler.py"),
    )

    assert "on open location theURL" in script
    assert "quoted form of theURL" in script
    assert "MKLink $runtime/python3" in script
    assert "/Users/test/handler.py" in script
    assert '\\"' in script


def test_windows_registry_command_uses_an_absolute_handler_and_quoted_uri(tmp_path):
    command = web_entry.windows_registry_command(
        Path(r"C:\Python\pythonw.exe"), tmp_path / "handler.py",
    )

    assert "pythonw.exe" in command
    assert str(tmp_path / "handler.py") in command
    assert '"%1"' in command


def test_gui_server_command_reuses_the_existing_cli_without_touching_mcp_or_serve(tmp_path):
    executable = Path("python3")
    command = web_entry.gui_server_command(
        port=8771,
        executable=executable,
        repository_root=tmp_path,
        project_root=tmp_path / "runtime workspace",
    )

    assert command[:4] == [str(executable), "-m", "mklink", "gui"]
    assert "--no-browser" in command
    assert command[command.index("--browser-session-timeout") + 1] == "15"
    assert "--port" in command and "8771" in command
    assert command[command.index("--project-root") + 1] == str(tmp_path / "runtime workspace")
    assert "serve" not in command
    assert "mcp" not in command


def test_web_entry_url_changes_with_the_frontend_build(tmp_path):
    dist = tmp_path / "gui" / "dist"
    dist.mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("old", encoding="utf-8")
    old_url = web_entry.web_entry_url(8765, root=tmp_path)

    index.write_text("new", encoding="utf-8")
    new_url = web_entry.web_entry_url(8765, root=tmp_path)

    assert old_url.startswith("http://127.0.0.1:8765/?build=")
    assert old_url.endswith("#/config")
    assert new_url != old_url


def test_start_reuses_an_existing_web_server_without_spawning_or_owning_it(tmp_path):
    spawned = []
    opened = []

    result = web_entry.start_web_entry(
        data_dir=tmp_path,
        probe=lambda port: "web" if port == 8765 else None,
        port_available=lambda _port: False,
        spawn=lambda *_args, **_kwargs: spawned.append(True),
        browser_open=opened.append,
    )

    assert result == {"status": "reused", "port": 8765, "owned": False}
    assert spawned == []
    assert opened == [web_entry.web_entry_url(8765)]
    assert not (tmp_path / "state.json").exists()


def test_start_scans_the_port_range_before_starting_a_competing_backend(tmp_path):
    opened = []

    result = web_entry.start_web_entry(
        data_dir=tmp_path,
        probe=lambda port: "web" if port == 8766 else None,
        port_available=lambda port: port == 8765,
        spawn=lambda *_args, **_kwargs: pytest.fail("must reuse the existing Web service"),
        browser_open=opened.append,
    )

    assert result == {"status": "reused", "port": 8766, "owned": False}
    assert opened == [web_entry.web_entry_url(8766)]


def test_start_skips_a_running_api_without_web_assets_and_uses_next_port(tmp_path):
    opened = []
    commands = []

    def probe(port):
        if port == 8765:
            return "api"
        return "web" if port == 8766 else None

    result = web_entry.start_web_entry(
        data_dir=tmp_path,
        probe=probe,
        port_available=lambda port: port == 8766,
        spawn=lambda *args, **kwargs: commands.append((args, kwargs)),
        browser_open=opened.append,
    )

    assert result == {"status": "reused", "port": 8766, "owned": False}
    assert commands == []
    assert opened == [web_entry.web_entry_url(8766)]


def test_start_spawns_web_gui_after_api_only_port(tmp_path):
    opened = []
    commands = []

    def probe(port):
        return "api" if port == 8765 else "web" if len(commands) else None

    def spawn(*args, **kwargs):
        commands.append((args, kwargs))
        return SimpleNamespace(pid=4321)

    result = web_entry.start_web_entry(
        data_dir=tmp_path,
        probe=probe,
        port_available=lambda port: port == 8766,
        spawn=spawn,
        browser_open=opened.append,
        process_identity=lambda pid: f"process-{pid}",
        sleep=lambda _seconds: None,
        timeout=1,
    )

    assert result == {"status": "started", "port": 8766, "owned": True, "pid": 4321}
    assert commands and "8766" in commands[0][0][0]
    assert opened == [web_entry.web_entry_url(8766)]


def test_start_spawns_one_owned_gui_and_stop_only_terminates_that_pid(tmp_path):
    probes = {8765: [None, None, "web"]}
    terminated = []
    opened = []
    commands = []

    def probe(port):
        values = probes.get(port, [None])
        return values.pop(0) if len(values) > 1 else values[0]

    def spawn(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(pid=4321)

    result = web_entry.start_web_entry(
        data_dir=tmp_path,
        probe=probe,
        port_available=lambda port: port == 8765,
        spawn=spawn,
        browser_open=opened.append,
        sleep=lambda _seconds: None,
        timeout=1,
        process_identity=lambda pid: f"process-{pid}",
    )

    assert result == {"status": "started", "port": 8765, "owned": True, "pid": 4321}
    assert commands and "gui" in commands[0]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["pid"] == 4321 and state["owned"] is True

    stopped = web_entry.stop_web_entry(
        data_dir=tmp_path,
        terminate=terminated.append,
        process_identity=lambda pid: f"process-{pid}",
    )
    assert stopped == {"status": "stopped", "port": 8765, "pid": 4321}
    assert terminated == [4321]
    assert not (tmp_path / "state.json").exists()


def test_start_timeout_terminates_the_owned_process_and_clears_state(tmp_path):
    terminated = []

    with pytest.raises(web_entry.WebEntryError, match="did not become ready"):
        web_entry.start_web_entry(
            data_dir=tmp_path,
            probe=lambda _port: None,
            port_available=lambda port: port == 8765,
            spawn=lambda *_args, **_kwargs: SimpleNamespace(pid=4321),
            terminate=terminated.append,
            browser_open=lambda _url: pytest.fail("must not open before ready"),
            process_identity=lambda pid: f"process-{pid}",
            sleep=lambda _seconds: None,
            timeout=0,
        )

    assert terminated == [4321]
    assert not (tmp_path / "state.json").exists()


def test_stop_does_not_kill_a_reused_pid_from_stale_state(tmp_path):
    terminated = []
    (tmp_path / "state.json").write_text(json.dumps({
        "pid": 4321,
        "port": 8765,
        "owned": True,
        "process_identity": "old-process",
    }), encoding="utf-8")

    result = web_entry.stop_web_entry(
        data_dir=tmp_path,
        terminate=terminated.append,
        process_identity=lambda _pid: "new-process",
    )

    assert result == {"status": "stale", "port": 8765, "pid": 4321}
    assert terminated == []
    assert not (tmp_path / "state.json").exists()


def test_stop_never_terminates_a_reused_or_missing_service(tmp_path):
    terminated = []
    (tmp_path / "state.json").write_text(json.dumps({
        "pid": 999, "port": 8765, "owned": False,
    }), encoding="utf-8")

    result = web_entry.stop_web_entry(
        data_dir=tmp_path,
        terminate=terminated.append,
    )

    assert result["status"] == "not_owned"
    assert terminated == []


def test_status_reports_an_exited_owned_backend_as_stopped(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "pid": 4321,
        "port": 8765,
        "owned": True,
        "process_identity": "exited-process",
    }), encoding="utf-8")

    result = web_entry.web_entry_status(
        data_dir=tmp_path,
        process_identity=lambda _pid: None,
    )

    assert result == {
        "status": "stopped", "port": 8765, "pid": 4321, "owned": False,
    }
    assert not (tmp_path / "state.json").exists()


def test_status_clears_an_owned_process_that_no_longer_serves_web(tmp_path, monkeypatch):
    (tmp_path / "state.json").write_text(json.dumps({
        "pid": 4321,
        "port": 8765,
        "owned": True,
        "process_identity": "same-process",
    }), encoding="utf-8")
    monkeypatch.setattr(web_entry, "probe_server", lambda _port: None)

    result = web_entry.web_entry_status(
        data_dir=tmp_path,
        process_identity=lambda _pid: "same-process",
    )

    assert result["status"] == "stopped"
    assert result["owned"] is False
    assert not (tmp_path / "state.json").exists()


def test_protocol_handler_dispatches_start_open_and_stop(monkeypatch, tmp_path):
    starts = []
    stops = []
    monkeypatch.setattr(web_entry, "platform_data_dir", lambda: tmp_path)
    monkeypatch.setattr(web_entry, "start_web_entry", lambda **kwargs: starts.append(kwargs) or {"status": "started"})
    monkeypatch.setattr(web_entry, "stop_web_entry", lambda **kwargs: stops.append(kwargs) or {"status": "stopped"})

    assert web_entry.handle_protocol_uri("mklink-ai-probe://web/start")["status"] == "started"
    assert web_entry.handle_protocol_uri("mklink-ai-probe://web/open")["status"] == "started"
    assert web_entry.handle_protocol_uri("mklink-ai-probe://web/stop")["status"] == "stopped"
    assert len(starts) == 2
    assert len(stops) == 1
    assert starts[0]["data_dir"] == tmp_path


def test_process_identity_is_stable_for_the_current_process():
    first = web_entry.get_process_identity(os.getpid())
    second = web_entry.get_process_identity(os.getpid())

    assert first
    assert second == first
    assert web_entry.get_process_identity(-1) is None


def test_linux_install_creates_a_user_desktop_handler(tmp_path):
    result = web_entry.install_protocol(
        system="Linux",
        data_dir=tmp_path / "data",
        home=tmp_path / "home",
        environment={"XDG_DATA_HOME": str(tmp_path / "share")},
        python_executable=Path("/opt/mklink/python3"),
        runner=lambda *_args, **_kwargs: None,
    )

    desktop = Path(result["registration"])
    assert desktop == tmp_path / "share" / "applications" / "mklink-ai-probe-web.desktop"
    assert desktop.is_file()
    assert (tmp_path / "data" / "handler.py").is_file()


def test_macos_install_creates_a_user_application_bundle(tmp_path):
    compiler = tmp_path / "osacompile"
    compiler.write_text("", encoding="utf-8")

    def compile_app(command, **_kwargs):
        app = Path(command[command.index("-o") + 1])
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as stream:
            import plistlib
            plistlib.dump({"CFBundleExecutable": "applet"}, stream)
        (contents / "MacOS" / "applet").write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    result = web_entry.install_protocol(
        system="Darwin",
        data_dir=tmp_path / "data",
        home=tmp_path / "home",
        environment={},
        python_executable=Path("/opt/mklink/python3"),
        macos_compiler=compiler,
        runner=compile_app,
    )

    app = Path(result["registration"])
    assert app == tmp_path / "home" / "Applications" / "Mklink AI Probe Web Launcher.app"
    assert (app / "Contents" / "Info.plist").is_file()
    assert (app / "Contents" / "MacOS" / "applet").is_file()
    with (app / "Contents" / "Info.plist").open("rb") as stream:
        import plistlib
        info = plistlib.load(stream)
    assert info["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["mklink-ai-probe"]


def test_windows_install_writes_only_the_user_protocol_keys(tmp_path, monkeypatch):
    values = {}

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        REG_SZ=1,
        CreateKey=lambda _root, path: Key(path),
        OpenKey=lambda _root, path: (_ for _ in ()).throw(FileNotFoundError(path)),
        SetValueEx=lambda key, name, _reserved, _kind, value: values.__setitem__(
            (key.path, name), value,
        ),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    result = web_entry.install_protocol(
        system="Windows",
        data_dir=tmp_path / "data",
        home=tmp_path,
        environment={"LOCALAPPDATA": str(tmp_path / "local")},
        python_executable=Path(r"C:\Python\pythonw.exe"),
    )

    assert result["status"] == "installed"
    base = r"Software\Classes\mklink-ai-probe"
    assert values[(base, "URL Protocol")] == ""
    assert values[(base, "FriendlyTypeName")] == web_entry.WINDOWS_FRIENDLY_NAME
    assert values[(base, "ApplicationName")] == web_entry.WINDOWS_FRIENDLY_NAME
    assert values[(base + r"\Application", "ApplicationName")] == web_entry.WINDOWS_FRIENDLY_NAME
    assert values[(base + r"\Application", "ApplicationDescription")] == web_entry.WINDOWS_DESCRIPTION
    command = values[(base + r"\shell\open\command", "")]
    assert "pythonw.exe" in command
    assert str(tmp_path / "data" / "handler.py") in command
    assert '"%1"' in command


def test_windows_uninstall_preserves_a_foreign_protocol_registration(tmp_path, monkeypatch):
    deleted = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        OpenKey=lambda *_args: Key(),
        QueryValueEx=lambda _key, name: (
            ("another-product", 1)
            if name == "Mklink Web Entry Owner"
            else (str(tmp_path / "data" / "handler.py"), 1)
        ),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(
        web_entry,
        "_delete_windows_registry_tree",
        deleted.append,
    )

    result = web_entry.uninstall_protocol(
        system="Windows",
        data_dir=tmp_path / "data",
        home=tmp_path,
        environment={"LOCALAPPDATA": str(tmp_path / "local")},
    )

    assert result["status"] == "uninstalled"
    assert deleted == []
