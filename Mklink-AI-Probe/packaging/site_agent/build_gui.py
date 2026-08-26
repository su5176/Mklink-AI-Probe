"""Build the v0.1.8 portable Site Agent GUI bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import zipfile
from pathlib import Path, PurePosixPath


BUNDLE_VERSION = "0.1.8"
CORE_VERSION = "0.1.8"
ROOT_NAME = "MKLink-Site-Agent-v0.1.8-windows-x86_64-portable"
ZIP_NAME = f"{ROOT_NAME}.zip"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_FILE_MODE = 0o100644


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pe_subsystem(path: Path) -> int:
    data = path.read_bytes()
    if data[:2] != b"MZ" or len(data) < 0x40:
        raise RuntimeError(f"not a PE executable: {path.name}")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise RuntimeError(f"invalid PE header: {path.name}")
    optional_offset = pe_offset + 24
    if len(data) < optional_offset + 70:
        raise RuntimeError(f"truncated PE optional header: {path.name}")
    return struct.unpack_from("<H", data, optional_offset + 68)[0]


def safe_core_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Path]]:
    prefix = "mklink-remote-agent/"
    members: list[tuple[zipfile.ZipInfo, Path]] = []
    seen: set[str] = set()
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    for info in archive.infolist():
        if info.is_dir():
            continue
        raw_name = info.filename
        if (
            "\\" in raw_name
            or raw_name.startswith(("/", "\\", "//", "\\\\"))
            or "\0" in raw_name
        ):
            raise RuntimeError(f"unsafe core ZIP member: {raw_name}")
        name = raw_name
        if not name.startswith(prefix):
            raise RuntimeError(f"unexpected core ZIP member: {name}")
        relative = PurePosixPath(name[len(prefix) :])
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
        ):
            raise RuntimeError(f"unsafe core ZIP member: {name}")
        for part in relative.parts:
            normalized = part.rstrip(" .")
            if normalized != part or normalized.split(".", 1)[0].upper() in reserved:
                raise RuntimeError(f"Windows-unsafe core ZIP member: {name}")
        collision_key = relative.as_posix().casefold()
        if collision_key in seen:
            raise RuntimeError(f"case-fold collision in core ZIP: {name}")
        seen.add(collision_key)
        members.append((info, Path(*relative.parts)))
    if not members:
        raise RuntimeError("core ZIP is empty")
    return sorted(members, key=lambda item: item[1].as_posix())


def write_text(path: Path, value: str) -> None:
    path.write_text(value.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def deterministic_zip(root: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        files = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in files:
            relative = (Path(root.name) / path.relative_to(root)).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = FIXED_FILE_MODE << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def build_portable(
    *,
    output: Path,
    core_zip: Path,
    core_manifest: Path,
    gui_exe: Path,
    source_root: Path,
) -> tuple[Path, Path]:
    output = output.expanduser().resolve()
    core_zip = core_zip.expanduser().resolve()
    core_manifest = core_manifest.expanduser().resolve()
    gui_exe = gui_exe.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    if BUNDLE_VERSION != CORE_VERSION or f"v{CORE_VERSION}" not in ROOT_NAME:
        raise RuntimeError("portable bundle and core versions must match")
    output.mkdir(parents=True, exist_ok=True)
    candidate = output / ZIP_NAME
    external_manifest = output / f"{ROOT_NAME}.manifest.json"
    if candidate.exists() or external_manifest.exists():
        raise RuntimeError("candidate already exists; use a new task-owned output directory")
    core_metadata = json.loads(core_manifest.read_text(encoding="utf-8"))
    if str(core_metadata.get("product", {}).get("version")) != CORE_VERSION:
        raise RuntimeError("core package version does not match the portable bundle")
    core_artifact = core_metadata.get("artifact")
    if not isinstance(core_artifact, dict) or core_artifact != {
        "name": core_zip.name,
        "sha256": sha256(core_zip),
        "size": core_zip.stat().st_size,
    }:
        raise RuntimeError("core ZIP does not match its package manifest")
    core_files = {
        record.get("path"): record
        for record in core_metadata.get("files", [])
        if isinstance(record, dict)
    }
    core_exe_record = core_files.get("mklink-remote-agent.exe")
    stcp_dll_record = core_files.get("mklink-stcp.dll")
    if not isinstance(core_exe_record, dict) or not isinstance(stcp_dll_record, dict):
        raise RuntimeError("core package manifest is missing required runtime files")
    if pe_subsystem(gui_exe) != 2:
        raise RuntimeError("portable GUI executable is not Windows GUI subsystem")

    stage = output / f".staging-{ROOT_NAME}"
    if stage.exists():
        raise RuntimeError("candidate staging directory already exists")
    bundle = stage / ROOT_NAME
    bin_root = bundle / "bin"
    data_root = bundle / "data"
    bin_root.mkdir(parents=True)
    data_root.mkdir()
    try:
        with zipfile.ZipFile(core_zip) as archive:
            for info, relative in safe_core_members(archive):
                destination = bin_root / relative
                if not destination.resolve().is_relative_to(bin_root.resolve()):
                    raise RuntimeError(f"core ZIP member escapes destination: {info.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))
        core_exe = bin_root / "mklink-remote-agent.exe"
        if not core_exe.is_file() or sha256(core_exe) != core_exe_record.get("sha256"):
            raise RuntimeError(
                f"embedded v{CORE_VERSION} core executable hash mismatch"
            )
        if pe_subsystem(core_exe) != 3:
            raise RuntimeError("embedded core executable is not Windows console subsystem")
        stcp_dll = bin_root / "mklink-stcp.dll"
        if not stcp_dll.is_file() or sha256(stcp_dll) != stcp_dll_record.get("sha256"):
            raise RuntimeError("embedded in-process STCP library hash mismatch")

        shutil.copy2(gui_exe, bundle / "MKLink-Site-Agent.exe")
        shutil.copy2(source_root / "portable.mode", bundle / "portable.mode")
        shutil.copy2(source_root / "THIRD-PARTY-NOTICES.txt", bundle / "THIRD-PARTY-NOTICES.txt")
        write_text(
            data_root / "README.txt",
            "运行配置、DPAPI CurrentUser 凭据密文、状态和日志保存在此目录。\n",
        )
        write_text(
            bundle / "README.txt",
            "双击 MKLink-Site-Agent.exe。可选择 LAN/VPN 直连或 LAN STCP；"
            "STCP 客户端在进程内运行，不需要 frpc.exe。关闭窗口会隐藏到托盘，"
            "托盘“退出”才会停止现场代理。\n",
        )

        component_files = {
            "gui_exe": {
                "name": "MKLink-Site-Agent.exe",
                "version": BUNDLE_VERSION,
                "sha256": sha256(bundle / "MKLink-Site-Agent.exe"),
                "size": (bundle / "MKLink-Site-Agent.exe").stat().st_size,
                "pe_subsystem": "windows-gui",
            },
            "core_exe": {
                "name": "bin/mklink-remote-agent.exe",
                "version": CORE_VERSION,
                "sha256": sha256(core_exe),
                "size": core_exe.stat().st_size,
                "pe_subsystem": "windows-console-hidden-by-gui",
                "source_zip": core_zip.name,
                "source_zip_sha256": str(core_artifact["sha256"]),
            },
            "stcp_dll": {
                "name": "bin/mklink-stcp.dll",
                "frp_version": "0.69.1",
                "sha256": sha256(stcp_dll),
                "size": stcp_dll.stat().st_size,
                "frpc_executable": False,
            },
        }
        manifest = {
            "schema": "mklink.site-agent.release-manifest.v2-draft",
            "bundle": {
                "name": "MKLink Site Agent",
                "version": BUNDLE_VERSION,
                "mode": "portable",
                "target": "windows-x86_64",
            },
            "components": component_files,
            "protocol": {
                "version": "1.0",
                "transport": "direct-websocket-or-in-process-lan-stcp",
            },
            "lifecycle": {
                "owner": "GUI/tray process",
                "close_window": "hide-to-tray",
                "explicit_exit": "stop-owned-core-and-exit",
                "windows_service": False,
                "admin_required": False,
            },
            "secrets": {
                "persistence": "DPAPI CurrentUser ciphertext",
                "plaintext_to_typescript": False,
                "child_transport": [
                    "inherited MKLINK_REMOTE_TOKEN environment",
                    "inherited MKLINK_STCP_AUTH_TOKEN environment",
                    "inherited MKLINK_STCP_SECRET environment",
                ],
                "distinct_credentials_required": True,
            },
            "network_policy": {
                "default_bind": "127.0.0.1",
                "allowed": [
                    "same LAN direct IP",
                    "managed VPN direct IP",
                    "LAN-local frps with in-process STCP provider",
                ],
                "prohibited": [
                    "frpc.exe or any renamed/extracted frpc executable",
                    "bundled or child-process frps",
                    "NAT traversal",
                    "public relay or public tunnel",
                ],
            },
            "signing": {"status": "unsigned", "authenticode": "not-requested"},
        }
        write_text(
            bundle / "manifest.json",
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )
        deterministic_zip(bundle, candidate)
        manifest["artifact"] = {
            "name": candidate.name,
            "sha256": sha256(candidate),
            "size": candidate.stat().st_size,
        }
        write_text(
            external_manifest,
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return candidate, external_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--core-zip", type=Path, required=True)
    parser.add_argument("--core-manifest", type=Path, required=True)
    parser.add_argument("--gui-exe", type=Path, required=True)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "site-agent-gui",
    )
    args = parser.parse_args()
    candidate, manifest = build_portable(
        output=args.output,
        core_zip=args.core_zip,
        core_manifest=args.core_manifest,
        gui_exe=args.gui_exe,
        source_root=args.source_root,
    )
    print(
        json.dumps(
            {
                "candidate": str(candidate),
                "candidate_sha256": sha256(candidate),
                "manifest": str(manifest),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
