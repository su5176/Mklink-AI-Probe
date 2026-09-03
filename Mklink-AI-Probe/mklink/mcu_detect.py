"""MCU profile and FLM discovery from Keil/Arm device packs."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


_EXTERNAL_FLASH_MARKERS = (
    "QSPI",
    "OSPI",
    "FMC",
    "NOR",
    "NAND",
    "MMC",
    "MT25",
    "MX25",
    "S25",
    "W25",
    "HYPER",
    "EVAL",
    "DISCO",
    "DISCOVERY",
)


def default_profiles_path() -> Path:
    return Path(__file__).resolve().parent / "mcu_profiles.json"


def default_pack_roots() -> list[Path]:
    roots = [
        Path(r"C:\Keil_v5\ARM\PACK"),
        Path(r"D:\Keil_v5\ARM\PACK"),
    ]
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        roots.extend(
            [
                Path(userprofile) / "AppData" / "Local" / "Arm" / "Packs",
                Path(userprofile) / "AppData" / "Roaming" / "Arm" / "Packs",
            ]
        )
    return [p for p in roots if p.exists()]


def default_keil_flash_roots() -> list[Path]:
    roots = [
        Path(r"C:\Keil_v5\ARM\Flash"),
        Path(r"C:\Keil_v5\ARM\Pack\Flash"),
        Path(r"D:\Keil_v5\ARM\Flash"),
        Path(r"D:\Keil_v5\ARM\Pack\Flash"),
    ]
    return [p for p in roots if p.exists()]


def _load_profile_data(path: Path) -> dict:
    if not path.exists():
        return {"$schema": "mklink-mcu-profiles-v1", "mcus": {}}
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if "mcus" not in data or not isinstance(data["mcus"], dict):
        data["mcus"] = {}
    return data


def _save_profile_data(path: Path, data: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return backup


def _norm_device(value: str | None) -> str:
    return (value or "").strip().upper()


def _hex(value: str | None, default: str = "0x00000000") -> str:
    if not value:
        return default
    s = value.strip()
    if s.lower().startswith("0x"):
        return "0x" + s[2:].upper().zfill(8)
    try:
        return f"0x{int(s, 0):08X}"
    except ValueError:
        return default


def _size_label(value: str | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            n = int(value, 0)
        except ValueError:
            return value
    else:
        n = int(value)
    if n and n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)}MB"
    if n and n % 1024 == 0:
        return f"{n // 1024}KB"
    return str(n)


def _profile_key(prefix: str) -> str:
    return prefix.lower()


def _iter_pdsc_files(pack_roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in pack_roots:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".pdsc":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.pdsc"))
    return sorted(files)


def _is_internal_algorithm(attrs: dict[str, str]) -> bool:
    name = attrs.get("name", "")
    if not name.upper().endswith(".FLM"):
        return False
    try:
        start = int(attrs.get("start", ""), 0)
    except ValueError:
        return False
    if start != 0x08000000:
        return False
    upper = name.upper()
    return not any(marker in upper for marker in _EXTERNAL_FLASH_MARKERS)


def _algorithm_record(attrs: dict[str, str], pdsc: Path) -> dict:
    return {
        "name": attrs.get("name", "").replace("\\", "/"),
        "start": _hex(attrs.get("start"), "0x08000000"),
        "size": _hex(attrs.get("size"), "0x00000000"),
        "ram_start": _hex(attrs.get("RAMstart"), "0x20000000"),
        "ram_size": _hex(attrs.get("RAMsize"), "0x00000000"),
        "default": attrs.get("default", "") == "1",
        "pdsc": str(pdsc),
    }


def _find_device_element(subfamily: ET.Element, device_name: str) -> ET.Element | None:
    normalized = _norm_device(device_name)
    for dev in subfamily.findall("device"):
        if _norm_device(dev.attrib.get("Dname")) == normalized:
            return dev
    for dev in subfamily.findall("device"):
        dname = _norm_device(dev.attrib.get("Dname"))
        if dname and normalized.startswith(dname.rstrip("X")):
            return dev
    return None


def _find_device_memory(subfamily: ET.Element, device_name: str) -> list[dict]:
    target = _find_device_element(subfamily, device_name)
    if target is None:
        return []
    regions = []
    for mem in target.findall("memory"):
        start = mem.attrib.get("start")
        size = mem.attrib.get("size")
        if start and size:
            regions.append(
                {
                    "name": mem.attrib.get("name", "memory"),
                    "start": _hex(start),
                    "size": _hex(size),
                }
            )
    return regions


def _discover_from_pdsc(device: str, pack_roots: list[Path]) -> dict | None:
    normalized = _norm_device(device)
    if not normalized:
        return None

    for pdsc in _iter_pdsc_files(pack_roots):
        try:
            root = ET.parse(pdsc).getroot()
        except (ET.ParseError, OSError):
            continue
        for subfamily in root.iter("subFamily"):
            prefix = subfamily.attrib.get("DsubFamily", "")
            if not prefix or not normalized.startswith(prefix.upper()):
                continue

            device_names = [
                dev.attrib.get("Dname", "")
                for dev in subfamily.findall("device")
                if dev.attrib.get("Dname")
            ]
            if device_names and normalized not in {_norm_device(d) for d in device_names}:
                if not any(normalized.startswith(_norm_device(d).rstrip("X")) for d in device_names):
                    continue

            # <algorithm> may sit directly under <subFamily> or under each
            # <device> (e.g. Keil.STM32F4xx_DFP declares per-device). The
            # device-level entry is the more specific match and wins; keep
            # subFamily-level entries only as fallback.
            device_el = _find_device_element(subfamily, device)
            algorithm_parents = (
                [device_el, subfamily] if device_el is not None else [subfamily]
            )
            algorithms: list[dict] = []
            for parent in algorithm_parents:
                algorithms = [
                    _algorithm_record(alg.attrib, pdsc)
                    for alg in parent.findall("algorithm")
                    if _is_internal_algorithm(alg.attrib)
                ]
                if algorithms:
                    break
            processor = subfamily.find("processor")
            dclock = None
            if processor is not None:
                dclock = processor.attrib.get("Dclock")
            return {
                "device": device,
                "device_prefix": prefix,
                "profile_key": _profile_key(prefix),
                "pdsc": str(pdsc),
                "algorithms": algorithms,
                "cpu_freq_default": int(dclock) if dclock and dclock.isdigit() else None,
                "regions": _find_device_memory(subfamily, device),
            }
    return None


def _flm_basename(name: str) -> str:
    return Path(name.replace("\\", "/")).name


def _find_flm_source(algorithm_name: str, pack_roots: list[Path], keil_flash_roots: list[Path]) -> Path | None:
    basename = _flm_basename(algorithm_name)
    rel_parts = [p for p in algorithm_name.replace("\\", "/").split("/") if p]

    for root in pack_roots:
        root = Path(root)
        if root.is_file():
            root = root.parent
        if not root.is_dir():
            continue
        if rel_parts:
            tail = Path(*rel_parts)
            for candidate in root.rglob(basename):
                try:
                    if candidate.is_file() and str(candidate).replace("\\", "/").endswith(str(tail).replace("\\", "/")):
                        return candidate
                except OSError:
                    continue
        for candidate in root.rglob(basename):
            if candidate.is_file():
                return candidate

    for root in keil_flash_roots:
        candidate = Path(root) / basename
        if candidate.is_file():
            return candidate
    return None


def _select_algorithm(discovered: dict, flm: str | None) -> tuple[dict | None, list[dict]]:
    candidates = discovered.get("algorithms", [])
    if flm:
        needle = flm.replace("\\", "/").lower()
        for candidate in candidates:
            name = candidate["name"].lower()
            if needle == name or needle == _flm_basename(name).lower():
                return candidate, candidates
        return None, candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def _build_profile(discovered: dict, algorithm: dict, idcode: int | None = None) -> dict:
    regions = discovered.get("regions") or [
        {"name": "flash", "start": algorithm["start"], "size": algorithm["size"]},
        {"name": "ram", "start": algorithm["ram_start"], "size": algorithm["ram_size"]},
    ]
    profile = {
        "name": discovered["device_prefix"],
        "idcode_pattern": f"0x{idcode:08X}" if idcode else "",
        "device_prefix": discovered["device_prefix"],
        "flm_path": f"FLM/{_flm_basename(algorithm['name'])}",
        "flash_base": algorithm["start"],
        "ram_base": algorithm["ram_start"],
        "page_size": 128,
        "sector_size": 2048,
        "flash_size": _size_label(algorithm["size"]),
        "swd_clock_default": 10000000,
        "rtt_default": {
            "addr": algorithm["ram_start"],
            "search_size": 1024,
            "channel": 0,
        },
        "regions": regions,
    }
    if discovered.get("cpu_freq_default"):
        profile["cpu_freq_default"] = discovered["cpu_freq_default"]
    return profile


def _read_idcode(port: str | None) -> int | None:
    if not port:
        return None
    try:
        from mklink.flash import MKLinkFlash

        flash = MKLinkFlash.connect(port)
        try:
            return flash.get_idcode()
        finally:
            flash.close()
    except Exception:
        return None


def detect_mcu_profile(
    *,
    project_root: str = ".",
    device: str | None = None,
    project_info: dict | None = None,
    profiles_path: str | Path | None = None,
    pack_roots: list[str | Path] | None = None,
    keil_flash_roots: list[str | Path] | None = None,
    microkeen_flm_dir: str | Path | None = None,
    flm: str | None = None,
    port: str | None = None,
    write_profile: bool = True,
    copy_flm: bool = True,
    read_idcode: bool = False,
) -> dict[str, Any]:
    """Detect or create an MCU profile for a project/device.

    The function is intentionally side-effect free until a single internal
    flash algorithm and a local FLM file have been resolved.
    """
    from mklink.profiles import match_mcu_by_device

    profiles_file = Path(profiles_path) if profiles_path else default_profiles_path()
    data = _load_profile_data(profiles_file)
    profiles = data.get("mcus", {})

    if project_info is None:
        try:
            from mklink.project_config import load_project_info

            project_info = load_project_info(project_root) or {}
        except Exception:
            project_info = {}
    device_name = device or (project_info or {}).get("device", "")
    if not device_name:
        return {"status": "error", "message": "No MCU device name available"}

    existing = match_mcu_by_device(device_name, profiles)
    if existing and existing != "custom":
        return {
            "status": "matched",
            "device": device_name,
            "profile_key": existing,
            "profile": profiles[existing],
        }

    roots = [Path(p) for p in (pack_roots if pack_roots is not None else default_pack_roots())]
    flash_roots = [
        Path(p)
        for p in (
            keil_flash_roots if keil_flash_roots is not None else default_keil_flash_roots()
        )
    ]
    discovered = _discover_from_pdsc(device_name, roots)
    if not discovered:
        return {
            "status": "unsupported",
            "device": device_name,
            "message": f"No local Keil/Arm PDSC entry found for {device_name}",
        }

    selected, candidates = _select_algorithm(discovered, flm)
    if not selected and flm:
        return {
            "status": "error",
            "device": device_name,
            "message": f"Requested FLM {flm!r} is not an internal flash algorithm for {device_name}",
            "candidates": candidates,
        }
    if not selected:
        if candidates:
            message = "Multiple internal flash algorithms found; specify one with flm/--flm."
        else:
            message = "No internal flash algorithm found in the matched pack entry."
        return {
            "status": "needs_selection",
            "device": device_name,
            "profile_key": discovered["profile_key"],
            "candidates": candidates,
            "message": message,
        }

    basename = _flm_basename(selected["name"])
    flm_source = _find_flm_source(selected["name"], roots, flash_roots)
    if microkeen_flm_dir is None and copy_flm:
        try:
            from mklink.discovery import get_microkeen_flm_path

            maybe = get_microkeen_flm_path()
            if maybe:
                microkeen_flm_dir = maybe
        except Exception:
            microkeen_flm_dir = None

    microkeen_path = Path(microkeen_flm_dir) / basename if microkeen_flm_dir else None
    copied = False
    if not flm_source and not (microkeen_path and microkeen_path.is_file()):
        return {
            "status": "missing_flm",
            "device": device_name,
            "profile_key": discovered["profile_key"],
            "selected_algorithm": selected,
            "required_flm": basename,
            "message": (
                "Install or unpack the Keil/Arm device pack that contains "
                f"{selected['name']}, then rerun mcu-detect/project-init."
            ),
        }

    if copy_flm and microkeen_path is None:
        return {
            "status": "missing_microkeen",
            "device": device_name,
            "profile_key": discovered["profile_key"],
            "selected_algorithm": selected,
            "required_flm": basename,
            "message": "MICROKEEN FLM directory was not found.",
        }

    if copy_flm and microkeen_path is not None:
        microkeen_path.parent.mkdir(parents=True, exist_ok=True)
        if not microkeen_path.exists():
            if flm_source is None:
                return {
                    "status": "missing_flm",
                    "device": device_name,
                    "profile_key": discovered["profile_key"],
                    "selected_algorithm": selected,
                    "required_flm": basename,
                    "message": f"FLM {basename} is not available locally for copying.",
                }
            shutil.copy2(flm_source, microkeen_path)
            copied = True

    idcode = _read_idcode(port) if read_idcode else None
    profile = _build_profile(discovered, selected, idcode=idcode)
    backup = None
    if write_profile:
        profiles[discovered["profile_key"]] = profile
        data["mcus"] = profiles
        backup = _save_profile_data(profiles_file, data)

    return {
        "status": "created",
        "device": device_name,
        "profile_key": discovered["profile_key"],
        "profile": profile,
        "selected_algorithm": selected,
        "flm_source": str(flm_source) if flm_source else None,
        "microkeen_flm": str(microkeen_path) if microkeen_path else None,
        "flm_copied": copied,
        "profile_written": bool(write_profile),
        "profile_path": str(profiles_file),
        "backup_path": str(backup) if backup else None,
    }

