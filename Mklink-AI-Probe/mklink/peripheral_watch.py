"""CMSIS-Pack device/SVD selection and read-only peripheral watch items."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile

from mklink.superwatch import WatchItem

MAX_XML_BYTES = 32 * 1024 * 1024


def _xml(data: bytes):
    if len(data) > MAX_XML_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Unsupported or oversized Pack XML")
    root = ET.fromstring(data)
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    return root


def _member(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in str(path):
        raise ValueError("SVD path must stay inside its Pack")
    return str(path)


@dataclass(frozen=True)
class SvdTarget:
    target: str
    pack: str
    source: Path
    svd: str
    archive: bool = False

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.source}|{self.target}|{self.svd}".encode()).hexdigest()[:24]

    def public(self) -> dict:
        return {"id": self.key, "target": self.target, "pack": self.pack, "svd": self.svd}

    def read(self) -> bytes:
        member = _member(self.svd)
        if self.archive:
            with ZipFile(self.source) as archive:
                if archive.getinfo(member).file_size > MAX_XML_BYTES:
                    raise ValueError("SVD is too large")
                return archive.read(member)
        root = self.source.parent.resolve()
        path = (root / member).resolve()
        path.relative_to(root)
        if path.stat().st_size > MAX_XML_BYTES:
            raise ValueError("SVD is too large")
        return path.read_bytes()


def pdsc_targets(data: bytes, source: Path, *, archive: bool = False) -> list[SvdTarget]:
    root = _xml(data)
    pack = f"{root.findtext('vendor', '')}.{root.findtext('name', '')}"
    version = root.find("./releases/release")
    if version is not None:
        pack += "@" + version.get("version", "")
    results = []

    def visit(element, inherited_svd=""):
        svd = inherited_svd
        for debug in element.findall("debug"):
            if debug.get("svd"):
                svd = debug.get("svd")
                break
        target = element.get("Dvariant") or element.get("Dname")
        if target and svd:
            try:
                results.append(SvdTarget(target, pack, source, _member(svd), archive))
            except ValueError:
                pass
        for child in element:
            if child.tag in {"devices", "family", "subFamily", "device", "variant"}:
                visit(child, svd)

    visit(root)
    return results


def discover_svd_targets(project_root: str) -> list[SvdTarget]:
    """Inspect installed Packs only; never download or guess SVD filenames."""
    from mklink.project_config import load_project_info
    from mklink.cmsis_dap.paths import PackPaths
    from mklink.cmsis_dap.algorithm_catalog import _installed_pack_records
    from mklink.cmsis_dap.builtin_pack_bundle import load_builtin_pack_records

    info = load_project_info(project_root) or {}
    roots = {Path.home() / "AppData/Local/Arm/Packs", Path.home() / ".cache/arm/packs"}
    for name in ("CMSIS_PACK_ROOT", "ARM_PACK_ROOT"):
        if os.environ.get(name):
            roots.add(Path(os.environ[name]))
    flm = info.get("flm_path")
    descriptors = set()
    if flm:
        for parent in list(Path(flm).parents)[:4]:
            matches = list(parent.glob("*.pdsc"))
            if matches:
                descriptors.update(matches)
                if len(parent.parents) >= 3:
                    roots.add(parent.parents[2])
                break
    for root in roots:
        if root.is_dir():
            descriptors.update(root.glob("*/*/*/*.pdsc"))
    targets = []
    for descriptor in sorted(descriptors):
        try:
            if descriptor.stat().st_size <= MAX_XML_BYTES:
                targets.extend(pdsc_targets(descriptor.read_bytes(), descriptor))
        except (OSError, ValueError, ET.ParseError):
            continue
    records = _installed_pack_records(PackPaths(), "")
    records.extend(load_builtin_pack_records())
    archive_members = {}
    for source in {Path(record.pack_path) for record in records if record.pack_path}:
        try:
            with ZipFile(source) as archive:
                archive_members[source] = set(archive.namelist())
                for item in archive.infolist():
                    if item.filename.endswith('.pdsc') and item.file_size <= MAX_XML_BYTES:
                        targets.extend(pdsc_targets(archive.read(item), source, archive=True))
        except (OSError, ValueError, ET.ParseError, BadZipFile):
            continue
    available = [target for target in targets if (
        target.svd in archive_members.get(target.source, set()) if target.archive
        else (target.source.parent / target.svd).is_file()
    )]
    unique = {}
    for target in available:
        unique.setdefault((target.target, target.pack, target.svd), target)
    return sorted(unique.values(), key=lambda t: (t.target, t.pack))


def svd_watch_items(data: bytes) -> tuple[dict[str, WatchItem], int]:
    from .cmsis_dap.pyocd_runtime import import_pyocd_module

    SVDParser = import_pyocd_module('pyocd.debug.svd.parser').SVDParser

    root = _xml(data)
    device = SVDParser.for_xml_file(io.BytesIO(ET.tostring(root)), remove_reserved=True).get_device()
    items = {}
    skipped = 0
    for peripheral in device.peripherals:
        for register in peripheral.registers or []:
            fields = register.fields or []
            width = register.size
            if (width not in (8, 16, 32) or register.access not in ('read-only', 'read-write', 'read-writeOnce')
                    or register.read_action or any(field.read_action for field in fields)):
                skipped += 1
                continue
            address = peripheral.base_address + register.address_offset
            if not 0 <= address <= 0xFFFFFFFF - width // 8 + 1 or address % (width // 8):
                skipped += 1
                continue
            name = f"{peripheral.name}.{register.name}"
            metadata = {"writable": False, "register": name, "description": register.description or ""}
            items[name] = WatchItem(name, address, f"uint{width}_t", width // 8,
                                    source="peripheral", scalar_kind="unsigned", metadata=metadata)
            for field in fields:
                offset, bits = field.bit_offset, field.bit_width
                if (not isinstance(bits, int) or not isinstance(offset, int) or bits <= 0
                        or offset < 0 or offset + bits > width
                        or field.access in ('write-only', 'writeOnce')):
                    continue
                field_name = f"{name}.{field.name}"
                field_meta = {**metadata, "bit_offset": offset, "bit_width": bits,
                              "description": field.description or ""}
                items[field_name] = WatchItem(field_name, address, "bool" if bits == 1 else f"uint{width}_t", width // 8,
                                              source="peripheral", scalar_kind="unsigned", metadata=field_meta)
                if re.fullmatch(r"GPIO[A-Z]", peripheral.name) and register.name == "IDR" and bits == 1:
                    alias = f"{peripheral.name}.{offset}"
                    items[alias] = WatchItem(alias, address, "bool", width // 8,
                                            source="peripheral", scalar_kind="unsigned", metadata=field_meta)
    return items, skipped
