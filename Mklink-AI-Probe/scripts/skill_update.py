"""Check and install Mklink AI Probe desktop and Skill updates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib


DEFAULT_MANIFEST_URLS = (
    "https://raw.githubusercontent.com/Aladdin-Wang/Mklink-AI-Probe/updates/latest.json",
    "https://gitee.com/Aladdin-Wang/Mklink-AI-Probe/raw/updates/latest.json",
)
USER_AGENT = "Mklink-AI-Probe-Skill-Updater"
MANAGED_CONTENT_ROOTS = (PurePosixPath("gui/dist"),)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def version_key(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", value.strip())
    if not match:
        raise ValueError(f"unsupported version: {value}")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    return major, minor, patch, 0 if match.group(4) else 1


def current_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def default_cache_file() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "mklink-ai-probe" / "skill-update-check.json"


def _request_bytes(url: str, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_manifest(urls: Iterable[str]) -> tuple[dict[str, object], str]:
    errors = []
    for url in urls:
        try:
            payload = json.loads(_request_bytes(url).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("manifest root is not an object")
            return payload, url
        except (OSError, UnicodeError, ValueError, urllib.error.URLError) as error:
            errors.append(f"{url}: {error}")
    raise RuntimeError("; ".join(errors) or "no update manifest URL configured")


def _read_cache(path: Path, max_age_hours: float) -> tuple[dict[str, object], str] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        checked_at = float(cached["checked_at_epoch"])
        manifest = cached["manifest"]
        url = str(cached["manifest_url"])
        if time.time() - checked_at > max_age_hours * 3600:
            return None
        if not isinstance(manifest, dict):
            return None
        return manifest, url
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, manifest: Mapping[str, object], url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "checked_at_epoch": time.time(),
        "checked_at": utc_now(),
        "manifest_url": url,
        "manifest": manifest,
    }, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def update_result(
    *, root: Path, manifest: Mapping[str, object], manifest_url: str, cached: bool,
) -> dict[str, object]:
    local = current_version(root)
    latest = str(manifest.get("version", ""))
    available = version_key(latest) > version_key(local)
    return {
        "status": "ok",
        "current_version": local,
        "latest_version": latest,
        "update_available": available,
        "notes": str(manifest.get("notes", "")),
        "manifest_url": manifest_url,
        "cached": cached,
        "checked_at": utc_now(),
    }


def check_for_update(
    *, root: Path, urls: Sequence[str] = DEFAULT_MANIFEST_URLS,
    cache_file: Path | None = None, max_age_hours: float = 24, force: bool = False,
) -> dict[str, object]:
    cache = cache_file or default_cache_file()
    if not force:
        cached = _read_cache(cache, max_age_hours)
        if cached is not None:
            manifest, url = cached
            return update_result(root=root, manifest=manifest, manifest_url=url, cached=True)
    try:
        manifest, url = fetch_manifest(urls)
        _write_cache(cache, manifest, url)
        return update_result(root=root, manifest=manifest, manifest_url=url, cached=False)
    except (RuntimeError, ValueError) as error:
        return {
            "status": "unavailable",
            "current_version": current_version(root),
            "update_available": False,
            "error": str(error),
            "checked_at": utc_now(),
        }


def download_verified(url: str, destination: Path, sha256: str, size: int) -> None:
    digest = hashlib.sha256()
    written = 0
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as stream:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            digest.update(chunk)
            stream.write(chunk)
    if written != size or digest.hexdigest().casefold() != sha256.casefold():
        destination.unlink(missing_ok=True)
        raise RuntimeError("downloaded update does not match its published size and SHA-256")


def _safe_archive_members(archive: zipfile.ZipFile, destination: Path) -> list[Path]:
    destination_resolved = destination.resolve()
    files = []
    for info in archive.infolist():
        relative = PurePosixPath(info.filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("skill archive contains an unsafe path")
        target = destination.joinpath(*relative.parts).resolve()
        if destination_resolved != target and destination_resolved not in target.parents:
            raise RuntimeError("skill archive escapes the staging directory")
        if not info.is_dir():
            files.append(target)
    return files


def extract_skill_archive(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        _safe_archive_members(archive, destination)
        archive.extractall(destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "pyproject.toml").is_file():
        raise RuntimeError("skill archive has an unexpected root layout")
    return roots[0]


def _backup_root(root: Path, version: str) -> Path:
    backup_dir = default_cache_file().parent / "skill-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"mklink-ai-probe-{version}-{int(time.time())}.zip"
    with zipfile.ZipFile(backup, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if not path.is_file() or any(part in {"__pycache__", ".git", "target"} for part in path.parts):
                continue
            archive.write(path, path.relative_to(root).as_posix())
    return backup


def _installed_manifest_files(root: Path) -> set[PurePosixPath]:
    marker = root / ".mklink-skill-install.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        values = payload.get("files", [])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(values, list):
        return set()
    files = set()
    for value in values:
        relative = PurePosixPath(str(value))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        files.add(relative)
    return files


def _remove_obsolete_installed_files(
    root: Path, previous_files: set[PurePosixPath], installed_files: set[PurePosixPath],
) -> None:
    for relative in sorted(previous_files - installed_files, key=lambda path: len(path.parts), reverse=True):
        target = root.joinpath(*relative.parts)
        if target.is_file() or target.is_symlink():
            target.unlink()
        parent = target.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _remove_unlisted_managed_files(
    root: Path, installed_files: set[PurePosixPath],
) -> None:
    for managed_root in MANAGED_CONTENT_ROOTS:
        directory = root.joinpath(*managed_root.parts)
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if (path.is_file() or path.is_symlink()) and relative not in installed_files:
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass


def install_skill_archive(
    *, root: Path, archive_path: Path, expected_version: str, source_commit: str,
) -> dict[str, object]:
    if (root / ".git").exists():
        raise RuntimeError("refusing to overwrite a Git checkout; update it through Git")
    with tempfile.TemporaryDirectory(prefix="mklink-skill-stage-") as directory:
        source = extract_skill_archive(archive_path, Path(directory))
        if current_version(source) != expected_version:
            raise RuntimeError("skill archive version does not match the update manifest")
        plugin = json.loads((source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        if str(plugin.get("version")) != expected_version:
            raise RuntimeError("skill plugin version does not match the update manifest")
        previous_version = current_version(root)
        backup = _backup_root(root, previous_version)
        previous_files = _installed_manifest_files(root)
        installed_files: set[PurePosixPath] = set()
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            installed_files.add(PurePosixPath(relative.as_posix()))
        _remove_obsolete_installed_files(root, previous_files, installed_files)
        _remove_unlisted_managed_files(root, installed_files)
        marker = root / ".mklink-skill-install.json"
        marker.write_text(json.dumps({
            "version": expected_version,
            "source_commit": source_commit,
            "updated_at": utc_now(),
            "files": sorted(path.as_posix() for path in installed_files),
        }, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return {
        "previous_version": previous_version,
        "installed_version": expected_version,
        "backup": str(backup),
        "restart_ai_required": True,
    }


def _installed_app() -> tuple[Path | None, str | None]:
    if os.name != "nt":
        return None, None
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError:
        return None, None
    with root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            try:
                child = winreg.OpenKey(root, winreg.EnumKey(root, index))
                with child:
                    name = str(winreg.QueryValueEx(child, "DisplayName")[0])
                    if name != "Mklink AI Probe":
                        continue
                    value = str(winreg.QueryValueEx(child, "InstallLocation")[0]).strip('"')
                    try:
                        version = str(winreg.QueryValueEx(child, "DisplayVersion")[0])
                    except OSError:
                        version = None
                    return (Path(value) if value else None), version
            except OSError:
                continue
    return None, None


def _port_in_use(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.3)
        return client.connect_ex(("127.0.0.1", port)) == 0


def install_desktop(installer: Path) -> dict[str, object]:
    if os.name != "nt":
        raise RuntimeError("automatic desktop installation is currently supported on Windows only")
    if _port_in_use(8765):
        raise RuntimeError("close Mklink AI Probe and its local service before installing the update")
    arguments = ["/S"]
    location, _version = _installed_app()
    if location is not None:
        arguments.append(f"/D={location}")
    completed = subprocess.run([str(installer), *arguments], check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"desktop installer exited with code {completed.returncode}")
    return {"installed": True, "install_location": str(location) if location else None}


def _update_metadata(manifest: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = manifest.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"update manifest does not include {key} metadata")
    for field in ("url", "sha256", "size"):
        if field not in value:
            raise RuntimeError(f"update manifest {key} metadata is missing {field}")
    return value


def install_update(
    *, root: Path, manifest: Mapping[str, object], install_skill: bool,
    install_app: bool,
) -> dict[str, object]:
    latest = str(manifest.get("version", ""))
    skill_needed = install_skill and version_key(latest) > version_key(current_version(root))
    _app_location, app_version = _installed_app() if install_app else (None, None)
    app_needed = install_app and (
        app_version is None or version_key(latest) > version_key(app_version)
    )
    if not skill_needed and not app_needed:
        return {
            "status": "current",
            "version": latest,
            "skill_version": current_version(root),
            "desktop_version": app_version,
        }
    result: dict[str, object] = {"status": "updated", "version": latest}
    with tempfile.TemporaryDirectory(prefix="mklink-update-") as directory:
        temporary = Path(directory)
        if app_needed:
            platform = manifest.get("platforms", {})
            if not isinstance(platform, dict):
                raise RuntimeError("update manifest platforms metadata is invalid")
            app = _update_metadata(platform, "windows-x86_64")
            installer = temporary / "Mklink-AI-Probe-Setup.exe"
            download_verified(str(app["url"]), installer, str(app["sha256"]), int(app["size"]))
            result["desktop"] = install_desktop(installer)
        if skill_needed:
            skill = _update_metadata(manifest, "skill")
            archive = temporary / "Mklink-AI-Probe-Skill.zip"
            download_verified(str(skill["url"]), archive, str(skill["sha256"]), int(skill["size"]))
            result["skill"] = install_skill_archive(
                root=root,
                archive_path=archive,
                expected_version=str(skill.get("version", latest)),
                source_commit=str(skill.get("source_commit", "")),
            )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--force", action="store_true")
    check.add_argument("--cache-hours", type=float, default=24)
    check.add_argument("--manifest-url", action="append", dest="manifest_urls")
    check.add_argument("--json", action="store_true")
    install = subparsers.add_parser("install")
    install.add_argument("--yes", action="store_true")
    install.add_argument("--skill-only", action="store_true")
    install.add_argument("--app-only", action="store_true")
    install.add_argument("--manifest-url", action="append", dest="manifest_urls")
    install.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    urls = tuple(args.manifest_urls or DEFAULT_MANIFEST_URLS)
    if args.command == "check":
        result = check_for_update(
            root=root, urls=urls, max_age_hours=args.cache_hours, force=args.force,
        )
    else:
        if not args.yes:
            raise SystemExit("install requires --yes after explicit user approval")
        if args.skill_only and args.app_only:
            raise SystemExit("--skill-only and --app-only are mutually exclusive")
        manifest, _url = fetch_manifest(urls)
        result = install_update(
            root=root,
            manifest=manifest,
            install_skill=not args.app_only,
            install_app=not args.skill_only,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
