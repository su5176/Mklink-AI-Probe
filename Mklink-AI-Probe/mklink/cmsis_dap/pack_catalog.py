"""Search installed and cached CMSIS-Pack targets without hardware access."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .models import TargetRecord
from .paths import PackPaths
from .pyocd_runtime import import_pyocd_attr


@dataclass(frozen=True)
class PackCatalogStatus:
    last_error: Optional[str]
    index_available: bool
    target_count: int


def _hpm_rom_records() -> List[TargetRecord]:
    from mklink.hpm_config import HPM_ROM_TARGETS

    return [
        TargetRecord(part_number, "HPMicro", installed=True, source="hpm-rom-api")
        for part_number in HPM_ROM_TARGETS
    ]


def _production_builtin_provider() -> Iterable[TargetRecord]:
    """Load pyOCD's builtin target registry only when builtin targets are needed."""

    from .builtin_pack_bundle import load_builtin_pack_records
    from .builtin_flm_bundle import load_builtin_flm_targets
    TARGET = import_pyocd_attr("pyocd.target", "TARGET")

    if hasattr(TARGET, "items"):
        entries = TARGET.items()
    else:
        names = TARGET.get_all_target_names()
        entries = ((name, TARGET[name]) for name in names)

    records = _hpm_rom_records()
    records.extend(load_builtin_pack_records())
    records.extend(load_builtin_flm_targets())
    for name, target_type in entries:
        part_number = getattr(target_type, "PART_NUMBER", None) or name
        vendor = getattr(target_type, "VENDOR", "") or ""
        family = getattr(target_type, "FAMILY", "") or ""
        series = getattr(target_type, "SERIES", "") or ""
        records.append(
            TargetRecord(
                part_number=str(part_number),
                vendor=str(vendor),
                installed=True,
                source="builtin",
                family=str(family),
                series=str(series),
            )
        )
    return records


class PackCatalog:
    """Merged view of pyOCD builtin targets and the last cached pack index."""

    def __init__(
        self,
        paths: PackPaths,
        builtin_provider: Callable[[], Iterable[TargetRecord]] = _production_builtin_provider,
    ) -> None:
        self._paths = paths
        self._builtin_provider = builtin_provider
        self._builtin_records = None  # type: Optional[List[TargetRecord]]
        self._refresh_error = None  # type: Optional[str]
        self._refresh_error_signature = None  # type: Optional[Tuple[int, int]]
        self._index_error = None  # type: Optional[str]
        self._state_error = None  # type: Optional[str]
        self._index_available = False
        self._index_loaded = False
        self._index_signature = None  # type: Optional[Tuple[int, int]]
        self._index_records = []  # type: List[TargetRecord]
        self._state_loaded = False
        self._state_signature = None  # type: Optional[Tuple[int, int]]
        self._installed_paths = {}  # type: Dict[Tuple[str, str], str]
        self._target_count = 0

    def note_refresh_failure(self, error: object) -> None:
        """Record a refresh failure without modifying the last-good index."""

        self._refresh_error = str(error)
        self._refresh_error_signature = self._file_signature(self._paths.index_file)

    def status(self) -> PackCatalogStatus:
        return PackCatalogStatus(
            last_error=self._refresh_error or self._index_error or self._state_error,
            index_available=self._index_available,
            target_count=self._target_count,
        )

    def refresh(self) -> PackCatalogStatus:
        """Reload cached metadata and return status for the new snapshot."""
        self._index_loaded = False
        self._state_loaded = False
        self.search("", limit=1)
        return self.status()

    def search(
        self,
        query: str,
        vendor: Optional[str] = None,
        installed: Optional[bool] = None,
        limit: int = 100,
    ) -> List[TargetRecord]:
        if limit <= 0:
            return []

        if self._builtin_records is None:
            self._builtin_records = list(self._builtin_provider())
        builtin_records = self._builtin_records
        cached_records = self._read_cached_records()
        installed_paths = self._read_installed_paths()
        cached_records = [
            self._apply_installed_path(record, installed_paths)
            for record in cached_records
        ]
        records = self._merge_records(builtin_records, cached_records)
        self._target_count = len(records)

        needle = query.casefold().strip()
        vendor_key = vendor.casefold().strip() if vendor is not None else None
        matches = [
            record
            for record in records
            if self._matches_query(record, needle)
            and (vendor_key is None or record.vendor.casefold() == vendor_key)
            and (installed is None or record.installed is installed)
        ]
        matches.sort(key=lambda record: (record.part_number.casefold(), record.pack_id or ""))
        return matches[:limit]

    def _read_cached_records(self) -> List[TargetRecord]:
        signature = self._file_signature(self._paths.index_file)
        if self._index_loaded and signature == self._index_signature:
            return self._index_records

        if signature is None:
            self._index_error = "cached pack index unavailable: file does not exist"
            return self._index_records

        try:
            with self._paths.index_file.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            records = self._parse_index(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._index_error = "cached pack index unavailable: {}".format(error)
            return self._index_records

        self._index_available = True
        self._index_loaded = True
        self._index_signature = signature
        self._index_error = None
        self._index_records = records
        if (
            self._refresh_error is not None
            and signature != self._refresh_error_signature
        ):
            self._refresh_error = None
            self._refresh_error_signature = None
        return self._index_records

    def _read_installed_paths(self) -> Dict[Tuple[str, str], str]:
        signature = self._file_signature(self._paths.state_file)
        if self._state_loaded and signature == self._state_signature:
            return self._installed_paths

        if signature is None:
            self._state_error = (
                "installed pack state unavailable: file does not exist"
                if self._state_loaded
                else None
            )
            return self._installed_paths

        try:
            with self._paths.state_file.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._state_error = "installed pack state unavailable: {}".format(error)
            return self._installed_paths

        installed = payload.get("installed") if isinstance(payload, Mapping) else None
        if not isinstance(installed, Mapping):
            self._state_error = "installed pack state must contain an installed mapping"
            return self._installed_paths

        paths = {}
        for pack_id, versions in installed.items():
            if not isinstance(versions, Mapping):
                continue
            for version, pack_path in versions.items():
                if isinstance(pack_path, str):
                    paths[(str(pack_id), str(version))] = pack_path
        self._state_loaded = True
        self._state_signature = signature
        self._state_error = None
        self._installed_paths = paths
        return self._installed_paths

    @staticmethod
    def _file_signature(path: Path) -> Optional[Tuple[int, int]]:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _apply_installed_path(
        self,
        record: TargetRecord,
        installed_paths: Mapping[Tuple[str, str], str],
    ) -> TargetRecord:
        from .pack_manager import resolve_managed_pack_path

        if record.pack_id is None or record.pack_version is None:
            return record
        pack_path = installed_paths.get((record.pack_id, record.pack_version))
        resolved = resolve_managed_pack_path(self._paths, pack_path)
        if resolved is None:
            return record
        return replace(record, installed=True, pack_path=str(resolved))

    @classmethod
    def _parse_index(cls, payload: object) -> List[TargetRecord]:
        if not isinstance(payload, Mapping):
            raise ValueError("cached pack index must be a mapping")

        candidates = payload
        for container_name in ("targets", "devices"):
            container = payload.get(container_name)
            if isinstance(container, Mapping):
                candidates = container
                break

        records = []
        for part_number, details in candidates.items():
            if not isinstance(details, Mapping):
                continue
            records.append(cls._record_from_index(str(part_number), details))
        return records

    @staticmethod
    def _record_from_index(part_number: str, details: Mapping) -> TargetRecord:
        pack_details = details.get("from_pack")
        if not isinstance(pack_details, Mapping):
            pack_details = {}

        vendor = pack_details.get("vendor") or details.get("vendor") or ""
        pack_name = (
            pack_details.get("pack")
            or pack_details.get("name")
            or details.get("pack")
            or ""
        )
        version = pack_details.get("version") or details.get("version")
        explicit_pack_id = details.get("pack_id")
        family = PackCatalog._metadata_text(details.get("family"))
        series = PackCatalog._metadata_text(
            details.get("series"),
            details.get("sub_family"),
            details.get("subfamily"),
        )

        if explicit_pack_id:
            pack_id = str(explicit_pack_id)
        elif pack_name and vendor:
            prefix = "{}.".format(vendor)
            pack_text = str(pack_name)
            pack_id = pack_text if pack_text.casefold().startswith(prefix.casefold()) else prefix + pack_text
        elif pack_name:
            pack_id = str(pack_name)
        else:
            pack_id = None

        return TargetRecord(
            part_number=part_number,
            vendor=str(vendor),
            pack_id=pack_id,
            pack_version=str(version) if version is not None else None,
            installed=False,
            source="index",
            family=family,
            series=series,
        )

    @staticmethod
    def _metadata_text(*values: object) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _matches_query(record: TargetRecord, needle: str) -> bool:
        return any(
            needle in value.casefold()
            for value in (
                record.part_number,
                record.vendor,
                record.family,
                record.series,
            )
        )

    @staticmethod
    def _merge_records(
        builtin_records: Iterable[TargetRecord],
        cached_records: Iterable[TargetRecord],
    ) -> List[TargetRecord]:
        selected = {}  # type: Dict[str, TargetRecord]
        for record in list(builtin_records) + list(cached_records):
            key = record.part_number.casefold()
            current = selected.get(key)
            if current is None:
                selected[key] = record
                continue
            if PackCatalog._priority(record) > PackCatalog._priority(current):
                primary, secondary = record, current
            else:
                primary, secondary = current, record
            selected[key] = PackCatalog._merge_search_metadata(primary, secondary)
        return list(selected.values())

    @staticmethod
    def _merge_search_metadata(
        primary: TargetRecord,
        secondary: TargetRecord,
    ) -> TargetRecord:
        updates = {}
        for field_name in ("vendor", "family", "series"):
            if not getattr(primary, field_name) and getattr(secondary, field_name):
                updates[field_name] = getattr(secondary, field_name)
        return replace(primary, **updates) if updates else primary

    @staticmethod
    def _priority(record: TargetRecord) -> Tuple[bool, int, bool]:
        if record.source == "index" and record.installed and record.pack_path is not None:
            source_priority = 4
        elif record.source == "hpm-rom-api":
            source_priority = 4
        elif record.source == "bundle":
            source_priority = 3
        elif record.source == "daplink-builtin":
            source_priority = 2
        elif record.source == "builtin":
            source_priority = 2
        else:
            source_priority = 1
        return record.installed, source_priority, record.pack_path is not None
