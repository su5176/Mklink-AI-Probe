"""Executable consistency checks for the repository-owned remote documentation."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
REMOTE_DOC = ROOT / "references" / "commands-remote.md"
DOCUMENTS = (
    ROOT / "SKILL.md",
    ROOT / "references" / "triggers.md",
    ROOT / "references" / "workflows.md",
    REMOTE_DOC,
    ROOT / "references" / "commands-remote-gui.md",
)
HIGH_RISK_OPERATIONS = {
    "flash.program",
    "flash.erase_chip",
    "flash.erase_sector",
    "offline.deploy",
    "target.reset",
    "breakpoint.set",
    "breakpoint.clear",
    "breakpoint.clear_all",
    "memory.write",
    "variable.write",
    "serial.exchange",
    "modbus.write",
}
MCP_TOOLS = {
    "remote_sites",
    "remote_status",
    "remote_capabilities",
    "remote_call",
    "remote_upload",
    "remote_flash",
    "remote_write_memory",
}
FORBIDDEN_ENABLEMENT = re.compile(
    r"\bfrpc(?:\.exe)?\b"
    r"|nat[\s_-]*travers(?:al|e)"
    r"|public[\s_-]*tunnell?ing"
    r"|\bsitetunnel\b",
    re.IGNORECASE,
)
NEGATIVE_MARKERS = re.compile(
    r"\b(?:forbidden|unsupported|excluded|direct-only|must\s+not|not\s+supported"
    r"|do\s+not|without|no|never|disabled|reject(?:ed|s)?)\b"
    r"|不需要|不包含|不启动|不支持|禁止|不得",
    re.IGNORECASE,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str | None = None) -> str:
    assert start in text, f"Missing section marker: {start}"
    value = text.split(start, 1)[1]
    if end is not None:
        assert end in value, f"Missing section end marker: {end}"
        value = value.split(end, 1)[0]
    return value


def _relative_markdown_links(path: Path) -> list[Path]:
    links: list[Path] = []
    for target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", _read(path)):
        target = target.strip().split("#", 1)[0]
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        links.append((path.parent / target).resolve())
    return links


def _replace_placeholders(line: str) -> str:
    replacements = {
        "<VPN_OR_LAN_HOST>": "field.example",
        "<PORT>": "8766",
        "<READY_FILE>": "ready.json",
        "<OWNER_ONLY_TOKEN_FILE>": "token.txt",
        "<LAN_FRPS_HOST>": "frps.example",
        "<SITE_PROXY_NAME>": "field-a-proxy",
        "<LOCAL_FILE>": "artifact.bin",
        "<OPAQUE_ID>": "opaque-id",
        "<LOCAL_FIRMWARE>": "firmware.bin",
        "<TARGET_PART>": "STM32F103RC",
    }
    for placeholder, value in replacements.items():
        line = line.replace(placeholder, value)
    return line


def _documented_commands(prefix: str) -> list[str]:
    return [
        line.strip()
        for line in _read(REMOTE_DOC).splitlines()
        if line.strip() == prefix or line.strip().startswith(f"{prefix} ")
    ]


def _split_powershell(line: str) -> list[str]:
    return shlex.split(_replace_placeholders(line), posix=True)


def _remote_added_content() -> str:
    skill = _read(ROOT / "SKILL.md")
    triggers = _read(ROOT / "references" / "triggers.md")
    workflows = _read(ROOT / "references" / "workflows.md")
    gui = _read(ROOT / "references" / "commands-remote-gui.md")
    skill_routing = _section(skill, "## 按需路由")
    trigger_remote = _section(
        triggers,
        "## VPN/局域网直连远程调试",
        "## Modbus",
    )
    workflow_remote = _section(
        workflows,
        "### VPN/局域网现场机直连",
        "---",
    )
    remote_error_rows = "\n".join(
        line
        for line in workflows.splitlines()
        if any(
            marker in line
            for marker in (
                "远程 listener",
                "远程认证",
                "`health` 成功",
                "capability unavailable",
                "上传中断",
                "高风险操作",
            )
        )
    )
    gui_routing = "\n".join(gui.splitlines()[:8])
    return "\n".join(
        (
            skill_routing,
            trigger_remote,
            workflow_remote,
            remote_error_rows,
            gui_routing,
            _read(REMOTE_DOC),
        )
    )


def test_remote_intent_routes_only_to_the_direct_reference_and_local_gui_remains():
    skill = _read(ROOT / "SKILL.md")
    routing = _section(skill, "## 按需路由")
    route_lines = [
        line for line in routing.splitlines() if line.lstrip().startswith("|")
    ]
    direct_routes = [
        line for line in route_lines if "(references/commands-remote.md)" in line
    ]
    gui_routes = [
        line for line in route_lines if "(references/commands-remote-gui.md)" in line
    ]

    assert len(direct_routes) == 1
    assert "VPN/局域网" in direct_routes[0]
    assert "Site Agent" in direct_routes[0]
    assert "直连远程" in direct_routes[0]
    assert len(gui_routes) == 1
    assert "本地 Web GUI/API" in gui_routes[0]
    assert "桌面应用" in gui_routes[0]
    assert "远程调试" not in gui_routes[0]
    assert "远程烧录" not in gui_routes[0]

    triggers = _section(
        _read(ROOT / "references" / "triggers.md"),
        "## VPN/局域网直连远程调试",
        "## Modbus",
    )
    workflows = _section(
        _read(ROOT / "references" / "workflows.md"),
        "### VPN/局域网现场机直连",
        "---",
    )
    for name, section in (("triggers", triggers), ("workflows", workflows)):
        assert "(commands-remote.md)" in section, f"{name} lost direct remote routing"
        assert "commands-remote-gui.md" not in section, (
            f"{name} routes generic remote intent to the local GUI reference"
        )

    gui_header = "\n".join(
        _read(ROOT / "references" / "commands-remote-gui.md").splitlines()[:8]
    )
    assert "本地 Web 服务与 GUI" in gui_header
    assert "(commands-remote.md)" in gui_header
    gui_trigger_line = _read(
        ROOT / "references" / "commands-remote-gui.md"
    ).splitlines()[2]
    assert "远程调试" not in gui_trigger_line
    assert "远程烧录" not in gui_trigger_line


def test_all_repository_document_links_resolve_without_a_global_skill():
    root = ROOT.resolve()
    missing: list[str] = []
    outside: list[str] = []
    for document in DOCUMENTS:
        for target in _relative_markdown_links(document):
            try:
                target.relative_to(root)
            except ValueError:
                outside.append(f"{document.relative_to(ROOT)} -> {target}")
            if not target.exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")

    assert outside == [], "Documentation links escape the repository:\n" + "\n".join(
        outside
    )
    assert missing == [], "Missing documentation links:\n" + "\n".join(missing)
    assert all(".codex" not in str(path).casefold() for path in DOCUMENTS)


def test_every_documented_engineer_and_field_command_matches_real_parsers():
    from mklink.remote.cli import build_parser as build_remote_parser
    from mklink.remote.package_agent import build_parser as build_agent_parser

    engineer_commands = _documented_commands("python -m mklink remote")
    field_commands = _documented_commands(r".\mklink-remote-agent.exe")
    assert len(engineer_commands) == 17
    assert len(field_commands) == 9

    remote_parser = build_remote_parser()
    for command in engineer_commands:
        argv = _split_powershell(command)
        assert argv[:4] == ["python", "-m", "mklink", "remote"]
        parsed = remote_parser.parse_args(argv[4:])
        assert parsed.command

    agent_parser = build_agent_parser()
    parsed_lifecycle: set[str] = set()
    for command in field_commands:
        argv = _split_powershell(command)
        parsed = agent_parser.parse_args(argv[1:])
        parsed_lifecycle.add(parsed.command)
    assert parsed_lifecycle == {"start", "health", "status", "stop", "restart"}

    secure = agent_parser.parse_args(
        [
            "start",
            "--host",
            "field.example",
            "--allow-lan",
            "--token-file",
            "token.txt",
            "--ready-file",
            "ready.json",
        ]
    )
    assert secure.command == "start"
    assert secure.allow_lan is True
    assert secure.token_file == Path("token.txt")
    assert secure.ready_file == Path("ready.json")


def test_mcp_documentation_matches_the_no_flag_stdio_entry_and_exact_tools(monkeypatch):
    from mklink.remote import mcp as remote_mcp

    class FakeMcp:
        def __init__(self):
            self.tools: dict[str, Any] = {}

        def tool(self):
            def register(function):
                self.tools[function.__name__] = function
                return function

            return register

    class FakeClient:
        def supports(self, _capability: str) -> bool:
            return True

        def call(self, method: str, **params: Any) -> dict[str, Any]:
            return {"method": method, "params": params}

    class FakeRegistry:
        def list(self) -> list[dict[str, Any]]:
            return []

        def client(self, _site: str | None):
            return FakeClient()

    mcp = FakeMcp()
    remote_mcp.register_tools(mcp, FakeRegistry())
    assert set(mcp.tools) == MCP_TOOLS
    for name in ("remote_call", "remote_flash", "remote_write_memory"):
        signature = inspect.signature(mcp.tools[name])
        assert signature.parameters["confirm"].default is False

    documented_entry = _documented_commands("mklink-remote-mcp")
    assert documented_entry == ["mklink-remote-mcp"]
    assert list(inspect.signature(remote_mcp.main).parameters) == []
    assert "argparse" not in _read(ROOT / "mklink" / "remote" / "mcp.py")
    assert (
        'mklink-remote-mcp = "mklink.remote.mcp:main"'
        in _read(ROOT / "pyproject.toml")
    )

    class FakeServer:
        def __init__(self):
            self.transports: list[str] = []

        def run(self, *, transport: str) -> None:
            self.transports.append(transport)

    server = FakeServer()
    monkeypatch.setattr(remote_mcp, "build_server", lambda: server)
    assert remote_mcp.main() == 0
    assert server.transports == ["stdio"]


def test_two_machine_architecture_and_authenticated_listener_rules_match_runtime():
    from mklink.remote.agent import validate_bind
    from mklink.remote.package_agent import build_parser as build_agent_parser

    document = _read(REMOTE_DOC)
    assert "现场机 | 官方独立 Site Agent ZIP/EXE" in document
    assert "Codex、工程师 Skill、源码 checkout、全局 Python/Node/Rust 工具链" in document
    assert "工程师机 | 本 Skill、`mklink.remote` SDK、`python -m mklink remote`" in document
    assert "带身份验证的直连 `ws://<VPN_OR_LAN_HOST>:<PORT>`" in document
    assert "现场机永不读取本 Skill" in document
    assert "非回环监听必须同时满足 `--allow-lan` 和 token" in document
    assert "`--no-token` 只允许回环开发验证" in document
    packaged_entry = _read(ROOT / "packaging" / "site_agent" / "entry.py")
    assert "from mklink.remote.package_agent import main" in packaged_entry

    defaults = build_agent_parser().parse_args([])
    assert defaults.command == "start"
    assert defaults.host == "127.0.0.1"
    assert defaults.allow_lan is False

    validate_bind("127.0.0.1", None)
    with pytest.raises(ValueError, match="allow_lan=True and a token"):
        validate_bind("field.example", "token", allow_lan=False)
    with pytest.raises(ValueError, match="allow_lan=True and a token"):
        validate_bind("field.example", None, allow_lan=True)
    validate_bind("field.example", "token", allow_lan=True)
    with pytest.raises(ValueError, match="wildcard"):
        validate_bind("0.0.0.0", "token", allow_lan=True)


def test_named_sites_project_pointer_sdk_and_reconnect_documentation_match(tmp_path):
    from mklink.remote import sites
    from mklink.remote.client import RemoteClient, connect_remote

    document = _read(REMOTE_DOC)
    for phrase in (
        "sites add",
        "sites list",
        "sites use",
        ".mklink/remote.json",
        "health",
        "status",
        "capabilities",
        "ports",
        "agent.reconnect",
        "RemoteClient.reconnect()",
        "flash_timeout=300.0",
        "completion-unknown",
    ):
        assert phrase in document

    registry = sites.SiteRegistry(tmp_path / "state" / "sites.json")
    registry.add(
        "field-a",
        "ws://field.example:8766",
        "test-only-token",
        note="managed VPN",
    )
    listed = registry.list()
    assert listed == [
        {
            "name": "field-a",
            "url": "ws://field.example:8766",
            "note": "managed VPN",
            "active": True,
            "connected": False,
            "token_configured": True,
        }
    ]
    assert "token" not in listed[0]

    project = tmp_path / "project"
    project.mkdir()
    result = registry.write_project_site(project, "field-a")
    pointer = project / ".mklink" / "remote.json"
    assert result["project_file"] == str(pointer)
    assert json.loads(pointer.read_text(encoding="utf-8")) == {
        "active_site": "field-a"
    }
    assert registry.resolve_name(project_root=project) == "field-a"

    expected_signatures = {
        sites.add_site: ("name", "url", "token", "note"),
        sites.list_sites: (),
        sites.use_site: ("name", "project_root"),
        sites.get_device: ("site",),
        sites.close_all: (),
        connect_remote: ("url", "token", "timeout", "flash_timeout"),
        RemoteClient.reconnect: ("self",),
        RemoteClient.handshake: ("self",),
        RemoteClient.supports: ("self", "capability"),
    }
    for function, expected in expected_signatures.items():
        assert tuple(inspect.signature(function).parameters) == expected


def test_upload_is_atomic_abortable_and_inert_until_a_later_operation(tmp_path):
    from mklink.remote.client import RemoteClient

    payload = tmp_path / "artifact.bin"
    payload.write_bytes(b"0123456789")
    expected_sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()

    class UploadClient:
        def __init__(self, *, fail_chunk: bool = False):
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.fail_chunk = fail_chunk

        def call(self, method: str, **params: Any) -> dict[str, Any]:
            self.calls.append((method, params))
            if method == "transfer.open":
                return {"session_id": "session-identifier-12345", "chunk_limit": 3}
            if method == "transfer.chunk" and self.fail_chunk:
                raise RuntimeError("synthetic transfer failure")
            if method == "transfer.finalize":
                return {
                    "file_id": "opaque-id",
                    "name": payload.name,
                    "size": payload.stat().st_size,
                    "sha256": params["sha256"],
                }
            return {"ok": True}

    client = UploadClient()
    remote_file = RemoteClient.upload(client, payload)
    methods = [method for method, _ in client.calls]
    assert methods == [
        "transfer.open",
        "transfer.chunk",
        "transfer.chunk",
        "transfer.chunk",
        "transfer.chunk",
        "transfer.finalize",
    ]
    assert client.calls[-1][1] == {
        "session_id": "session-identifier-12345",
        "size": 10,
        "sha256": expected_sha256,
    }
    assert remote_file.reference == "remote-file:opaque-id"
    assert set(methods) <= {
        "transfer.open",
        "transfer.chunk",
        "transfer.finalize",
        "transfer.abort",
    }

    failing = UploadClient(fail_chunk=True)
    with pytest.raises(RuntimeError, match="synthetic transfer failure"):
        RemoteClient.upload(failing, payload)
    assert [method for method, _ in failing.calls][-1] == "transfer.abort"

    document = _read(REMOTE_DOC)
    assert "transfer.open` → 顺序 `transfer.chunk` →" in document
    assert "`transfer.finalize`" in document
    assert "失败会尝试 `transfer.abort`" in document
    assert "reference 是 inert 数据" in document
    assert "不会自动连接、解析、烧录或替换任何内容" in document


def test_exact_high_risk_schema_is_gated_by_cli_mcp_and_field_agent(
    monkeypatch,
    capsys,
):
    from mklink.remote import cli as remote_cli
    from mklink.remote import dispatcher, mcp as remote_mcp, sites
    from mklink.remote.capabilities import OPERATION_SCHEMAS
    from mklink.remote.protocol import RequestValidationError

    runtime_high_risk = {
        name for name, schema in OPERATION_SCHEMAS.items() if schema.high_risk
    }
    assert runtime_high_risk == HIGH_RISK_OPERATIONS

    document = _read(REMOTE_DOC)
    documented_schema = _section(
        document,
        "当前高风险 schema 是：",
        "这些 operation 的 CLI 必须带",
    )
    documented_high_risk = {
        value
        for value in re.findall(r"`([^`]+)`", documented_schema)
        if "." in value
    }
    assert documented_high_risk == HIGH_RISK_OPERATIONS

    class FakeClient:
        def __init__(self, *, terminal_flash: bool = False):
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.terminal_flash = terminal_flash

        def supports(self, _capability: str) -> bool:
            return True

        def call(self, method: str, **params: Any) -> dict[str, Any]:
            self.calls.append((method, params))
            if method == "flash.program" and self.terminal_flash:
                return {"state": "succeeded", "result": {"method": method}}
            return {"method": method}

    cli_client = FakeClient(terminal_flash=True)
    monkeypatch.setattr(remote_cli, "_client", lambda _args: cli_client)
    monkeypatch.setattr(sites, "close_all", lambda: None)
    for operation in sorted(HIGH_RISK_OPERATIONS):
        cli_client.calls.clear()
        assert remote_cli.main(["call", operation, "--params", "{}"]) == 2
        assert cli_client.calls == []
        capsys.readouterr()

        assert (
            remote_cli.main(
                ["call", operation, "--params", "{}", "--yes"]
            )
            == 0
        )
        assert cli_client.calls == [(operation, {"confirm": True})]
        capsys.readouterr()

    with pytest.raises(SystemExit):
        remote_cli.build_parser().parse_args(["stop-agent"])
    assert remote_cli.build_parser().parse_args(
        ["stop-agent", "--yes"]
    ).yes is True

    class FakeMcp:
        def __init__(self):
            self.tools: dict[str, Any] = {}

        def tool(self):
            def register(function):
                self.tools[function.__name__] = function
                return function

            return register

    class FakeRegistry:
        def __init__(self, client: FakeClient):
            self._client = client

        def list(self) -> list[dict[str, Any]]:
            return []

        def client(self, _site: str | None):
            return self._client

    mcp_client = FakeClient()
    mcp = FakeMcp()
    remote_mcp.register_tools(mcp, FakeRegistry(mcp_client))
    for operation in sorted(HIGH_RISK_OPERATIONS):
        mcp_client.calls.clear()
        with pytest.raises(ValueError, match="explicit confirmation"):
            mcp.tools["remote_call"](operation, {}, confirm=False)
        assert mcp_client.calls == []
        result = mcp.tools["remote_call"](operation, {}, confirm=True)
        assert result["method"] == operation
        assert mcp_client.calls == [(operation, {"confirm": True})]

    monkeypatch.setattr(dispatcher, "capability_available", lambda _name: True)
    for operation in sorted(HIGH_RISK_OPERATIONS):
        with pytest.raises(RequestValidationError, match="Explicit confirmation"):
            dispatcher.dispatch_capability(operation, {}, context=None)

    assert "CLI 必须带 `--yes`" in document
    assert "MCP 必须传 `confirm=True`" in document
    assert "现场 Agent 还会做第二次校验" in document


def test_field_agent_replacement_has_a_separate_on_site_authorization_boundary():
    document = _read(REMOTE_DOC)
    section = _section(document, "## 停止与替换现场 Agent")
    compact = re.sub(r"\s+", "", section)
    assert "stop-agent--yes" in compact
    assert "当前协议没有远程自更新或文件替换operation" in compact
    assert "另行取得现场维护者授权" in compact
    assert "验证旧前台进程已退出" in compact
    assert "校验官方ZIP的来源与摘要" in compact
    assert "保留回滚包" in compact
    assert "不得把任意工程师上传reference当作Agent更新包自动激活" in compact


def test_remote_document_transport_policy_contains_no_real_identity_or_secret():
    text = _remote_added_content()
    violations = [
        line.strip()
        for line in text.splitlines()
        if FORBIDDEN_ENABLEMENT.search(line) and not NEGATIVE_MARKERS.search(line)
    ]
    assert violations == []

    endpoint_hosts = re.findall(r"\bwss?://([^/:\"'\s]+)", text)
    assert endpoint_hosts
    assert set(endpoint_hosts) <= {"127.0.0.1", "<VPN_OR_LAN_HOST>"}
    assert re.search(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b", text) is None
    assert re.search(
        r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
        text,
    ) is None
    assert re.search(r"\bCOM\d+\b", text, re.IGNORECASE) is None
    assert re.search(
        r"(?i)(?<![A-Za-z])(?:[A-Z]:[\\/]|/home/|/Users/|\\\\Users\\\\)",
        text,
    ) is None
    assert re.search(
        r"(?i)\b(?:probe|hardware|device)[_-]?(?:id|serial)\s*[:=]\s*"
        r"[A-Za-z0-9_-]{4,}",
        text,
    ) is None
    assert re.search(
        r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|"
        r"glpat-[A-Za-z0-9_-]{16,})\b",
        text,
    ) is None
    assert re.search(r"--token(?:\s+|=)\S+", text) is None
    assert re.search(r"\bwss?://[^/\s]+@", text) is None

    environment_assignments = re.findall(
        r"^\s*\$env:[A-Z0-9_]+\s*=\s*(.+)$",
        text,
        flags=re.MULTILINE,
    )
    assert environment_assignments
    assert all(value.startswith("Read-Host ") for value in environment_assignments)
