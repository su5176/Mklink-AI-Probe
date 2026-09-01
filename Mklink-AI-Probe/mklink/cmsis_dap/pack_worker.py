"""Subprocess implementation for CMSIS-Pack downloads and imports.

Standard output is reserved for newline-delimited JSON protocol messages.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Callable, Dict, IO, List, Mapping, Optional, Sequence, Tuple, Type
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import xml.etree.ElementTree as ElementTree
from zipfile import BadZipFile, ZipFile

from cmsis_pack_manager import Cache

from .errors import FlashErrorCode
from .pack_manager import _canonical_pack_path, _normalize_pack_identity
from .paths import PackPaths


EventEmitter = Callable[[Dict[str, object]], None]
_MAX_PACK_DOWNLOAD_BYTES = 1024 * 1024 * 1024
_PACK_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_PACK_PDSC_BYTES = 16 * 1024 * 1024


class WorkerFailure(Exception):
    def __init__(self, code: FlashErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_secure_https_url(value: object) -> bool:
    try:
        parsed = urlparse(str(value))
        return (
            parsed.scheme.casefold() == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except (UnicodeError, ValueError):
        return False


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_secure_https_url(newurl):
            raise WorkerFailure(
                FlashErrorCode.PACK_DOWNLOAD_FAIL,
                "pack download redirected outside HTTPS",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_https_request(request: Request, timeout: int):
    return build_opener(_HTTPSOnlyRedirectHandler()).open(
        request, timeout=timeout
    )


class ReportingCache(Cache):
    """CMSIS Pack Manager cache whose progress is JSON-protocol-safe."""

    def __init__(
        self,
        silent: bool,
        no_timeouts: bool,
        json_path: str,
        data_path: str,
        emitter: EventEmitter,
    ) -> None:
        self._event_emitter = emitter
        super().__init__(
            silent,
            no_timeouts,
            json_path=json_path,
            data_path=data_path,
        )

    def _verbose_on_tick_fn(self, total: int, current: int) -> None:
        self._event_emitter(
            {"type": "progress", "current": current, "total": total}
        )


def _resolved_child(path: Path, parent: Path, description: str) -> Path:
    resolved_parent = parent.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_parent)
    except ValueError:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "{} is outside its managed directory".format(description),
        )
    if resolved_path == resolved_parent:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "{} must not be the managed directory itself".format(description),
        )
    return resolved_path


def _pack_metadata(device: Mapping[str, object]) -> Tuple[str, str, str]:
    from_pack = device.get("from_pack")
    if not isinstance(from_pack, Mapping):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "selected device has no pack metadata",
        )
    values = tuple(from_pack.get(key) for key in ("vendor", "pack", "version"))
    if not all(isinstance(value, str) and value for value in values):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "selected device has incomplete pack metadata",
        )
    return str(values[0]), str(values[1]), str(values[2])


def _ref_metadata(pack_ref: object) -> Tuple[str, str, str]:
    values = tuple(getattr(pack_ref, key, None) for key in ("vendor", "pack", "version"))
    if not all(isinstance(value, str) and value for value in values):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "pack reference has incomplete metadata",
        )
    return str(values[0]), str(values[1]), str(values[2])


def _read_json_object(path: Path) -> Dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "pack metadata is invalid: {}".format(error),
        )
    if not isinstance(value, dict):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "pack metadata must be an object",
        )
    return value


def _write_transaction_journal(paths: PackPaths, value: Mapping[str, object]) -> None:
    path = _transaction_journal(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / "pack-transaction.json.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    finally:
        if temporary.exists():
            temporary.unlink()


def _transaction_journal(paths: PackPaths) -> Path:
    return paths.root / "pack-transaction.json"


def _transaction_entries(
    paths: PackPaths,
    replacements: Sequence[Tuple[Path, Path]],
    staging_dir: Path,
) -> List[Dict[str, object]]:
    staging = _resolved_child(staging_dir, paths.staging_dir, "transaction staging")
    if staging.parent != paths.staging_dir.resolve() or not staging.is_dir():
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction staging must be an existing direct child",
        )
    staging_device = staging.stat().st_dev
    entries = []
    for index, (temporary, target) in enumerate(replacements):
        target = _resolved_child(Path(target), paths.root, "transaction target")
        temporary = _resolved_child(
            Path(temporary), paths.root, "transaction temporary file"
        )
        if staging not in temporary.parents or not temporary.is_file():
            raise WorkerFailure(
                FlashErrorCode.PACK_INTEGRITY_ERROR,
                "transaction prepared file must exist inside unique staging",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.stat().st_dev != staging_device:
            raise WorkerFailure(
                FlashErrorCode.PACK_INTEGRITY_ERROR,
                "transaction staging and targets must share a volume",
            )
        backup = staging / "{:03}.backup".format(index)
        entries.append(
            {
                "target": str(target),
                "prepared": str(temporary),
                "backup": str(backup),
                "original_exists": target.exists(),
            }
        )
    if not entries:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction has no files",
        )
    return entries


def _entry_paths(
    paths: PackPaths,
    staging: Path,
    entry: Mapping[str, object],
) -> Tuple[Path, Path, Path, bool]:
    values = tuple(entry.get(key) for key in ("target", "prepared", "backup"))
    if not all(isinstance(value, str) and value for value in values):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction journal path is invalid",
        )
    target = _resolved_child(Path(str(values[0])), paths.root, "journal target")
    prepared = _resolved_child(Path(str(values[1])), staging, "journal prepared")
    backup = _resolved_child(Path(str(values[2])), staging, "journal backup")
    if staging not in prepared.parents or staging not in backup.parents:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction artifacts must remain inside staging",
        )
    original_exists = entry.get("original_exists")
    if not isinstance(original_exists, bool):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction journal target state is invalid",
        )
    return target, prepared, backup, original_exists


def _unlink_transaction_file(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction path is not a file",
        )
    current_mode = path.stat().st_mode
    if not current_mode & stat.S_IWRITE:
        path.chmod(current_mode | stat.S_IWRITE)
    path.unlink()


def _rollback_entries(
    paths: PackPaths,
    staging: Path,
    entries: Sequence[Mapping[str, object]],
    replace: Callable[[object, object], object],
) -> None:
    for entry in reversed(entries):
        target, prepared, backup, original_exists = _entry_paths(
            paths, staging, entry
        )
        if backup.is_file():
            _unlink_transaction_file(target)
            replace(str(backup), str(target))
        elif not original_exists:
            _unlink_transaction_file(target)
        _unlink_transaction_file(prepared)
        _unlink_transaction_file(backup)


def _journal_result(payload: Mapping[str, object]) -> Dict[str, object]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction journal result is invalid",
        )
    if result.get("status") == "updated":
        target_count = result.get("target_count")
        if type(target_count) is not int or target_count < 0:
            raise WorkerFailure(
                FlashErrorCode.PACK_INTEGRITY_ERROR,
                "transaction index result is incomplete",
            )
        return dict(result)
    required = ("status", "pack_id", "version", "pack_path")
    if any(not result.get(key) for key in required):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction journal result is incomplete",
        )
    return dict(result)


def _state_matches_result(paths: PackPaths, result: Mapping[str, object]) -> bool:
    if result.get("status") == "updated":
        return paths.index_file.is_file() and paths.aliases_file.is_file()
    if not paths.state_file.is_file() or not Path(str(result["pack_path"])).is_file():
        return False
    try:
        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        registered = state["installed"][str(result["pack_id"])][str(result["version"])]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return Path(str(registered)).resolve() == Path(str(result["pack_path"])).resolve()


def _finalize_entries(
    paths: PackPaths,
    staging: Path,
    entries: Sequence[Mapping[str, object]],
) -> None:
    for entry in entries:
        _, prepared, backup, _ = _entry_paths(paths, staging, entry)
        _unlink_transaction_file(prepared)
        _unlink_transaction_file(backup)
    _transaction_journal(paths).unlink()


def recover_pending_transaction(
    paths: PackPaths,
    preserve_committed: bool = False,
) -> Optional[Dict[str, object]]:
    journal = _transaction_journal(paths)
    if not journal.exists():
        journal_temp = paths.root / "pack-transaction.json.tmp"
        if journal_temp.is_file():
            journal_temp.unlink()
        return
    payload = _read_json_object(journal)
    result = _journal_result(payload)
    staging_value = payload.get("staging_dir")
    entries = payload.get("entries")
    phase = payload.get("phase")
    if (
        not isinstance(staging_value, str)
        or phase not in ("prepared", "committing", "committed")
        or not isinstance(entries, list)
        or not all(
        isinstance(entry, Mapping) for entry in entries
        )
    ):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "transaction journal is invalid",
        )
    staging = _resolved_child(
        Path(staging_value), paths.staging_dir, "journal staging"
    )
    if staging.parent != paths.staging_dir.resolve():
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "journal staging must be a direct child",
        )
    if phase == "committed":
        if preserve_committed:
            return result
        if _state_matches_result(paths, result):
            _finalize_entries(paths, staging, entries)
        else:
            _rollback_entries(paths, staging, entries, os.replace)
            journal.unlink()
    elif phase == "committing":
        _rollback_entries(paths, staging, entries, os.replace)
        journal.unlink()
    else:
        for entry in entries:
            _, prepared, backup, _ = _entry_paths(paths, staging, entry)
            _unlink_transaction_file(prepared)
            _unlink_transaction_file(backup)
        journal.unlink()
    return None


def _load_matching_committed(
    paths: PackPaths, result: Mapping[str, object]
) -> Tuple[Path, List[Mapping[str, object]]]:
    payload = _read_json_object(_transaction_journal(paths))
    embedded = _journal_result(payload)
    if payload.get("phase") != "committed" or embedded != dict(result):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "committed transaction result does not match",
        )
    staging = _resolved_child(
        Path(str(payload.get("staging_dir"))), paths.staging_dir, "journal staging"
    )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR, "transaction entries are invalid"
        )
    return staging, entries


def acknowledge_committed_transaction(
    paths: PackPaths, result: Mapping[str, object]
) -> None:
    staging, entries = _load_matching_committed(paths, result)
    _finalize_entries(paths, staging, entries)


def rollback_committed_transaction(
    paths: PackPaths, result: Mapping[str, object]
) -> None:
    staging, entries = _load_matching_committed(paths, result)
    _rollback_entries(paths, staging, entries, os.replace)
    _transaction_journal(paths).unlink()


def _recover_transaction(paths: PackPaths) -> None:
    recover_pending_transaction(paths)


def _commit_transaction(
    paths: PackPaths,
    replacements: Sequence[Tuple[Path, Path]],
    staging_dir: Path,
    result: Mapping[str, object],
    replace: Optional[Callable[[object, object], object]] = None,
) -> None:
    replace_fn = replace if replace is not None else os.replace
    recover_pending_transaction(paths)
    staging = _resolved_child(staging_dir, paths.staging_dir, "transaction staging")
    entries = _transaction_entries(paths, replacements, staging)
    journal = _transaction_journal(paths)
    payload = {
        "phase": "prepared",
        "staging_dir": str(staging),
        "result": dict(result),
        "entries": entries,
    }
    try:
        _write_transaction_journal(paths, payload)
        payload["phase"] = "committing"
        _write_transaction_journal(paths, payload)
    except BaseException:
        for entry in entries:
            _, prepared, backup, _ = _entry_paths(paths, staging, entry)
            _unlink_transaction_file(prepared)
            _unlink_transaction_file(backup)
        if journal.is_file():
            journal.unlink()
        raise
    try:
        for entry in entries:
            target, _, backup, original_exists = _entry_paths(paths, staging, entry)
            if original_exists:
                replace_fn(str(target), str(backup))
        for entry in entries:
            target, prepared, _, _ = _entry_paths(paths, staging, entry)
            replace_fn(str(prepared), str(target))
        payload["phase"] = "committed"
        _write_transaction_journal(paths, payload)
    except BaseException:
        try:
            _rollback_entries(paths, staging, entries, replace_fn)
        except BaseException:
            raise
        else:
            if journal.exists():
                journal.unlink()
        raise


def _merged_metadata(
    stage_index_dir: Path,
    paths: PackPaths,
    expected: Tuple[str, str, str],
) -> List[Tuple[Path, Dict[str, object]]]:
    stage_index_file = stage_index_dir / "index.json"
    stage_aliases_file = stage_index_dir / "aliases.json"
    if not stage_index_file.is_file() or not stage_aliases_file.is_file():
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "worker must produce both index.json and aliases.json",
        )
    staged_index = _read_json_object(stage_index_file)
    staged_aliases = _read_json_object(stage_aliases_file)
    metadata = []
    for device in staged_index.values():
        if not isinstance(device, Mapping):
            raise WorkerFailure(
                FlashErrorCode.PACK_INTEGRITY_ERROR,
                "pack index device metadata must be an object",
            )
        metadata.append(_pack_metadata(device))
    if expected not in metadata:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "pack index does not contain the selected pack identity",
        )
    current_index = _read_json_object(paths.index_file) if paths.index_file.exists() else {}
    current_aliases = (
        _read_json_object(paths.aliases_file) if paths.aliases_file.exists() else {}
    )
    current_index.update(staged_index)
    current_aliases.update(staged_aliases)
    return [
        (paths.index_file, current_index),
        (paths.aliases_file, current_aliases),
    ]


def _prepare_pack_transaction(
    staged_pack: Path,
    stage_data_dir: Path,
    destination: Path,
    stage_index_dir: Path,
    paths: PackPaths,
    expected: Tuple[str, str, str],
) -> List[Tuple[Path, Path]]:
    staged_pack = _resolved_child(staged_pack, stage_data_dir, "staged pack")
    destination = _resolved_child(destination, paths.data_dir, "installed pack")
    if not staged_pack.is_file() or staged_pack.suffix.casefold() != ".pack":
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "download did not produce the exact .pack artifact",
        )
    metadata = _merged_metadata(stage_index_dir, paths, expected)
    replacements = []  # type: List[Tuple[Path, Path]]
    try:
        transaction_dir = stage_data_dir.parent / "transaction"
        transaction_dir.mkdir()
        pack_temp = transaction_dir / "pack.prepared"
        shutil.copyfile(str(staged_pack), str(pack_temp))
        replacements.append((pack_temp, destination))
        for index, (target, value) in enumerate(metadata):
            temporary = transaction_dir / "metadata-{}.prepared".format(index)
            temporary.write_text(
                json.dumps(value, indent=2, sort_keys=True), encoding="utf-8"
            )
            replacements.append((temporary, target))
        return replacements
    except BaseException:
        for temporary, _ in replacements:
            if temporary.is_file():
                temporary.unlink()
        raise


def _download_pack_over_https(
    pdsc_path: Path,
    staged_pack: Path,
    expected: Tuple[str, str, str],
    emit: EventEmitter,
) -> None:
    try:
        root = ElementTree.parse(str(pdsc_path)).getroot()
    except (OSError, ElementTree.ParseError):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "selected pack descriptor is invalid",
        )
    urls = [
        str(element.text).strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "url"
        and element.text
        and str(element.text).strip()
    ]
    if len(urls) != 1:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "selected pack descriptor must contain one download URL",
        )
    _download_pack_from_https_base(urls[0], staged_pack, expected, emit)


def _download_pack_from_https_base(
    download_base: str,
    staged_pack: Path,
    expected: Tuple[str, str, str],
    emit: EventEmitter,
) -> None:
    _, _, normalized_version, pack_id = _normalize_pack_identity(*expected)
    remote_name = "{}.{}.pack".format(pack_id, normalized_version)
    try:
        download_url = urljoin(
            download_base.rstrip("/") + "/", quote(remote_name)
        )
    except (UnicodeError, ValueError):
        raise WorkerFailure(
            FlashErrorCode.PACK_DOWNLOAD_FAIL,
            "pack download fallback must use HTTPS",
        )
    if not _is_secure_https_url(download_url):
        raise WorkerFailure(
            FlashErrorCode.PACK_DOWNLOAD_FAIL,
            "pack download fallback must use HTTPS",
        )
    if staged_pack.exists():
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "pack download destination already exists",
        )

    staged_pack.parent.mkdir(parents=True, exist_ok=True)
    temporary = staged_pack.with_name(staged_pack.name + ".download")
    request = Request(
        download_url,
        headers={"User-Agent": "Mklink-AI-Probe/0.1 PackWorker"},
    )
    emit({"type": "event", "event": "pack-download-fallback"})
    try:
        with _open_https_request(request, timeout=30) as response:
            if not _is_secure_https_url(response.geturl()):
                raise WorkerFailure(
                    FlashErrorCode.PACK_DOWNLOAD_FAIL,
                    "pack download redirected outside HTTPS",
                )
            if getattr(response, "status", 200) != 200:
                raise WorkerFailure(
                    FlashErrorCode.PACK_DOWNLOAD_FAIL,
                    "pack download returned an unexpected HTTP status",
                )
            length_value = response.headers.get("Content-Length")
            try:
                declared_length = (
                    int(length_value) if length_value is not None else None
                )
            except (TypeError, ValueError):
                raise WorkerFailure(
                    FlashErrorCode.PACK_DOWNLOAD_FAIL,
                    "pack download returned an invalid content length",
                )
            if declared_length is not None and (
                declared_length < 0
                or declared_length > _MAX_PACK_DOWNLOAD_BYTES
            ):
                raise WorkerFailure(
                    FlashErrorCode.PACK_DOWNLOAD_FAIL,
                    "pack download exceeds the size limit",
                )

            downloaded = 0
            with temporary.open("xb") as stream:
                while True:
                    chunk = response.read(_PACK_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > _MAX_PACK_DOWNLOAD_BYTES:
                        raise WorkerFailure(
                            FlashErrorCode.PACK_DOWNLOAD_FAIL,
                            "pack download exceeds the size limit",
                        )
                    stream.write(chunk)
                    if declared_length is not None:
                        emit(
                            {
                                "type": "progress",
                                "current": downloaded,
                                "total": declared_length,
                            }
                        )
            if declared_length is not None and downloaded != declared_length:
                raise WorkerFailure(
                    FlashErrorCode.PACK_DOWNLOAD_FAIL,
                    "pack download length does not match the response",
                )
        os.replace(str(temporary), str(staged_pack))
    except WorkerFailure:
        raise
    except Exception:
        raise WorkerFailure(
            FlashErrorCode.PACK_DOWNLOAD_FAIL,
            "HTTPS pack download failed",
        )
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_pack_archive_identity(
    staged_pack: Path,
    expected: Tuple[str, str, str],
) -> None:
    pdsc_data = _read_pack_descriptor(staged_pack, "downloaded pack")
    try:
        root = ElementTree.fromstring(pdsc_data)
    except ElementTree.ParseError:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "downloaded pack descriptor is invalid",
        )

    direct_values = {}
    for child in root:
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name in ("vendor", "name") and child.text:
            direct_values[local_name] = str(child.text).strip()
    release_versions = [
        str(element.attrib.get("version")).strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "release"
        and element.attrib.get("version")
    ]
    expected_vendor, expected_pack, expected_version, _ = (
        _normalize_pack_identity(*expected)
    )
    actual_vendor = direct_values.get("vendor", "").split(":", 1)[0].strip()
    actual_pack = direct_values.get("name", "")
    if (
        actual_vendor != expected_vendor
        or actual_pack != expected_pack
    ):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "downloaded pack identity does not match the selected device",
        )
    if not release_versions or release_versions[0] != expected_version:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "downloaded pack current version does not match the selected device",
        )


def _read_pack_descriptor(pack_path: Path, subject: str) -> bytes:
    try:
        with ZipFile(str(pack_path)) as archive:
            descriptors = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.casefold().endswith(".pdsc")
            ]
            if len(descriptors) != 1:
                raise WorkerFailure(
                    FlashErrorCode.PACK_INTEGRITY_ERROR,
                    "{} must contain one descriptor".format(subject),
                )
            descriptor = descriptors[0]
            if descriptor.file_size > _MAX_PACK_PDSC_BYTES:
                raise WorkerFailure(
                    FlashErrorCode.PACK_INTEGRITY_ERROR,
                    "{} descriptor exceeds the size limit".format(subject),
                )
            with archive.open(descriptor) as stream:
                pdsc_data = stream.read(_MAX_PACK_PDSC_BYTES + 1)
    except WorkerFailure:
        raise
    except (BadZipFile, OSError, RuntimeError):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "{} archive is invalid".format(subject),
        )
    if len(pdsc_data) > _MAX_PACK_PDSC_BYTES:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "{} descriptor exceeds the size limit".format(subject),
        )
    return pdsc_data


def _normalize_local_import_descriptor(pdsc_data: bytes) -> bytes:
    """Make locally imported descriptors consumable without changing the Pack.

    Some vendor Packs omit the top-level ``url`` element because they are only
    distributed as local files. cmsis-pack-manager silently skips every device
    in those descriptors, even though a download URL is irrelevant for a local
    import. Add an empty element only to the staged descriptor copy that is fed
    to the indexer; the original archive remains the installed artifact.
    """

    try:
        root = ElementTree.fromstring(pdsc_data)
    except ElementTree.ParseError:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "imported pack descriptor is invalid",
        )
    if root.tag.rsplit("}", 1)[-1] != "package":
        return pdsc_data
    namespace = ""
    if root.tag.startswith("{") and "}" in root.tag:
        namespace = root.tag.split("}", 1)[0] + "}"
    children = list(root)
    url_tag = namespace + "url"
    if any(child.tag == url_tag for child in children):
        return pdsc_data

    url = ElementTree.Element(url_tag)
    insert_at = len(children)
    for anchor in ("description", "name", "vendor"):
        anchor_tag = namespace + anchor
        for index, child in enumerate(children):
            if child.tag == anchor_tag:
                insert_at = index + 1
                break
        else:
            continue
        break
    root.insert(insert_at, url)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _install(
    payload: Mapping[str, object],
    paths: PackPaths,
    stage: Path,
    emit: EventEmitter,
    cache_factory: Type[Cache],
) -> Dict[str, object]:
    part_number = payload.get("part_number")
    if not isinstance(part_number, str) or not part_number:
        raise WorkerFailure(FlashErrorCode.PACK_NOT_FOUND, "part number is required")
    stage_index = stage / "index"
    stage_data = stage / "data"
    stage_index.mkdir()
    stage_data.mkdir()
    cache = cache_factory(
        False,
        False,
        json_path=str(stage_index),
        data_path=str(stage_data),
        emitter=emit,
    )
    cache.cache_descriptors()
    refreshed_device = True
    try:
        device = cache.index[part_number]
    except KeyError:
        refreshed_device = False
        current_index = (
            _read_json_object(paths.index_file)
            if paths.index_file.is_file()
            else {}
        )
        device = current_index.get(part_number)
        if not isinstance(device, Mapping):
            raise WorkerFailure(
                FlashErrorCode.PACK_NOT_FOUND,
                "no CMSIS-Pack provides {}".format(part_number),
            )
    expected = _pack_metadata(device)
    pack_refs = cache.packs_for_devices([device])
    if len(pack_refs) != 1 or _ref_metadata(pack_refs[0]) != expected:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "selected device resolved to unexpected pack references",
        )
    if refreshed_device:
        cache.download_pack_list(pack_refs)
    pack_ref = pack_refs[0]
    relative_name = Path(pack_ref.get_pack_name())
    staged_pack = stage_data / relative_name
    if not staged_pack.is_file():
        if refreshed_device:
            pdsc_path = stage_data / Path(pack_ref.get_pdsc_name())
            _download_pack_over_https(pdsc_path, staged_pack, expected, emit)
        else:
            from_pack = device.get("from_pack")
            download_base = (
                from_pack.get("url") if isinstance(from_pack, Mapping) else None
            )
            if not isinstance(download_base, str) or not download_base:
                raise WorkerFailure(
                    FlashErrorCode.PACK_DOWNLOAD_FAIL,
                    "last known pack metadata has no download URL",
                )
            _download_pack_from_https_base(
                download_base, staged_pack, expected, emit
            )
        _validate_pack_archive_identity(staged_pack, expected)
    if not refreshed_device:
        staged_index_file = stage_index / "index.json"
        staged_index_data = _read_json_object(staged_index_file)
        staged_index_data[part_number] = dict(device)
        staged_index_file.write_text(
            json.dumps(staged_index_data, indent=2, sort_keys=True), encoding="utf-8"
        )
    vendor, pack, version = expected
    destination = _canonical_pack_path(paths, vendor, pack, version)
    replacements = _prepare_pack_transaction(
        staged_pack,
        stage_data,
        destination,
        stage_index,
        paths,
        expected,
    )
    _, _, _, pack_id = _normalize_pack_identity(vendor, pack, version)
    result = {
        "status": "installed",
        "pack_id": pack_id,
        "version": version,
        "pack_path": str(destination.resolve()),
    }
    _commit_transaction(paths, replacements, stage, result)
    return result


def _update_index(
    paths: PackPaths,
    stage: Path,
    emit: EventEmitter,
    cache_factory: Type[Cache],
) -> Dict[str, object]:
    stage_index = stage / "index"
    stage_data = stage / "data"
    stage_index.mkdir()
    stage_data.mkdir()
    cache = cache_factory(
        False,
        False,
        json_path=str(stage_index),
        data_path=str(stage_data),
        emitter=emit,
    )
    cache.cache_descriptors()
    staged_index = _read_json_object(stage_index / "index.json")
    _read_json_object(stage_index / "aliases.json")
    transaction_dir = stage / "transaction"
    transaction_dir.mkdir()
    replacements = []
    for name, target in (
        ("index.json", paths.index_file),
        ("aliases.json", paths.aliases_file),
    ):
        prepared = transaction_dir / (name + ".prepared")
        shutil.copy2(str(stage_index / name), str(prepared))
        replacements.append((prepared, target))
    result = {"status": "updated", "target_count": len(staged_index)}
    _commit_transaction(paths, replacements, stage, result)
    return result


def _import_pack(
    payload: Mapping[str, object],
    paths: PackPaths,
    stage: Path,
    emit: EventEmitter,
    cache_factory: Type[Cache],
) -> Dict[str, object]:
    source_value = payload.get("path")
    if not isinstance(source_value, str):
        raise WorkerFailure(FlashErrorCode.PACK_INTEGRITY_ERROR, "pack path is required")
    source = Path(source_value).resolve()
    if not source.is_file() or source.suffix.casefold() != ".pack":
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "import path must name an existing .pack file",
        )
    stage_index = stage / "index"
    stage_data = stage / "data"
    stage_index.mkdir()
    stage_data.mkdir()
    staged_pack = stage_data / "import.pack"
    shutil.copyfile(str(source), str(staged_pack))
    cache = cache_factory(
        False,
        False,
        json_path=str(stage_index),
        data_path=str(stage_data),
        emitter=emit,
    )
    staged_descriptor = stage_index / "import.pdsc"
    descriptor = _read_pack_descriptor(staged_pack, "imported pack")
    staged_descriptor.write_bytes(_normalize_local_import_descriptor(descriptor))
    cache.add_pack_from_path(str(staged_descriptor))
    metadata = {_pack_metadata(device) for device in cache.index.values()}
    if len(metadata) != 1:
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "imported pack metadata is missing or ambiguous",
        )
    vendor, pack, version = metadata.pop()
    destination = _canonical_pack_path(paths, vendor, pack, version)
    replacements = _prepare_pack_transaction(
        staged_pack,
        stage_data,
        destination,
        stage_index,
        paths,
        (vendor, pack, version),
    )
    _, _, _, pack_id = _normalize_pack_identity(vendor, pack, version)
    result = {
        "status": "installed",
        "pack_id": pack_id,
        "version": version,
        "pack_path": str(destination.resolve()),
    }
    _commit_transaction(paths, replacements, stage, result)
    return result


def handle_request(
    request: Mapping[str, object],
    emit: EventEmitter,
    cache_factory: Type[Cache] = ReportingCache,
) -> Dict[str, object]:
    command = request.get("command")
    payload = request.get("payload")
    root = request.get("root")
    staging_value = request.get("staging_dir")
    if (
        not isinstance(payload, Mapping)
        or not isinstance(root, str)
        or not root
        or not isinstance(staging_value, str)
        or not staging_value
    ):
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "worker request is incomplete",
        )
    paths = PackPaths(Path(root))
    recover_pending_transaction(paths)
    stage = _resolved_child(
        Path(staging_value), paths.staging_dir, "worker staging"
    )
    if stage.parent != paths.staging_dir.resolve():
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "worker staging must be a direct child",
        )
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    stage.mkdir()
    try:
        emit({"type": "event", "event": "staging", "path": str(stage.resolve())})
        if command == "update-index":
            return _update_index(paths, stage, emit, cache_factory)
        if command == "install":
            return _install(payload, paths, stage, emit, cache_factory)
        if command == "import":
            return _import_pack(payload, paths, stage, emit, cache_factory)
        raise WorkerFailure(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "unknown worker command: {}".format(command),
        )
    finally:
        resolved_stage = _resolved_child(stage, paths.staging_dir, "worker staging")
        if resolved_stage.exists() and not _transaction_journal(paths).exists():
            shutil.rmtree(str(resolved_stage))
        try:
            paths.staging_dir.rmdir()
        except OSError:
            pass


def run_protocol(
    stdin: IO[str],
    stdout: IO[str],
    handler: Callable[
        [Mapping[str, object], EventEmitter], Dict[str, object]
    ] = handle_request,
) -> int:
    def emit(message: Dict[str, object]) -> None:
        stdout.write(json.dumps(message) + "\n")
        stdout.flush()

    try:
        line = stdin.readline()
        request = json.loads(line)
        if not isinstance(request, Mapping):
            raise WorkerFailure(
                FlashErrorCode.PACK_INTEGRITY_ERROR,
                "worker request must be an object",
            )
        result = handler(request, emit)
        emit({"type": "result", "result": result})
        return 0
    except WorkerFailure as error:
        emit({"type": "error", "code": error.code.value, "message": error.message})
        return 1
    except Exception as error:
        emit(
            {
                "type": "error",
                "code": FlashErrorCode.PACK_DOWNLOAD_FAIL.value,
                "message": str(error),
            }
        )
        return 1


def main() -> int:
    return run_protocol(sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
