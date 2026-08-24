"""Non-blocking public release checks shared by AI-facing runtimes."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Iterable, Mapping, Sequence


DEFAULT_MANIFEST_URLS = (
    "https://raw.githubusercontent.com/Aladdin-Wang/Mklink-AI-Probe/updates/latest.json",
    "https://gitee.com/Aladdin-Wang/Mklink-AI-Probe/raw/updates/latest.json",
)
USER_AGENT = "Mklink-AI-Probe-Runtime-Updater"
_CHECK_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def version_key(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", value.strip())
    if not match:
        raise ValueError(f"unsupported version: {value}")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    return major, minor, patch, 0 if match.group(4) else 1


def current_version() -> str:
    try:
        return version("mklink")
    except PackageNotFoundError:
        root = Path(__file__).resolve().parent.parent
        try:
            import tomllib

            with (root / "pyproject.toml").open("rb") as stream:
                return str(tomllib.load(stream)["project"]["version"])
        except (ImportError, KeyError, OSError, TypeError, ValueError):
            return "unknown"


def default_cache_file() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "mklink-ai-probe" / "skill-update-check.json"


def _request_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_manifest(
    urls: Iterable[str], *, timeout: float = 3.0,
) -> tuple[dict[str, object], str]:
    errors = []
    for url in urls:
        try:
            payload = json.loads(_request_bytes(url, timeout).decode("utf-8"))
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
        manifest_url = str(cached["manifest_url"])
        if time.time() - checked_at > max_age_hours * 3600 or not isinstance(manifest, dict):
            return None
        return manifest, manifest_url
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, manifest: Mapping[str, object], manifest_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "checked_at_epoch": time.time(),
        "checked_at": utc_now(),
        "manifest_url": manifest_url,
        "manifest": manifest,
    }, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _result(
    manifest: Mapping[str, object], manifest_url: str, *, cached: bool,
) -> dict[str, object]:
    local = current_version()
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
        "install_requires_user_approval": True,
    }


def check_for_update(
    *,
    urls: Sequence[str] = DEFAULT_MANIFEST_URLS,
    cache_file: Path | None = None,
    max_age_hours: float = 24,
    force: bool = False,
    timeout: float = 3.0,
) -> dict[str, object]:
    """Check once per cache window and never make an AI health call fail."""
    cache = cache_file or default_cache_file()
    with _CHECK_LOCK:
        if not force:
            cached = _read_cache(cache, max_age_hours)
            if cached is not None:
                manifest, manifest_url = cached
                try:
                    return _result(manifest, manifest_url, cached=True)
                except ValueError:
                    pass
        try:
            manifest, manifest_url = fetch_manifest(urls, timeout=timeout)
            result = _result(manifest, manifest_url, cached=False)
            _write_cache(cache, manifest, manifest_url)
            return result
        except (RuntimeError, ValueError) as error:
            return {
                "status": "unavailable",
                "current_version": current_version(),
                "update_available": False,
                "error": str(error),
                "checked_at": utc_now(),
                "install_requires_user_approval": True,
            }
