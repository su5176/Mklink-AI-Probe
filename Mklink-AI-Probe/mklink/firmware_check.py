"""Probe firmware version check for MicroLink and HPMLink burners.

纯函数 + dataclass 设计；CLI / FastAPI / GUI 共用。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Literal
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

# 固件文件名格式：MicroLink_V3.3.1.uf2 / HPMLink_V4.3.7.uf2
_FIRMWARE_FILE_RE = re.compile(
    r"^(?P<prefix>MicroLink|HPMLink)_"
    r"(?P<version>V(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+))\.uf2$"
)
FirmwareFamily = Literal["microlink", "hpmlink"]
HPM_FIRMWARE_MARKER = re.compile(
    r"(?im)^\s*HPM\s+Firmware\s+Build\s+Date\s*:"
)

# Firmware directory name (relative to repo/package root)
FIRMWARE_DIR_NAME = "MK-Firmware"

# Default environment variable for overriding firmware dir
FIRMWARE_DIR_ENV = "MKLINK_FIRMWARE_DIR"

# Serial command timeout (seconds) for cmd.get_version()
DEFAULT_VERSION_TIMEOUT = 5.0

# Status of CheckResult
CheckStatus = Literal[
    "ok", "upgrade_required", "no_firmware", "manifest_unavailable", "skipped"
]
UpgradeStatus = Literal[
    "up_to_date",
    "updated",
    "copied_unverified",
    "manual_required",
    "no_probe_disk",
    "no_firmware",
]


@dataclass(frozen=True)
class Version:
    """SemVer-style version with V<major>.<minor>.<patch> string format."""
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"V{self.major}.{self.minor}.{self.patch}"

    def __lt__(self, other: "Version") -> bool:
        return (self.major, self.minor, self.patch) < (
            other.major, other.minor, other.patch
        )

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (self.major, self.minor, self.patch) == (
            other.major, other.minor, other.patch
        )

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch))


@dataclass
class FirmwareInfo:
    """A supported local UF2 or an entry from the online firmware channel."""
    name: str
    version: Version
    model: str  # "V3" | "V4"
    path: Path
    family: FirmwareFamily = "microlink"
    download_url: str | None = None
    download_source: Literal["github", "gitee"] | None = None
    download_urls: dict[str, str] | None = None
    size: int | None = None
    sha256: str | None = None

    @property
    def version_str(self) -> str:
        return str(self.version)


def parse_firmware_filename(name) -> FirmwareInfo | None:
    """Parse a supported MicroLink/HPMLink UF2 filename.

    Accepts str or Path; returns None for non-matching names.
    """
    raw = Path(name).name  # strip parent directories
    m = _FIRMWARE_FILE_RE.match(raw)
    if not m:
        return None
    major = int(m.group("major"))
    minor = int(m.group("minor"))
    patch = int(m.group("patch"))
    family: FirmwareFamily = (
        "hpmlink" if m.group("prefix") == "HPMLink" else "microlink"
    )
    if family == "hpmlink" and major != 4:
        return None
    return FirmwareInfo(
        name=raw,
        version=Version(major, minor, patch),
        model=f"V{major}",
        path=Path(name),
        family=family,
    )


def list_firmwares(root: Path) -> list[FirmwareInfo]:
    """List supported UF2 files in `root`, sorted by version ascending.

    Raises FileNotFoundError if `root` does not exist.
    Files that don't match the pattern are silently skipped.
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"Firmware directory not found: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"Not a directory: {root}")

    result: list[FirmwareInfo] = []
    for entry in root.iterdir():
        info = parse_firmware_filename(entry)
        if info is not None:
            result.append(info)
    result.sort(key=lambda f: f.version)
    return result


def _read_microkeen_readme(root: str | Path) -> str | None:
    path = Path(root) / "readme.txt"
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, UnicodeError):
        return None


def read_microkeen_version(root: str | Path) -> Version | None:
    """Read the active probe version from a MICROKEEN ``readme.txt``.

    The probe keeps a descending changelog in this file; the first semantic
    version is the build currently installed on the drive.
    """
    text = _read_microkeen_readme(root)
    if text is None:
        return None
    match = re.search(r"(?<![A-Za-z0-9])V(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return Version(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def probe_firmware_family(
    root: str | Path, current: Version | None = None,
) -> FirmwareFamily:
    """Identify HPMLink from the firmware identity line in ``readme.txt``."""
    if current is not None and current.major != 4:
        return "microlink"
    text = _read_microkeen_readme(root)
    return "hpmlink" if text is not None and HPM_FIRMWARE_MARKER.search(text) else "microlink"


def _probe_disk():
    from mklink.discovery import find_microkeen_disk

    return find_microkeen_disk()


def _find_bootloader_disk() -> str | None:
    """Find a UF2 bootloader volume, whose label is not MICROKEEN."""
    configured = os.environ.get("MKLINK_BOOTLOADER_DISK", "").strip()
    candidates = [configured] if configured else []
    if os.name == "nt":
        import string

        candidates.extend(f"{letter}:\\" for letter in string.ascii_uppercase)
    else:
        candidates.extend(("/media", "/run/media", "/Volumes"))
    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        root = str(raw).rstrip("\\/") + ("\\" if os.name == "nt" else "/")
        key = root.casefold()
        if key in seen:
            continue
        seen.add(key)
        path = Path(root)
        if path.is_dir() and _looks_like_bootloader(path):
            return root
    return None


FIRMWARE_MANIFEST_URLS = {
    "github": "https://raw.githubusercontent.com/Aladdin-Wang/Mklink-AI-Probe/firmware/latest.json",
    "gitee": "https://gitee.com/Aladdin-Wang/Mklink-AI-Probe/raw/firmware/latest.json",
}
FIRMWARE_MANIFEST_SCHEMA = "mklink-firmware-v1"


def _parse_manifest(payload: object) -> list[FirmwareInfo]:
    """Validate the public manifest and return its firmware entries."""
    if not isinstance(payload, dict) or payload.get("schema") != FIRMWARE_MANIFEST_SCHEMA:
        raise ValueError("unsupported firmware manifest schema")
    raw_entries = payload.get("firmwares")
    if not isinstance(raw_entries, list):
        raise ValueError("firmware manifest has no firmware list")
    entries: list[FirmwareInfo] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("invalid firmware manifest entry")
        info = parse_firmware_filename(str(raw.get("name") or ""))
        urls = raw.get("urls")
        digest = str(raw.get("sha256") or "").lower()
        size = raw.get("size")
        if (
            info is None
            or raw.get("family") != info.family
            or raw.get("model") != info.model
            or raw.get("version") != info.version_str
            or not isinstance(size, int)
            or size <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(urls, dict)
        ):
            raise ValueError("invalid firmware manifest entry")
        clean_urls = {
            source: str(urls[source])
            for source in ("github", "gitee")
            if isinstance(urls.get(source), str)
            and str(urls[source]).startswith("https://")
        }
        if not clean_urls:
            raise ValueError("firmware manifest entry has no HTTPS download URL")
        key = (info.family, info.model)
        if key in seen:
            raise ValueError("duplicate firmware family/model in manifest")
        seen.add(key)
        info.path = Path(info.name)
        info.size = size
        info.sha256 = digest
        info.download_urls = clean_urls
        preferred = "github" if "github" in clean_urls else "gitee"
        info.download_source = preferred
        info.download_url = clean_urls[preferred]
        entries.append(info)
    return entries


def _remote_firmwares_from_source(
    source: Literal["github", "gitee"], *, timeout: float = 8.0,
) -> list[FirmwareInfo] | None:
    """Fetch one provider's complete firmware manifest; fail softly."""
    try:
        request = Request(
            FIRMWARE_MANIFEST_URLS[source],
            headers={"Accept": "application/json", "User-Agent": "mklink-ai-probe"},
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _parse_manifest(payload)
    except (OSError, URLError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _remote_firmware_from_source(
    model: str,
    source: Literal["github", "gitee"],
    *,
    family: FirmwareFamily = "microlink",
    timeout: float = 8.0,
) -> FirmwareInfo | None:
    """Find an exact family/model entry from one manifest provider."""
    entries = _remote_firmwares_from_source(source, timeout=timeout)
    if entries is None:
        return None
    for info in entries:
        if info.model == model and info.family == family:
            if info.download_urls and source in info.download_urls:
                info.download_source = source
                info.download_url = info.download_urls[source]
            return info
    return None


def _remote_firmware(
    model: str,
    *,
    family: FirmwareFamily = "microlink",
    timeout: float = 8.0,
) -> FirmwareInfo | None:
    """Find the newest same-model UF2 from GitHub, then Gitee."""
    for source in ("github", "gitee"):
        candidate = _remote_firmware_from_source(
            model, source, family=family, timeout=timeout,
        )
        if candidate is not None:
            return candidate
    return None


def _remote_firmwares(*, timeout: float = 8.0) -> list[FirmwareInfo] | None:
    """Fetch the complete index with GitHub-to-Gitee provider fallback."""
    for source in ("github", "gitee"):
        entries = _remote_firmwares_from_source(source, timeout=timeout)
        if entries is not None:
            for info in entries:
                if info.download_urls and source in info.download_urls:
                    info.download_source = source
                    info.download_url = info.download_urls[source]
            return entries
    return None


def _materialize_firmware(info: FirmwareInfo) -> tuple[Path, bool, str]:
    """Return a local UF2 path, ownership flag, and actual download source."""
    if info.download_url:
        handle, raw_path = tempfile.mkstemp(prefix="mklink-firmware-", suffix=".uf2")
        os.close(handle)
        destination = Path(raw_path)
        preferred = info.download_source or "github"
        downloads: list[tuple[str, str]] = [(preferred, info.download_url)]
        for source in ("github", "gitee"):
            url = (info.download_urls or {}).get(source)
            if url and (source, url) not in downloads:
                downloads.append((source, url))
        attempted_sources: list[str] = []
        last_error: Exception | None = None
        index = 0
        while index < len(downloads):
            source, url = downloads[index]
            index += 1
            if source not in attempted_sources:
                attempted_sources.append(source)
            try:
                request = Request(url, headers={"User-Agent": "mklink-ai-probe"})
                with urlopen(request, timeout=30) as response, destination.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
                _validate_downloaded_firmware(destination, info)
                return destination, True, source
            except Exception as error:
                last_error = error
                destination.unlink(missing_ok=True)
                if source == "github" and not info.download_urls:
                    if "gitee" not in attempted_sources:
                        attempted_sources.append("gitee")
                    fallback = _remote_firmware_from_source(
                        info.model, "gitee", family=info.family,
                    )
                    if (
                        fallback is not None
                        and fallback.version == info.version
                        and fallback.download_url
                    ):
                        downloads.append(("gitee", fallback.download_url))
        destination.unlink(missing_ok=True)
        attempted = "、".join(attempted_sources)
        raise OSError(f"固件下载失败（已尝试 {attempted}）") from last_error
    return info.path, False, "local"


def validate_uf2(path: str | Path) -> int:
    """Validate UF2 framing and return its block count."""
    path = Path(path)
    size = path.stat().st_size
    if size == 0 or size % 512:
        raise ValueError("UF2 file size is not a non-zero multiple of 512 bytes")
    expected_blocks = size // 512
    with path.open("rb") as stream:
        for index in range(expected_blocks):
            block = stream.read(512)
            if (
                int.from_bytes(block[0:4], "little") != 0x0A324655
                or int.from_bytes(block[4:8], "little") != 0x9E5D5157
                or int.from_bytes(block[508:512], "little") != 0x0AB16F30
            ):
                raise ValueError(f"invalid UF2 magic in block {index}")
            payload_size = int.from_bytes(block[16:20], "little")
            block_number = int.from_bytes(block[20:24], "little")
            total_blocks = int.from_bytes(block[24:28], "little")
            if payload_size > 476 or block_number != index or total_blocks != expected_blocks:
                raise ValueError(f"invalid UF2 metadata in block {index}")
    return expected_blocks


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_downloaded_firmware(path: Path, info: FirmwareInfo) -> None:
    if info.size is not None and path.stat().st_size != info.size:
        raise ValueError("downloaded firmware size does not match manifest")
    if info.sha256 is not None and _file_sha256(path) != info.sha256:
        raise ValueError("downloaded firmware SHA-256 does not match manifest")
    if info.size is not None or info.sha256 is not None:
        validate_uf2(path)


def latest_firmware(
    model: str,
    firmware_root: Path,
    *,
    family: FirmwareFamily = "microlink",
) -> FirmwareInfo | None:
    """Resolve the latest distributable firmware for a validated probe model."""
    if family == "hpmlink" and model != "V4":
        return None
    remote = _remote_firmware(model, family=family)
    try:
        local = [
            item for item in list_firmwares(firmware_root)
            if item.model == model and item.family == family
        ]
    except (FileNotFoundError, NotADirectoryError, OSError):
        local = []
    candidates = ([remote] if remote is not None else []) + local
    return max(candidates, key=lambda item: item.version) if candidates else None


def _looks_like_bootloader(root: str | Path) -> bool:
    path = Path(root)
    return any((path / marker).is_file() for marker in ("INFO_UF2.TXT", "INDEX.HTM", "CURRENT.UF2"))


def _wait_for_bootloader_drive(previous: str | None, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    disappeared = False
    while time.monotonic() < deadline:
        current = _find_bootloader_disk()
        if current is None or (previous and current.rstrip("\\/").casefold() != previous.rstrip("\\/").casefold() and not Path(current).exists()):
            disappeared = True
        elif current and Path(current).is_dir() and (disappeared or _looks_like_bootloader(current)):
            if _looks_like_bootloader(current):
                return current
        time.sleep(0.15)
    return None


def upgrade_probe_firmware(
    device: object | None,
    firmware_root: Path,
    *,
    confirm: bool = False,
    bootloader_timeout: float = 20.0,
    verify_timeout: float = 20.0,
) -> dict:
    """Upgrade a connected probe through its UF2 bootloader drive."""
    if confirm is not True:
        raise ValueError("firmware upgrade requires confirm=True")
    disk = _probe_disk()
    if not disk:
        return {"status": "no_probe_disk", "message": "未找到 MICROKEEN U 盘"}
    current = read_microkeen_version(disk)
    if current is None:
        return {"status": "manual_required", "message": "无法从 U 盘 readme.txt 读取当前固件版本", "disk": disk}
    family = probe_firmware_family(disk, current)
    candidate = latest_firmware(
        f"V{current.major}", firmware_root, family=family,
    )
    if candidate is None:
        return {
            "status": "no_firmware",
            "message": (
                "在线固件通道暂不可用，或尚未发布 "
                f"{family} V{current.major} 固件"
            ),
            "current_version": str(current),
            "model": f"V{current.major}",
            "family": family,
        }
    if candidate.version <= current:
        return {"status": "up_to_date", "current_version": str(current), "latest_version": candidate.version_str, "firmware": candidate.name}
    manual_details = {
        "current_version": str(current),
        "latest_version": candidate.version_str,
        "firmware": candidate.name,
        "model": candidate.model,
        "family": candidate.family,
        "download_available": True,
    }
    enter = getattr(device, "enter_bootloader", None)
    if not callable(enter):
        return {
            "status": "manual_required",
            "message": "未连接调试会话，请按住升级键进入 Bootloader 后复制该 UF2 文件",
            **manual_details,
        }
    try:
        source, temporary, download_source = _materialize_firmware(candidate)
    except OSError as error:
        return {
            "status": "manual_required",
            "message": str(error),
            **manual_details,
        }
    try:
        enter()
        boot_disk = _wait_for_bootloader_drive(disk, bootloader_timeout)
        if not boot_disk:
            return {
                "status": "manual_required",
                "message": "未检测到 Bootloader U 盘，请按住升级键手动升级",
                **manual_details,
            }
        target = Path(boot_disk) / candidate.name
        shutil.copyfile(source, target)
        deadline = time.monotonic() + verify_timeout
        verified = None
        while time.monotonic() < deadline:
            active = _probe_disk()
            if active:
                version = read_microkeen_version(active)
                if version is not None:
                    verified = version
                    if version >= candidate.version:
                        return {
                            "status": "updated",
                            "current_version": str(current),
                            "latest_version": candidate.version_str,
                            "verified_version": str(version),
                            "firmware": candidate.name,
                            "family": candidate.family,
                            "source": download_source,
                        }
            time.sleep(0.25)
        return {
            "status": "copied_unverified",
            "current_version": str(current),
            "latest_version": candidate.version_str,
            "verified_version": str(verified) if verified else None,
            "firmware": candidate.name,
            "message": "固件已复制到 U 盘，但尚未读到新的 readme.txt 版本",
            "model": candidate.model,
            "family": candidate.family,
            "download_available": True,
            "source": download_source,
        }
    finally:
        if temporary:
            source.unlink(missing_ok=True)


def find_min_version(firmwares: list[FirmwareInfo]) -> Version | None:
    """Return the lowest version among firmwares, or None if empty."""
    if not firmwares:
        return None
    return min(f.version for f in firmwares)


def find_recommended_uf2(
    firmwares: list[FirmwareInfo], current: Version | None
) -> FirmwareInfo | None:
    """Recommend the highest-version firmware with the same major version as `current`.

    Returns None if current is None or no firmware shares current.major.
    """
    if current is None or not firmwares:
        return None
    same_major = [f for f in firmwares if f.version.major == current.major]
    if not same_major:
        return None
    return max(same_major, key=lambda f: f.version)


def _resolve_firmware_root() -> Path:
    """Resolve the MK-Firmware directory.

    Priority:
      1. MKLINK_FIRMWARE_DIR env var
      2. <cwd>/MK-Firmware
      3. <package_parent>/MK-Firmware  (i.e., one level above mklink/ in the installed package)

    Returns the first path that exists; if none exist, returns the env-var path
    (or cwd path) so the caller can decide how to handle the missing case.
    """
    # 1. env var
    env_path = os.environ.get(FIRMWARE_DIR_ENV)
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return candidate
        # env-set but missing → still return it; caller handles FileNotFoundError
        return candidate

    # 2. cwd
    cwd_candidate = Path.cwd() / FIRMWARE_DIR_NAME
    if cwd_candidate.exists():
        return cwd_candidate

    # 3. package parent (one level above the mklink/ package)
    pkg_parent = _PACKAGE_PARENT_OVERRIDE if _PACKAGE_PARENT_OVERRIDE is not None else Path(__file__).resolve().parent.parent
    pkg_candidate = pkg_parent / FIRMWARE_DIR_NAME
    return pkg_candidate  # may not exist; caller handles


# Test seam: lets tests inject a different package parent.
_PACKAGE_PARENT_OVERRIDE: Path | None = None


def read_bridge_version(bridge: object, *, timeout: float = DEFAULT_VERSION_TIMEOUT) -> Version | None:
    """Read and parse the probe version from an already connected bridge."""
    resp = bridge.send_command("cmd.get_version()", timeout=timeout)
    # Reuse the existing CLI parser (single source of truth for the format)
    from mklink.cli import _parse_version_response
    current_str, _ = _parse_version_response(resp)
    if not current_str:
        return None
    # Re-parse into Version using the firmware file regex (same shape)
    m = _FIRMWARE_FILE_RE.match(f"MicroLink_{current_str}.uf2")
    if not m:
        return None
    return Version(
        int(m.group("major")),
        int(m.group("minor")),
        int(m.group("patch")),
    )


def read_device_version(port: str, *, timeout: float = DEFAULT_VERSION_TIMEOUT) -> Version | None:
    """Read probe firmware version via cmd.get_version().

    Returns the parsed Version, or None if the response cannot be parsed.
    Raises (TimeoutError, ConnectionError, etc.) on serial errors — caller
    decides how to recover.
    """
    from mklink.bridge import MKLinkSerialBridge

    bridge = MKLinkSerialBridge(port)
    try:
        if not bridge.connect():
            raise ConnectionError("Unable to connect to MKLink CDC port")
        return read_bridge_version(bridge, timeout=timeout)
    finally:
        bridge.close()


# Late-bound import: bridge is only needed when read_device_version is called.
# This avoids forcing pyserial import on every module load.
def __getattr__(name: str):
    if name == "MKLinkSerialBridge":
        from mklink.bridge import MKLinkSerialBridge
        return MKLinkSerialBridge
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class CheckResult:
    """Outcome of probe firmware check. See CheckStatus for possible values."""
    status: CheckStatus
    current_version: Version | None
    min_required_version: Version | None
    recommended_uf2: FirmwareInfo | None
    all_uf2s: list[FirmwareInfo]
    firmware_dir: Path | None
    instructions: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "current_version": str(self.current_version) if self.current_version else None,
            "min_required_version": str(self.min_required_version) if self.min_required_version else None,
            "recommended_uf2": _firmware_to_dict(self.recommended_uf2) if self.recommended_uf2 else None,
            "all_uf2s": [_firmware_to_dict(f) for f in self.all_uf2s],
            "firmware_dir": str(self.firmware_dir) if self.firmware_dir else None,
            "instructions": self.instructions,
        }


def _firmware_to_dict(f: FirmwareInfo) -> dict:
    return {
        "name": f.name,
        "version": f.version_str,
        "model": f.model,
        "family": f.family,
        "path": str(f.path),
        "source": f.download_source or "local",
    }


# Button locations per probe model (V3: between two "eyes"; V4: side toggle)
_BUTTON_HINT = {
    "V3": "按住 V3 探针上**两个眼睛中间**的按钮",
    "V4": "按住 V4 探针**侧边拨轮**按钮",
}


def build_instructions(result: CheckResult) -> str:
    """Build the user-facing multi-line instructions text.

    Used by both CLI (printed as [WARN] block) and GUI (rendered in modal).
    """
    lines: list[str] = []
    lines.append("[WARN] 探针固件需要升级，请按以下步骤操作：")
    lines.append("")

    # 1. Determine button hint based on what we know about the probe model
    model: str | None = None
    if result.current_version is not None:
        model = f"V{result.current_version.major}"
    if model and model in _BUTTON_HINT:
        lines.append(f"  1. {_BUTTON_HINT[model]}，再插上 USB 上电")
    else:
        # unknown model — explain both
        lines.append("  1. 按住探针的升级按钮不放：")
        lines.append(f"     - V3 探针：{_BUTTON_HINT['V3']}")
        lines.append(f"     - V4 探针：{_BUTTON_HINT['V4']}")
        lines.append("     然后插上 USB 上电")
    lines.append("  2. 此时电脑会弹出一个 MICROKEEN U 盘")
    lines.append("  3. 将以下固件文件拷贝到该 U 盘根目录：")

    # 2. Tell the user which UF2 to use
    if result.recommended_uf2 is not None:
        lines.append(f"     - {result.recommended_uf2.name}")
    elif result.current_version is not None and not result.recommended_uf2:
        # current known but no same-major firmware
        lines.append(
            f"     - （无 V{result.current_version.major} 同型号固件，"
            "请检查在线固件通道或联系维护者）"
        )
    else:
        for f in result.all_uf2s:
            lines.append(f"     - {f.path}")
    lines.append("  4. 拷贝完成后拔下 USB，重新插上即可使用新固件")
    lines.append("")
    if result.current_version is not None and result.min_required_version is not None:
        lines.append(
            f"  [诊断] 当前 {result.current_version}，"
            f"最低要求 {result.min_required_version}"
        )
    return "\n".join(lines)


def check_probe_firmware(
    port: str | None, firmware_root: Path
) -> CheckResult:
    """Top-level check: list firmwares, read device version, decide status.

    Failure-soft: never raises. Any error → returns a CheckResult with
    a non-'ok' status. UI/CLI can inspect `status` and act accordingly.
    """
    # Step 1: resolve the independent online channel, with a local development
    # fallback. Installed builds intentionally do not carry MK-Firmware/.
    remote_available = True
    firmwares = _remote_firmwares()
    try:
        local_firmwares = list_firmwares(firmware_root)
    except (FileNotFoundError, NotADirectoryError, OSError):
        local_firmwares = []
    if firmwares is None:
        remote_available = False
        firmwares = local_firmwares
    else:
        remote_versions = {
            (item.family, item.model, item.version) for item in firmwares
        }
        firmwares.extend(
            item for item in local_firmwares
            if (item.family, item.model, item.version) not in remote_versions
        )
    if not firmwares:
        status: CheckStatus = (
            "no_firmware" if remote_available else "manifest_unavailable"
        )
        return CheckResult(
            status=status,
            current_version=None,
            min_required_version=None,
            recommended_uf2=None,
            all_uf2s=[],
            firmware_dir=None if remote_available else Path(firmware_root),
            instructions=(
                "在线固件索引中没有可用固件"
                if remote_available
                else "无法连接 GitHub/Gitee 固件通道，且没有本地备用固件"
            ),
        )

    # Step 2: prefer the version stamped in the mounted probe drive.  This is
    # available even when an older firmware cannot answer cmd.get_version().
    current: Version | None = None
    disk = _probe_disk()
    if disk:
        current = read_microkeen_version(disk)
    family = probe_firmware_family(disk, current) if disk else "microlink"
    all_firmwares = firmwares
    firmwares = [item for item in all_firmwares if item.family == family]
    if not firmwares:
        return CheckResult(
            status="no_firmware",
            current_version=current,
            min_required_version=None,
            recommended_uf2=None,
            all_uf2s=[],
            firmware_dir=None if remote_available else Path(firmware_root),
            instructions=f"在线固件通道尚未发布 {family} 固件",
        )

    min_version = find_min_version(firmwares)
    assert min_version is not None
    if current is None:
        if port is None:
            return CheckResult(
                status="skipped",
                current_version=None,
                min_required_version=min_version,
                recommended_uf2=None,
                all_uf2s=firmwares,
                firmware_dir=None if remote_available else Path(firmware_root),
                instructions="",
            )
        try:
            current = read_device_version(port)
            resolved_family = (
                probe_firmware_family(disk, current) if disk else "microlink"
            )
            if resolved_family != family:
                family = resolved_family
                firmwares = [
                    item for item in all_firmwares if item.family == family
                ]
                if not firmwares:
                    return CheckResult(
                        status="no_firmware",
                        current_version=current,
                        min_required_version=None,
                        recommended_uf2=None,
                        all_uf2s=[],
                        firmware_dir=None if remote_available else Path(firmware_root),
                        instructions=f"在线固件通道尚未发布 {family} 固件",
                    )
                min_version = find_min_version(firmwares)
                assert min_version is not None
        except TimeoutError:
            current = None
        except Exception:
            return CheckResult(
                status="skipped",
                current_version=None,
                min_required_version=min_version,
                recommended_uf2=None,
                all_uf2s=firmwares,
                firmware_dir=None if remote_available else Path(firmware_root),
                instructions="",
            )

    # Step 3: compare against the newest firmware for the exact hardware model.
    recommended = find_recommended_uf2(firmwares, current)
    required = recommended.version if recommended is not None else min_version
    requires_upgrade = (current is None) or (
        recommended is not None and current < recommended.version
    )
    status = "upgrade_required" if requires_upgrade else "ok"

    result = CheckResult(
        status=status,
        current_version=current,
        min_required_version=required,
        recommended_uf2=recommended,
        all_uf2s=firmwares,
        firmware_dir=None if remote_available else Path(firmware_root),
        instructions="",
    )
    if status == "upgrade_required":
        result.instructions = build_instructions(result)
    return result
