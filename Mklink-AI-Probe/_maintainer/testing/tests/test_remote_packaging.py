"""Independent reproduction, dependency, and content audits for Site Agent."""

from __future__ import annotations

import hashlib
import json
import marshal
import re
import types
import zlib
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest

from remote_package_test_support import (
    BUILD_ENDPOINT_SENTINEL,
    BUILD_HARDWARE_SENTINEL,
    BUILD_SECRET_SENTINEL,
    PACKAGE_EVIDENCE_POINTER,
    PACKAGE_EVIDENCE_PRODUCER,
    PACKAGE_EVIDENCE_STATE,
    ROOT,
    get_clean_package,
    get_clean_wheel,
    sha256,
)

EXPECTED_WEBSOCKETS_REQUIREMENTS = {
    "websockets>=11.0",
    'websockets>=11.0; extra == "remote"',
    'websockets>=11.0; extra == "gui"',
}


@pytest.fixture(scope="session")
def clean_package(tmp_path_factory):
    return get_clean_package(tmp_path_factory)


@pytest.fixture(scope="session")
def clean_wheel(tmp_path_factory):
    return get_clean_wheel(tmp_path_factory)


def _path_variants(path: Path) -> set[bytes]:
    resolved = path.expanduser().resolve()
    native = str(resolved)
    forward = native.replace("\\", "/")
    values = {
        native,
        native.replace("\\", "\\\\"),
        forward,
        quote(forward, safe="/:"),
        quote(forward, safe=""),
        quote(native, safe=""),
    }
    if len(forward) >= 3 and forward[1:3] == ":/":
        for value in tuple(values):
            values.add(f"file:/{value}")
            values.add(f"file:///{value}")
    else:
        values.add(resolved.as_uri())
    result = set()
    for value in values:
        result.add(value.casefold().encode("utf-8"))
        result.add(value.casefold().encode("utf-16-le"))
    return result


def _sensitive_markers(evidence) -> set[bytes]:
    paths = {
        evidence["base"],
        evidence["source"],
        evidence["output"],
        evidence["output"] / ".site-agent-build",
        evidence["home"],
        evidence["source_product"],
        evidence["source_worktree"],
        Path.home(),
    }
    markers = set()
    for path in paths:
        markers.update(_path_variants(path))
    for value in (
        BUILD_SECRET_SENTINEL,
        BUILD_ENDPOINT_SENTINEL,
        BUILD_HARDWARE_SENTINEL,
    ):
        markers.add(value.casefold().encode("utf-8"))
        markers.add(value.casefold().encode("utf-16-le"))
    return markers


def _assert_content_clean(
    label: str,
    data: bytes,
    markers: set[bytes],
) -> None:
    lowered = data.lower()
    for marker in markers:
        assert marker not in lowered, f"sensitive marker in {label}: {marker!r}"
    assert b"-----begin private key-----" not in lowered
    assert b"-----begin openssh private key-----" not in lowered


def _code_payload(code: types.CodeType) -> bytes:
    values = []

    def visit(item: types.CodeType):
        values.extend((item.co_filename, item.co_name, item.co_qualname))
        for constant in item.co_consts:
            if isinstance(constant, types.CodeType):
                visit(constant)
            elif isinstance(constant, str):
                values.append(constant)
            elif isinstance(constant, bytes):
                values.append(constant.decode("latin1", errors="ignore"))

    visit(code)
    return "\n".join(values).encode("utf-8", errors="surrogatepass")


def _try_marshaled(data: bytes, *, pyc: bool = False):
    for offset in ((16, 12, 8) if pyc else (0,)):
        try:
            value = marshal.loads(data[offset:])
        except (EOFError, TypeError, ValueError):
            continue
        if isinstance(value, types.CodeType):
            return value
    return None


def _recursive_archive_entries(executable: Path):
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(executable))
    entries = []
    for name in sorted(reader.toc):
        try:
            data = reader.extract(name)
        except Exception:
            data = None
        try:
            embedded = reader.open_embedded_archive(name)
        except Exception:
            embedded = None
        entries.append(
            (
                name,
                data if embedded is None and isinstance(data, bytes) else None,
                None,
            )
        )
        if embedded is not None:
            for child_name in sorted(embedded.toc):
                child_data = embedded.extract(child_name)
                label = f"{name}::{child_name}"
                if isinstance(child_data, types.CodeType):
                    entries.append((label, None, child_data))
                else:
                    entries.append(
                        (
                            label,
                            child_data if isinstance(child_data, bytes) else None,
                            None,
                        )
                    )
        if isinstance(data, bytes) and name.casefold().endswith(".zip"):
            try:
                with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
                    for info in sorted(
                        archive.infolist(),
                        key=lambda item: item.filename,
                    ):
                        if info.is_dir():
                            continue
                        child_data = archive.read(info)
                        label = f"{name}::{info.filename}"
                        code = (
                            _try_marshaled(child_data, pyc=True)
                            if info.filename.casefold().endswith(".pyc")
                            else None
                        )
                        entries.append((label, None if code else child_data, code))
            except zipfile.BadZipFile:
                pass
    return entries


def test_final_package_evidence_is_explicitly_deferred_to_n5():
    provenance = json.loads(
        (
            ROOT
            / "packaging"
            / "site_agent"
            / "build-provenance.json"
        ).read_text("utf-8")
    )
    evidence = provenance["package_evidence"]

    assert evidence == {
        "canonical_pointer": PACKAGE_EVIDENCE_POINTER,
        "producer": PACKAGE_EVIDENCE_PRODUCER,
        "state": PACKAGE_EVIDENCE_STATE,
    }
    assert "sha256" not in evidence
    assert "size" not in evidence
    project_version = re.search(
        r'(?m)^version\s*=\s*"(?P<value>[^"\r\n]+)"\s*$',
        (ROOT / "pyproject.toml").read_text("utf-8"),
    )
    assert project_version is not None
    expected_version = project_version.group("value")
    assert provenance["product_version"] == expected_version
    gui_builder = (
        ROOT / "packaging" / "site_agent" / "build_gui.py"
    ).read_text("utf-8")
    assert f'BUNDLE_VERSION = "{expected_version}"' in gui_builder
    assert f'CORE_VERSION = "{expected_version}"' in gui_builder


def test_clean_environment_emits_a_self_consistent_artifact(clean_package):
    artifact = clean_package["artifact"]
    manifest = clean_package["manifest"]

    assert artifact.parent == clean_package["output"]
    assert ROOT not in artifact.parents
    assert re.fullmatch(r"[0-9a-f]{64}", sha256(artifact))
    assert artifact.stat().st_size > 0
    assert manifest.is_file()
    assert not (clean_package["output"] / ".site-agent-build").exists()
    assert clean_package["build_log"].stat().st_size > 0


def test_package_audit_covers_zip_manifest_and_recursive_archives(
    clean_package, tmp_path
):
    artifact = clean_package["artifact"]
    manifest_path = clean_package["manifest"]
    manifest = json.loads(manifest_path.read_text("utf-8"))
    markers = _sensitive_markers(clean_package)

    assert manifest["schema"] == "mklink.site-agent.package-manifest.v1"
    assert manifest["artifact"] == {
        "name": artifact.name,
        "sha256": sha256(artifact),
        "size": artifact.stat().st_size,
    }
    surfaces = manifest["audit"]["surfaces"]
    assert surfaces["manifest"] == 1
    assert surfaces["bundle_files"] == len(manifest["files"])
    assert surfaces["bundle_files"] >= 90
    assert manifest["audit"]["removed_local_origin_metadata"] == [
        "_internal/mklink-0.1.8.dist-info/direct_url.json"
    ]
    assert manifest["dependencies"]["in_process_stcp"] == {
        "frp_version": "0.69.1",
        "frpc_executable": False,
        "library": "mklink-stcp.dll",
        "sha256": sha256(
            ROOT / "native" / "stcp_bridge" / "build" / "mklink-stcp.dll"
        ),
        "source": "official github.com/fatedier/frp client packages",
    }
    _assert_content_clean("manifest", manifest_path.read_bytes(), markers)

    expected = {record["path"]: record for record in manifest["files"]}
    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        infos = [info for info in archive.infolist() if not info.is_dir()]
        assert len(infos) == surfaces["zip_members"]
        assert len(infos) == surfaces["bundle_files"]
        assert len({info.filename for info in infos}) == len(infos)
        assert not any(
            Path(info.filename).name.casefold() in {"frpc", "frpc.exe"}
            for info in infos
        )
        for info in infos:
            assert info.filename.startswith("mklink-remote-agent/")
            relative = info.filename.split("/", 1)[1]
            lowered_parts = {part.casefold() for part in Path(relative).parts}
            assert not {
                ".site-agent-build",
                "__pycache__",
                "source",
                "stage",
            }.intersection(lowered_parts)
            assert not any(part.endswith((".egg-info", ".pyc")) for part in lowered_parts)
            assert Path(relative).suffix.casefold() not in {
                ".axf",
                ".bin",
                ".elf",
                ".flm",
                ".hex",
                ".jpg",
                ".jpeg",
                ".log",
                ".pack",
                ".png",
                ".screenshot",
            }
            data = archive.read(info)
            assert info.CRC == (zlib.crc32(data) & 0xFFFFFFFF)
            record = expected[relative]
            assert len(data) == record["size"]
            assert hashlib.sha256(data).hexdigest() == record["sha256"]
            _assert_content_clean(f"ZIP member {relative}", data, markers)
        archive.extractall(tmp_path / "extract")

    executable = (
        tmp_path
        / "extract"
        / "mklink-remote-agent"
        / "mklink-remote-agent.exe"
    )
    entries = _recursive_archive_entries(executable)
    assert len(entries) == surfaces["archive_entries"]
    assert len(entries) > surfaces["bundle_files"]
    names = [name for name, _data, _code in entries]
    assert hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest() == (
        manifest["audit"]["archive_names_sha256"]
    )
    prohibited = re.compile(
        r"(?i)(?:^|[.:/\\])(?:fastmcp|pyinstaller|fastapi|starlette|uvicorn)"
        r"(?:$|[.:/\\])"
        r"|mklink\.(?:mcp_server|remote\.(?:api|mcp|stream_api))"
        r"|mklink\.cmsis_dap\.builtin_(?:flm|pack)_bundle"
    )
    for name, data, code in entries:
        assert not prohibited.search(name), name
        assert "direct_url.json" not in name.casefold(), name
        _assert_content_clean(f"archive name {name}", name.encode(), markers)
        if code is not None:
            _assert_content_clean(f"archive code {name}", _code_payload(code), markers)
        elif data is not None:
            _assert_content_clean(f"archive data {name}", data, markers)


def test_dependency_isolation_fresh_wheel_and_installed_metadata(
    clean_wheel
):
    wheel = clean_wheel["wheel"]
    report = clean_wheel["report"]
    markers = set()
    for path in (
        clean_wheel["base"],
        clean_wheel["source"],
        clean_wheel["home"],
        ROOT,
        ROOT.parent,
        Path.home(),
    ):
        markers.update(_path_variants(path))

    assert report["probe_connected"] is False
    assert report["fastmcp_loaded"] is False
    report_websockets = [
        " ".join(item.split())
        for item in report["websockets_requirements"]
    ]
    assert len(report_websockets) == len(EXPECTED_WEBSOCKETS_REQUIREMENTS)
    assert set(report_websockets) == EXPECTED_WEBSOCKETS_REQUIREMENTS
    installed = set(report["installed"])
    assert installed.isdisjoint(
        {
            "fastapi",
            "fastmcp",
            "pyinstaller",
            "pyside6",
            "pyqt6",
            "sitetunnel",
            "frp",
            "frpc",
            "frps",
            "stcp",
        }
    )

    with zipfile.ZipFile(wheel) as archive:
        assert archive.testzip() is None
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        assert not any("direct_url.json" in name.casefold() for name in names)
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        requirements = [
            line.split(":", 1)[1].strip()
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist:")
        ]
        websockets = [
            " ".join(item.split())
            for item in requirements
            if item.casefold().startswith("websockets")
        ]
        assert len(websockets) == len(EXPECTED_WEBSOCKETS_REQUIREMENTS)
        assert set(websockets) == EXPECTED_WEBSOCKETS_REQUIREMENTS
        optional_forbidden = [
            item
            for item in requirements
            if item.casefold().startswith(
                ("fastmcp", "fastapi", "pyinstaller", "pyqt", "pyside")
            )
        ]
        assert optional_forbidden
        assert all("extra ==" in item for item in optional_forbidden)
        assert all('extra == "remote"' not in item for item in optional_forbidden)
        assert (
            'pyinstaller==6.18.0; extra == "site-agent-build"'
            in optional_forbidden
        )
        for name in names:
            _assert_content_clean(
                f"wheel member {name}",
                archive.read(name),
                markers,
            )
