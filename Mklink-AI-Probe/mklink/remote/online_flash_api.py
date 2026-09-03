"""Dependency-injected REST API for CMSIS-DAP online flashing."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from mklink.cmsis_dap.backend import _pack_flm_address_offset
from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
from mklink.cmsis_dap.models import JobRequest, JobState, MemoryRegion, TargetRecord
from mklink.cmsis_dap.probes import filter_mklink_probes
from mklink.cmsis_dap.pyocd_runtime import import_pyocd_attr
from mklink.remote.resource_manager import ResourceError


_DEFAULT_UPLOAD_LIMIT = 256 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024
_TERMINAL_STATES = {JobState.STOPPED, JobState.SUCCEEDED, JobState.FAILED}
_REDACTED_PATH = "[redacted-path]"
_PATH_TOKEN_END = r"\s\"'<>|,;)\]}"
_FILE_URI = re.compile(r"\bfile:[^" + _PATH_TOKEN_END + r"]+", re.IGNORECASE)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^" + _PATH_TOKEN_END + r"]+"
)
_POSIX_LOCAL_ROOT = re.compile(
    r"(?<![A-Za-z0-9/])/"
    r"(?:home|Users|root|tmp|var|etc|mnt|opt|usr|srv|dev|proc|sys|bin|sbin|"
    r"boot|data|workspace|run|lib64|lib|media|snap|nix)"
    r"(?=/|[" + _PATH_TOKEN_END + r"]|$)"
    r"(?:/[^" + _PATH_TOKEN_END + r"]+)*"
)
_POSIX_FILE_PATH = re.compile(
    r"(?<![A-Za-z0-9/])/(?!api(?:/|$)|ws(?:/|$))"
    r"(?:[^/" + _PATH_TOKEN_END + r"]+/)*"
    r"[^/" + _PATH_TOKEN_END + r"]+\.[A-Za-z0-9]{1,16}"
)
_LOG = logging.getLogger(__name__)


@dataclass
class OnlineFlashServices:
    catalog: object
    pack_manager: object
    image_inspector: object
    job_manager: object
    probe_provider: Callable[[], Sequence[object]]
    target_memory_provider: Callable[[str], Sequence[MemoryRegion]]
    paths: object
    custom_flms: object = None
    configuration_lock: object = field(default_factory=threading.RLock)
    image_targets: Dict[str, object] = field(default_factory=dict)
    image_flash_overrides: Dict[str, object] = field(default_factory=dict)
    upload_limit: int = _DEFAULT_UPLOAD_LIMIT
    pack_index_updater: Optional[Callable[[Callable[[Dict[str, object]], None]], object]] = None
    heartbeat_interval: float = 15.0
    shutdown_timeout: float = 2.0


def _production_probe_provider() -> Sequence[object]:
    DebugProbeAggregator = import_pyocd_attr(
        "pyocd.probe.aggregator", "DebugProbeAggregator"
    )

    return DebugProbeAggregator.get_all_connected_probes()


def create_default_online_flash_services(
    resource_manager: object,
    prepare_connect: Optional[Callable[[JobRequest], None]] = None,
) -> OnlineFlashServices:
    """Build lazy production services without enumerating USB or accessing the network."""
    from mklink.cmsis_dap.backend import RoutingFlashBackend
    from mklink.cmsis_dap.custom_flm import CustomFlmCatalog
    from mklink.cmsis_dap.images import ImageInspector
    from mklink.cmsis_dap.jobs import OnlineFlashJobManager
    from mklink.cmsis_dap.pack_catalog import PackCatalog
    from mklink.cmsis_dap.pack_manager import PackManager
    from mklink.cmsis_dap.paths import PackPaths

    paths = PackPaths()
    inspector = ImageInspector(snapshot_root=paths.root / "images")
    return OnlineFlashServices(
        catalog=PackCatalog(paths),
        pack_manager=PackManager(paths.root),
        image_inspector=inspector,
        job_manager=OnlineFlashJobManager(
            RoutingFlashBackend,
            resource_manager,
            inspector.validate_unchanged,
            prepare_connect=prepare_connect,
        ),
        probe_provider=_production_probe_provider,
        target_memory_provider=lambda part_number: default_target_memory_provider(
            part_number, paths
        ),
        paths=paths,
        custom_flms=CustomFlmCatalog(paths.root),
    )


def shutdown_online_flash_services(services: OnlineFlashServices) -> None:
    """Request active work to stop, then clean up without unbounded waiting.

    A backend blocked in native code may outlive this call. Its job remains in
    STOPPING and is allowed to fail or finish cleanup when the backend returns.
    """
    errors = []
    shutdown = getattr(services.job_manager, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(wait=True, timeout=services.shutdown_timeout)
        except BaseException as error:
            errors.append(error)
    for component in (services.pack_manager, services.image_inspector):
        shutdown = getattr(component, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except BaseException as error:
                errors.append(error)
    if errors:
        raise errors[0]


class PackInstallBody(BaseModel):
    part_number: str


class LocalImageBody(BaseModel):
    path: str
    part_number: str
    base_address: Optional[Union[str, int]] = None


class JobBody(BaseModel):
    actions: List[str]
    image_id: Optional[str] = None
    preempt_ai: bool = True
    probe_id: Optional[str] = None
    target_part: Optional[str] = None
    frequency: int = Field(default=1_000_000, ge=1, le=10_000_000)
    connect_mode: str = "halt"
    reset_mode: str = "default"
    base_address: Optional[int] = None
    sector_addresses: List[int] = Field(default_factory=list)
    board: Optional[str] = None
    hpm_flash_cfg: Optional[Tuple[str, str, str, str]] = None


class ReadMemoryBody(BaseModel):
    address: Union[str, int]
    size: int = Field(..., ge=1, le=64 * 1024 * 1024)
    probe_id: Optional[str] = None
    target_part: str
    preempt_ai: bool = True
    frequency: int = Field(default=1_000_000, ge=1, le=10_000_000)
    connect_mode: str = "halt"
    reset_mode: str = "default"
    chunk_sizes: List[int] = Field(default_factory=list, max_length=65536)
    board: Optional[str] = None
    hpm_flash_cfg: Optional[Tuple[str, str, str, str]] = None


def _redact_paths(value: str) -> str:
    result = _FILE_URI.sub(_REDACTED_PATH, value)
    result = _WINDOWS_ABSOLUTE_PATH.sub(_REDACTED_PATH, result)
    result = _POSIX_LOCAL_ROOT.sub(_REDACTED_PATH, result)
    return _POSIX_FILE_PATH.sub(_REDACTED_PATH, result)


def _json_mapping(value: Mapping, *, hide_paths: bool) -> Dict[str, object]:
    result = {}
    redacted_index = 0
    for key, item in value.items():
        raw_key = str(key)
        redacted_key = _redact_paths(raw_key)
        if isinstance(key, Path) or redacted_key != raw_key:
            redacted_index += 1
            safe_key = "[redacted-key-{}]".format(redacted_index)
        else:
            safe_key = raw_key
        if hide_paths and raw_key in ("file_path", "pack_path", "custom_flm_paths", "flm_path"):
            continue
        base_key = safe_key
        collision_index = 2
        while safe_key in result:
            safe_key = "{}#{}".format(base_key, collision_index)
            collision_index += 1
        result[safe_key] = _json_primitive(item, hide_paths=hide_paths)
    return result


def _json_primitive(value: object, *, hide_paths: bool = False) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return _REDACTED_PATH
    if isinstance(value, str):
        return _redact_paths(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if is_dataclass(value) and not isinstance(value, type):
        result = {}
        for field in fields(value):
            if hide_paths and field.name in ("file_path", "pack_path", "custom_flm_paths", "flm_path"):
                continue
            result[field.name] = _json_primitive(getattr(value, field.name), hide_paths=hide_paths)
        return result
    if isinstance(value, Mapping):
        return _json_mapping(value, hide_paths=hide_paths)
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_primitive(item, hide_paths=hide_paths) for item in value]
    return value


def _safe_job_snapshot(snapshot: object) -> object:
    value = _json_primitive(snapshot)
    if isinstance(value, dict) and "file_path" in value:
        value["file_path"] = None
    return value


def _flash_status(code: FlashErrorCode) -> int:
    if code is FlashErrorCode.PROBE_BUSY:
        return 409
    if code in {
        FlashErrorCode.FILE_NOT_FOUND,
        FlashErrorCode.PACK_NOT_FOUND,
        FlashErrorCode.MKLINK_DAP_NOT_FOUND,
    }:
        return 404
    if code in {
        FlashErrorCode.TARGET_NOT_SUPPORTED,
        FlashErrorCode.FILE_FORMAT_ERROR,
        FlashErrorCode.BIN_ADDRESS_MISSING,
        FlashErrorCode.IMAGE_OUT_OF_RANGE,
        FlashErrorCode.TARGET_LOCKED,
    }:
        return 422
    if code is FlashErrorCode.PACK_INDEX_UNAVAILABLE:
        return 503
    if code in {
        FlashErrorCode.PACK_DOWNLOAD_FAIL,
        FlashErrorCode.PACK_INTEGRITY_ERROR,
        FlashErrorCode.CONNECT_FAIL,
        FlashErrorCode.ERASE_FAIL,
        FlashErrorCode.PROGRAM_FAIL,
        FlashErrorCode.VERIFY_FAIL,
        FlashErrorCode.RESET_FAIL,
    }:
        return 502
    if code is FlashErrorCode.USER_ABORT:
        return 409
    return 500


def _raise_http(error: Exception) -> None:
    if isinstance(error, HTTPException):
        raise HTTPException(
            status_code=error.status_code,
            detail=_json_primitive(error.detail),
            headers=error.headers,
        )
    if isinstance(error, FlashError):
        raise HTTPException(
            status_code=_flash_status(error.code),
            detail=_json_primitive(error.to_dict()),
        )
    if isinstance(error, ResourceError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": FlashErrorCode.PROBE_BUSY.value,
                "owner": error.conflict_owner,
                "resource": error.resource.value,
            },
        )
    if isinstance(error, KeyError):
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": _redact_paths(str(error))},
        )
    if isinstance(error, (ValueError, TypeError)):
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": _redact_paths(str(error))},
        )
    raise HTTPException(
        status_code=500,
        detail={"code": FlashErrorCode.UNKNOWN_ERROR.value, "message": "online flash operation failed"},
    )


async def _blocking(function: Callable[..., Any], *args: object, **kwargs: object) -> Any:
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except Exception as error:
        _raise_http(error)


def _pack_stream_requested(request: Request) -> bool:
    return "application/x-ndjson" in request.headers.get("accept", "").casefold()


def _pack_stream_error(error: BaseException) -> Dict[str, object]:
    if isinstance(error, HTTPException):
        return {
            "type": "error",
            "status": error.status_code,
            "detail": _json_primitive(error.detail, hide_paths=True),
        }
    if isinstance(error, FlashError):
        return {
            "type": "error",
            "status": _flash_status(error.code),
            "detail": _json_primitive(error.to_dict(), hide_paths=True),
        }
    if isinstance(error, (ValueError, TypeError)):
        return {
            "type": "error",
            "status": 422,
            "detail": {
                "code": "VALIDATION_ERROR",
                "message": _redact_paths(str(error)),
            },
        }
    return {
        "type": "error",
        "status": 500,
        "detail": {
            "code": FlashErrorCode.UNKNOWN_ERROR.value,
            "message": "online flash operation failed",
        },
    }


def _put_latest_pack_event(queue: asyncio.Queue, message: Dict[str, object]) -> None:
    if queue.full():
        if any(
            isinstance(item, Mapping) and item.get("type") in ("result", "error")
            for item in queue._queue
        ):
            return
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(message)


def _pack_stream_response(
    services: OnlineFlashServices,
    operation: Callable[[Callable[[Dict[str, object]], None]], object],
    *,
    refresh_catalog: bool = False,
    cleanup: Optional[Callable[[], None]] = None,
) -> StreamingResponse:
    async def stream():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=32)  # type: asyncio.Queue
        progress_high_water = 0.01

        def emit(raw_event: Dict[str, object]) -> None:
            nonlocal progress_high_water
            event = dict(raw_event)
            if event.get("type") == "progress":
                raw_progress = event.get("progress")
                if isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool):
                    fraction = float(raw_progress)
                else:
                    current = event.get("current")
                    total = event.get("total")
                    fraction = (
                        float(current) / float(total)
                        if isinstance(current, (int, float))
                        and not isinstance(current, bool)
                        and isinstance(total, (int, float))
                        and not isinstance(total, bool)
                        and float(total) > 0
                        else 0.0
                    )
                progress_high_water = max(
                    progress_high_water,
                    min(0.90, 0.05 + max(0.0, min(1.0, fraction)) * 0.85),
                )
                event["phase"] = "downloading"
                event["progress"] = progress_high_water
            message = {"type": "event", "event": _json_primitive(event, hide_paths=True)}

            loop.call_soon_threadsafe(_put_latest_pack_event, queue, message)

        async def run_operation() -> None:
            try:
                result = await run_in_threadpool(operation, emit)
                if refresh_catalog:
                    await queue.put({
                        "type": "event",
                        "event": {
                            "type": "progress",
                            "phase": "refreshing",
                            "progress": 0.95,
                        },
                    })
                    refresh = getattr(services.catalog, "refresh", None)
                    if callable(refresh):
                        await run_in_threadpool(refresh)
                await queue.put({
                    "type": "result",
                    "result": _json_primitive(result, hide_paths=True),
                })
            except BaseException as error:
                await queue.put(_pack_stream_error(error))

        task = asyncio.create_task(run_operation())
        try:
            initial = {
                "type": "event",
                "event": {"type": "progress", "phase": "preparing", "progress": 0.01},
            }
            yield json.dumps(initial, separators=(",", ":")) + "\n"
            while True:
                message = await queue.get()
                yield json.dumps(message, separators=(",", ":")) + "\n"
                if message.get("type") in ("result", "error"):
                    break
            await task
        finally:
            if not task.done():
                try:
                    await run_in_threadpool(services.pack_manager.cancel)
                except Exception:
                    pass
                task.cancel()
            if cleanup is not None:
                try:
                    await run_in_threadpool(cleanup)
                except Exception:
                    pass

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def update_pack_index(
    manager: object,
    on_event: Callable[[Dict[str, object]], None],
    updater: Optional[Callable[[Callable[[Dict[str, object]], None]], object]] = None,
) -> object:
    """Use a public manager capability or an explicitly supplied production adapter."""
    public_update = getattr(manager, "update_index", None)
    if callable(public_update):
        return public_update(on_event)
    if updater is not None:
        return updater(on_event)
    raise FlashError(
        FlashErrorCode.PACK_INDEX_UNAVAILABLE,
        "pack index update is unavailable",
    )


def _refresh_pack_index(
    services: OnlineFlashServices,
    on_event: Callable[[Dict[str, object]], None],
) -> object:
    try:
        result = update_pack_index(
            services.pack_manager,
            on_event,
            services.pack_index_updater,
        )
        refresh = getattr(services.catalog, "refresh", None)
        if callable(refresh):
            refresh()
        return result
    except Exception as error:
        note_failure = getattr(services.catalog, "note_refresh_failure", None)
        if callable(note_failure):
            note_failure(error)
        search = getattr(services.catalog, "search", None)
        if callable(search):
            try:
                search("", limit=1)
            except Exception:
                pass
        status = services.catalog.status()
        if isinstance(status, Mapping):
            available = bool(status.get("index_available"))
        else:
            available = bool(getattr(status, "index_available", False))
        if not available:
            raise FlashError(
                FlashErrorCode.PACK_INDEX_UNAVAILABLE,
                "pack index is unavailable: {}".format(error),
            ) from error
        raise


def _upload_path(paths: object, file_name: str, allowed_suffixes: Sequence[str]) -> Path:
    suffix = Path(file_name or "").suffix.casefold()
    if suffix not in set(allowed_suffixes):
        raise ValueError("upload must use one of: {}".format(", ".join(allowed_suffixes)))
    root = Path(getattr(paths, "root"))
    uploads = (root / "uploads").resolve()
    uploads.mkdir(parents=True, exist_ok=True)
    candidate = (uploads / (secrets.token_hex(24) + suffix)).resolve()
    if candidate.parent != uploads:
        raise ValueError("invalid upload path")
    return candidate


def _local_firmware_path(raw_path: str, limit: int) -> Path:
    source = Path(str(raw_path or "")).expanduser().resolve()
    if source.suffix.casefold() not in (".bin", ".hex"):
        raise ValueError("firmware path must use .bin or .hex")
    if not source.is_file():
        raise ValueError("firmware path is not a readable file")
    size = source.stat().st_size
    if size <= 0:
        raise ValueError("firmware file is empty")
    if size > limit:
        raise ValueError("firmware file exceeds {} bytes".format(limit))
    return source


def _stream_upload(
    upload: UploadFile,
    paths: object,
    allowed_suffixes: Sequence[str],
    limit: int,
) -> Tuple[Path, str, int]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("upload limit must be a positive integer")
    destination = _upload_path(paths, upload.filename or "", allowed_suffixes)
    digest = hashlib.sha256()
    total = 0
    descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = upload.file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError("upload exceeds {} bytes".format(limit))
                digest.update(chunk)
                output.write(chunk)
        if total == 0:
            raise ValueError("upload is empty")
        return destination, digest.hexdigest(), total
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _unlink(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _parse_base_address(value: Optional[Union[str, int]]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        result = int(text, 0)
    except ValueError as error:
        raise ValueError("base_address must be a decimal or 0x-prefixed integer") from error
    if result < 0:
        raise ValueError("base_address must be nonnegative")
    return result


def _exact_installed_target(catalog: object, part_number: str) -> TargetRecord:
    records = catalog.search(part_number, installed=True, limit=100)
    exact = [
        record for record in records
        if record.part_number.casefold() == part_number.casefold() and record.installed
    ]
    if len(exact) != 1:
        raise FlashError(
            FlashErrorCode.TARGET_NOT_SUPPORTED,
            "target must resolve to one installed target",
        )
    return exact[0]


def _resolved_target(catalog: object, part_number: str) -> TargetRecord:
    from mklink.hpm_config import is_hpm_target

    if is_hpm_target(part_number):
        return TargetRecord(
            part_number=part_number.strip(),
            vendor="HPMicro",
            installed=True,
            source="hpm-rom-api",
        )
    return _exact_installed_target(catalog, part_number)


def _custom_flm_payload(record: object) -> Dict[str, object]:
    return {
        "algorithm_id": str(getattr(record, "algorithm_id")),
        "target_part": str(getattr(record, "target_part")),
        "file_name": str(getattr(record, "file_name")),
        "flash_start": int(getattr(record, "flash_start")),
        "flash_size": int(getattr(record, "flash_size")),
        "page_size": int(getattr(record, "page_size")),
        "sector_sizes": [list(pair) for pair in getattr(record, "sector_sizes")],
    }


def _flash_algorithm_payload(record: object) -> Dict[str, object]:
    """Return public metadata for an algorithm that can serve a target."""
    return {
        "algorithm_id": str(getattr(record, "algorithm_id")),
        "target_part": str(getattr(record, "target_part")),
        "file_name": str(getattr(record, "file_name")),
        "flash_start": int(getattr(record, "flash_start")),
        "flash_size": int(getattr(record, "flash_size")),
        "default": bool(getattr(record, "default", False)),
        "source_kind": str(getattr(record, "source_kind")),
        "source_name": str(getattr(record, "source_name")),
    }


def _target_flash_configuration(
    services: OnlineFlashServices,
    part_number: str,
) -> tuple[tuple[MemoryRegion, ...], tuple[str, ...], tuple[str, ...]]:
    from mklink.hpm_config import is_hpm_target

    if is_hpm_target(part_number):
        return (
            (MemoryRegion("hpm-xpi", 0x80000000, 0x10000000, True, True, None),),
            (),
            (),
        )
    target = _exact_installed_target(services.catalog, part_number)
    base_regions = tuple(services.target_memory_provider(part_number))
    if services.custom_flms is None:
        return base_regions, (), ()
    custom_regions = tuple(services.custom_flms.regions(part_number))
    retained_base = list(base_regions)
    for index, region in enumerate(custom_regions):
        for other in tuple(retained_base) + custom_regions[:index]:
            if region.start < other.end and other.start < region.end:
                if other in retained_base:
                    retained_base.remove(other)
                    continue
                raise FlashError(
                    FlashErrorCode.TARGET_NOT_SUPPORTED,
                    "custom FLM range overlaps an existing flash algorithm",
                )
    builtin_algorithms = []
    if target.source == "daplink-builtin":
        from mklink.cmsis_dap.builtin_flm_bundle import discover_builtin_flm_algorithms

        builtin_algorithms = discover_builtin_flm_algorithms(part_number)
    retained_algorithms = []
    for algorithm in builtin_algorithms:
        algorithm_end = algorithm.flash_start + algorithm.flash_size
        if any(
            algorithm.flash_start < region.end and region.start < algorithm_end
            for region in custom_regions
        ):
            continue
        retained_algorithms.append(algorithm)
    builtin_sources = {}
    for algorithm in retained_algorithms:
        if algorithm.builtin_blob_path and algorithm.builtin_blob_sha256:
            builtin_sources.setdefault(
                algorithm.builtin_blob_sha256,
                algorithm.builtin_blob_path,
            )
    user_digests = tuple(services.custom_flms.fingerprint(part_number))
    user_paths = tuple(services.custom_flms.paths(part_number))
    return (
        tuple(retained_base) + custom_regions,
        tuple(builtin_sources) + user_digests,
        tuple(builtin_sources.values()) + user_paths,
    )


def _captured_image_flash_regions(
    services: OnlineFlashServices,
    target: TargetRecord,
    base_regions: Sequence[MemoryRegion],
) -> tuple[tuple[MemoryRegion, ...], tuple[tuple[int, int], ...]]:
    """Extend a captured image only to a same-Pack FLM's explicit range."""
    from mklink.cmsis_dap.algorithm_catalog import discover_flash_algorithms

    if not target.pack_path:
        return tuple(base_regions), ()
    pack_path = os.path.normcase(os.path.abspath(target.pack_path))
    algorithms = [
        algorithm
        for algorithm in discover_flash_algorithms(target.part_number, paths=services.paths)
        if bool(getattr(algorithm, "default", False))
        and getattr(algorithm, "source_kind", "") in {"installed-pack", "builtin-pack"}
        and getattr(algorithm, "pack_path", None)
        and os.path.normcase(os.path.abspath(str(algorithm.pack_path))) == pack_path
    ]
    expanded = []
    overrides = []
    for region in base_regions:
        if (
            not region.is_flash
            or not region.writable
            or not isinstance(region.sector_size, int)
            or isinstance(region.sector_size, bool)
            or region.sector_size <= 0
        ):
            expanded.append(region)
            continue
        candidates = [
            algorithm
            for algorithm in algorithms
            if int(getattr(algorithm, "flash_start", -1)) == region.start
            and int(getattr(algorithm, "flash_size", 0)) > region.length
        ]
        if not candidates:
            expanded.append(region)
            continue
        algorithm = min(candidates, key=lambda item: int(item.flash_size))
        size = int(algorithm.flash_size)
        expanded.append(MemoryRegion(
            region.name,
            region.start,
            size,
            True,
            True,
            region.sector_size,
        ))
        overrides.append((region.start, size))
    return tuple(expanded), tuple(overrides)


def _selected_probe(provider: Callable[[], Sequence[object]], probe_id: str) -> object:
    records = filter_mklink_probes(provider())
    for record in records:
        if record.unique_id == probe_id:
            return record
    raise FlashError(FlashErrorCode.MKLINK_DAP_NOT_FOUND, "MKLink DAP probe was not found")


def _enumerate_probes(provider: Callable[[], Sequence[object]]) -> list[object]:
    try:
        return filter_mklink_probes(provider())
    except Exception as error:
        _LOG.exception("CMSIS-DAP probe enumeration failed")
        raise FlashError(
            FlashErrorCode.CONNECT_FAIL,
            "CMSIS-DAP 枚举失败，请检查 MicroKeen 设备的 WinUSB 驱动后重试",
        ) from error


def _active_snapshot(job_manager: object) -> Optional[object]:
    snapshots = job_manager.list()
    for snapshot in reversed(snapshots):
        if snapshot.state not in _TERMINAL_STATES:
            return snapshot
    return None


def _pack_in_use(job_manager: object) -> bool:
    return _active_snapshot(job_manager) is not None


def _add_custom_flm_configuration(
    services: OnlineFlashServices,
    temporary: Path,
    file_name: str,
    part_number: str,
) -> object:
    with services.configuration_lock:
        if _pack_in_use(services.job_manager):
            raise FlashError(
                FlashErrorCode.PROBE_BUSY,
                "custom FLM configuration is in use by an online flash job",
            )
        target = _exact_installed_target(services.catalog, part_number)
        return services.custom_flms.add(
            temporary,
            file_name,
            target.part_number,
            (),
        )


def _remove_custom_flm_configuration(
    services: OnlineFlashServices,
    part_number: str,
    algorithm_id: str,
) -> None:
    with services.configuration_lock:
        if _pack_in_use(services.job_manager):
            raise FlashError(
                FlashErrorCode.PROBE_BUSY,
                "custom FLM configuration is in use by an online flash job",
            )
        services.custom_flms.remove(part_number, algorithm_id)


def _job_flash_algorithms(
    target: TargetRecord,
    body: JobBody,
    inspection: object,
    algorithms: Sequence[object],
    preferred_algorithm_ids: Sequence[str] = (),
) -> list[object]:
    from mklink.cmsis_dap.algorithm_catalog import resolve_firmware_algorithms

    if not algorithms:
        if target.source == "daplink-builtin":
            raise FlashError(
                FlashErrorCode.TARGET_NOT_SUPPORTED,
                "DAPLink builtin target has no usable Flash algorithm",
            )
        return []

    if inspection is not None:
        ranges = tuple(
            (int(segment.start), int(segment.end))
            for segment in inspection.segments
        ) or ((int(inspection.start), int(inspection.end)),)
        return [
            selection.algorithm
            for selection in resolve_firmware_algorithms(
                algorithms,
                ranges,
                allow_uncovered=target.source == "builtin",
                preferred_algorithm_ids=preferred_algorithm_ids,
            )
        ]

    if "erase" in body.actions and body.sector_addresses:
        return [
            selection.algorithm
            for selection in resolve_firmware_algorithms(
                algorithms,
                tuple((int(address), int(address) + 1) for address in body.sector_addresses),
                allow_uncovered=target.source == "builtin",
                preferred_algorithm_ids=preferred_algorithm_ids,
            )
        ]

    if "erase" in body.actions:
        has_custom = any(
            getattr(algorithm, "source_kind", "") == "custom-flm"
            for algorithm in algorithms
        )
        if target.source == "builtin" and has_custom:
            raise FlashError(
                FlashErrorCode.TARGET_NOT_SUPPORTED,
                "chip erase is ambiguous when a pyOCD target has custom Flash algorithms",
            )
        if target.source == "daplink-builtin" or has_custom:
            ranges = tuple(
                (
                    int(algorithm.flash_start),
                    int(algorithm.flash_start) + int(algorithm.flash_size),
                )
                for algorithm in algorithms
                if int(getattr(algorithm, "flash_size", 0)) > 0
            )
            selections = resolve_firmware_algorithms(
                algorithms,
                ranges,
                preferred_algorithm_ids=preferred_algorithm_ids,
            ) if ranges else []
            if len(selections) != 1:
                raise FlashError(
                    FlashErrorCode.TARGET_NOT_SUPPORTED,
                    "chip erase requires exactly one unambiguous Flash algorithm",
                )
            return [selections[0].algorithm]
        return []

    if target.source == "daplink-builtin":
        candidates = [
            algorithm for algorithm in algorithms
            if int(getattr(algorithm, "flash_size", 0)) > 0
        ]
        if not candidates:
            raise FlashError(
                FlashErrorCode.TARGET_NOT_SUPPORTED,
                "DAPLink builtin target has no usable Flash algorithm",
            )
        return [min(candidates, key=lambda algorithm: (
            0 if bool(getattr(algorithm, "default", False)) else 1,
            0 if getattr(algorithm, "source_kind", "") == "daplink-builtin" else 1,
            int(algorithm.flash_start),
            int(algorithm.flash_size),
            str(algorithm.algorithm_id),
        ))]
    return []


def _start_job_with_configuration(
    services: OnlineFlashServices,
    body: JobBody,
    target: TargetRecord,
) -> tuple[str, object]:
    with services.configuration_lock:
        from mklink.hpm_config import is_hpm_target, normalize_hpm_configuration

        hpm_target = is_hpm_target(target.part_number)
        board = body.board
        hpm_flash_cfg = body.hpm_flash_cfg
        if hpm_target:
            board, hpm_flash_cfg = normalize_hpm_configuration(
                target.part_number, board=board, flash_cfg=hpm_flash_cfg
            )
        regions, fingerprint, configured_flm_paths = _target_flash_configuration(
            services, target.part_number
        )
        custom_flm_paths = ()
        custom_flm_digests = ()
        custom_flm_regions = ()
        pack_flm_regions = ()
        custom_flm_ram_start = None
        custom_flm_ram_size = None
        inspection = None
        if any(action in body.actions for action in ("program", "verify")):
            if not body.image_id:
                raise HTTPException(
                    status_code=422,
                    detail="program and verify require image_id",
                )
            inspection = services.image_inspector.validate_unchanged(body.image_id)
            if services.image_targets.get(body.image_id) != (
                target.part_number.casefold(),
                fingerprint,
            ):
                raise FlashError(
                    FlashErrorCode.TARGET_NOT_SUPPORTED,
                    "image inspection does not match the selected target",
                )
            flash_override = services.image_flash_overrides.get(body.image_id)
            if flash_override is not None:
                regions, pack_flm_regions = flash_override
        if not hpm_target:
            from mklink.cmsis_dap.algorithm_catalog import (
                FlashAlgorithmError,
                discover_flash_algorithms,
            )

            try:
                needs_catalog = (
                    inspection is not None
                    or bool(body.sector_addresses)
                    or target.source == "daplink-builtin"
                    or ("erase" in body.actions and bool(configured_flm_paths))
                )
                catalog = (
                    discover_flash_algorithms(target.part_number, paths=services.paths)
                    if needs_catalog else []
                )
                configured_paths = {str(path) for path in configured_flm_paths}
                preferred_algorithm_ids = tuple(
                    algorithm.algorithm_id
                    for algorithm in catalog
                    if algorithm.source_kind == "custom-flm"
                    and str(algorithm.custom_path) in configured_paths
                )
                selected = _job_flash_algorithms(
                    target,
                    body,
                    inspection,
                    catalog,
                    preferred_algorithm_ids,
                )
            except FlashAlgorithmError as error:
                raise FlashError(
                    FlashErrorCode.TARGET_NOT_SUPPORTED,
                    str(error),
                ) from error
            source_records = []
            for algorithm in selected:
                path = algorithm.custom_path or algorithm.builtin_blob_path
                digest = algorithm.custom_sha256 or algorithm.builtin_blob_sha256
                if path and digest:
                    source_records.append((
                        str(path),
                        str(digest),
                        (int(algorithm.flash_start), int(algorithm.flash_size)),
                    ))
                if (
                    target.source == "daplink-builtin"
                    and custom_flm_ram_start is None
                    and algorithm.ram_size > 0
                ):
                    custom_flm_ram_start = int(algorithm.ram_start)
                    custom_flm_ram_size = int(algorithm.ram_size)
            if source_records:
                custom_flm_paths = tuple(record[0] for record in source_records)
                custom_flm_digests = tuple(record[1] for record in source_records)
                custom_flm_regions = tuple(record[2] for record in source_records)
            elif inspection is not None and not catalog and configured_flm_paths:
                custom_flm_paths = tuple(configured_flm_paths)
                custom_flm_digests = tuple(fingerprint)
            if custom_flm_ram_start is None and target.source == "daplink-builtin":
                from mklink.cmsis_dap.builtin_flm_bundle import discover_builtin_flm_algorithms

                builtin_algorithms = discover_builtin_flm_algorithms(target.part_number)
                if builtin_algorithms:
                    custom_flm_ram_start = builtin_algorithms[0].ram_start
                    custom_flm_ram_size = builtin_algorithms[0].ram_size
        if "program" in body.actions and not hpm_target:
            if "erase" not in body.actions:
                raise FlashError(
                    FlashErrorCode.IMAGE_OUT_OF_RANGE,
                    "program requires image-covered sector erase",
                )
            coverage = services.image_inspector.covered_sectors(
                body.image_id,
                regions,
            )
            expected_sectors = tuple(sector.address for sector in coverage.sectors)
            if not coverage.sector_operations_available or not expected_sectors:
                raise FlashError(
                    FlashErrorCode.IMAGE_OUT_OF_RANGE,
                    "reliable sector geometry is required for programming",
                )
            if tuple(body.sector_addresses) != expected_sectors:
                raise FlashError(
                    FlashErrorCode.IMAGE_OUT_OF_RANGE,
                    "erase sectors must exactly match the image-covered sectors",
                )
        job_request = JobRequest(
            actions=tuple(body.actions),
            image_id=body.image_id,
            preempt_ai=body.preempt_ai,
            probe_id=body.probe_id,
            target_part=target.part_number,
            pack_path=target.pack_path,
            custom_flm_paths=custom_flm_paths,
            custom_flm_digests=custom_flm_digests,
            custom_flm_regions=custom_flm_regions,
            pack_flm_regions=pack_flm_regions,
            custom_flm_ram_start=custom_flm_ram_start,
            custom_flm_ram_size=custom_flm_ram_size,
            frequency=body.frequency,
            connect_mode=body.connect_mode,
            reset_mode=body.reset_mode,
            base_address=body.base_address,
            sector_addresses=tuple(body.sector_addresses),
            board=board,
            hpm_flash_cfg=hpm_flash_cfg,
        )
        job_id = services.job_manager.start(job_request)
        return job_id, services.job_manager.get(job_id)


def create_online_flash_router(services: OnlineFlashServices) -> APIRouter:
    router = APIRouter(prefix="/api/online-flash", tags=["online-flash"])

    async def inspect_source(
        source: Path,
        part_number: str,
        base_address: Optional[Union[str, int]],
        captured_from_target: bool = False,
    ) -> object:
        from mklink.hpm_config import is_hpm_target

        target = await _blocking(_resolved_target, services.catalog, part_number)
        if is_hpm_target(target.part_number) and source.suffix.casefold() != ".bin":
            _raise_http(FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "HPM ROM API only supports BIN firmware",
            ))
        regions, fingerprint, _paths = await _blocking(
            _target_flash_configuration, services, target.part_number
        )
        pack_flm_regions = ()
        if captured_from_target and not is_hpm_target(target.part_number):
            regions, pack_flm_regions = await _blocking(
                _captured_image_flash_regions,
                services,
                target,
                regions,
            )
        parsed_base = await _blocking(_parse_base_address, base_address)
        inspection = await _blocking(
            services.image_inspector.inspect,
            source,
            regions,
            base_address=parsed_base,
        )
        if is_hpm_target(target.part_number):
            from mklink.cmsis_dap.images import SectorCoverage

            coverage = SectorCoverage((), False)
        else:
            coverage = await _blocking(
                services.image_inspector.covered_sectors,
                inspection.image_id,
                regions,
            )
        services.image_targets[inspection.image_id] = (
            target.part_number.casefold(), fingerprint
        )
        if pack_flm_regions:
            services.image_flash_overrides[inspection.image_id] = (
                tuple(regions), pack_flm_regions
            )
        payload = _json_primitive(inspection, hide_paths=True)
        payload["sector_operations_available"] = coverage.sector_operations_available
        payload["sectors"] = _json_primitive(coverage.sectors)
        return payload

    @router.get("/probes")
    async def probes() -> object:
        return _json_primitive(await _blocking(_enumerate_probes, services.probe_provider))

    @router.get("/targets")
    async def targets(
        q: str = "",
        vendor: Optional[str] = None,
        installed: Optional[bool] = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> object:
        result = await _blocking(services.catalog.search, q, vendor=vendor, installed=installed, limit=limit)
        return _json_primitive(result, hide_paths=True)

    @router.get("/targets/{part_number}/memory-map")
    async def target_memory_map(part_number: str) -> object:
        target = await _blocking(_resolved_target, services.catalog, part_number)
        regions, _fingerprint, _paths = await _blocking(
            _target_flash_configuration, services, target.part_number
        )
        return [
            {
                "name": region.name,
                "start": region.start,
                "length": region.length,
                "sector_size": region.sector_size,
            }
            for region in regions
            if region.is_flash
            and region.length > 0
            and region.sector_size is not None
            and region.sector_size > 0
        ]

    @router.get("/targets/{part_number}/algorithms")
    async def target_flash_algorithms(part_number: str) -> object:
        target = await _blocking(_resolved_target, services.catalog, part_number)
        from mklink.hpm_config import is_hpm_target

        if is_hpm_target(target.part_number):
            return [{
                "algorithm_id": "hpm-rom-api",
                "target_part": target.part_number,
                "file_name": "HPM ROM API",
                "flash_start": 0x80000000,
                "flash_size": 0x10000000,
                "default": True,
                "source_kind": "hpm-rom-api",
                "source_name": "HPM ROM API",
            }]

        from mklink.cmsis_dap.algorithm_catalog import (
            FlashAlgorithmError,
            discover_flash_algorithms,
        )

        try:
            records = await _blocking(
                discover_flash_algorithms,
                target.part_number,
                paths=services.paths,
            )
        except FlashAlgorithmError as error:
            _raise_http(FlashError(
                FlashErrorCode.TARGET_NOT_SUPPORTED,
                str(error),
            ))
        payload = [_flash_algorithm_payload(record) for record in records]
        if target.source == "builtin":
            regions = await _blocking(
                services.target_memory_provider,
                target.part_number,
            )
            builtin = [
                {
                    "algorithm_id": "pyocd-builtin:{}:{:08x}".format(
                        target.part_number.casefold(), region.start,
                    ),
                    "target_part": target.part_number,
                    "file_name": "{} · {}".format(target.part_number, region.name),
                    "flash_start": region.start,
                    "flash_size": region.length,
                    "default": index == 0,
                    "source_kind": "pyocd-builtin",
                    "source_name": "pyOCD",
                }
                for index, region in enumerate(regions)
                if region.is_flash and region.length > 0
            ]
            payload = builtin + payload
        return payload

    @router.post("/memory/read")
    async def read_memory(body: ReadMemoryBody) -> Response:
        """Read a target range and return it as a downloadable BIN file.
        """
        target = await _blocking(_resolved_target, services.catalog, body.target_part)
        if not body.probe_id:
            raise HTTPException(status_code=422, detail="probe_id is required")
        address = await _blocking(_parse_base_address, body.address)
        if address is None:
            raise HTTPException(status_code=422, detail="address is required")
        request = JobRequest(
            actions=("connect", "disconnect"),
            preempt_ai=body.preempt_ai,
            probe_id=body.probe_id,
            target_part=target.part_number,
            pack_path=target.pack_path,
            frequency=body.frequency,
            connect_mode=body.connect_mode,
            reset_mode=body.reset_mode,
            board=body.board,
            hpm_flash_cfg=body.hpm_flash_cfg,
        )
        data = await _blocking(
            services.job_manager.read_memory,
            request,
            address,
            body.size,
        )
        filename = "read-0x{:08X}-{}.bin".format(address, body.size)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
            },
        )

    @router.post("/memory/read-stream")
    async def read_memory_stream(body: ReadMemoryBody) -> StreamingResponse:
        """Stream sector chunks while keeping a single target connection open."""
        target = await _blocking(_resolved_target, services.catalog, body.target_part)
        if not body.probe_id:
            raise HTTPException(status_code=422, detail="probe_id is required")
        address = await _blocking(_parse_base_address, body.address)
        if address is None:
            raise HTTPException(status_code=422, detail="address is required")
        chunk_sizes = tuple(body.chunk_sizes)
        if (
            not chunk_sizes
            or any(type(value) is not int or value <= 0 for value in chunk_sizes)
            or sum(chunk_sizes) != body.size
        ):
            raise HTTPException(
                status_code=422,
                detail="chunk_sizes must contain positive sizes that add up to size",
            )
        request = JobRequest(
            actions=("connect", "disconnect"),
            preempt_ai=body.preempt_ai,
            probe_id=body.probe_id,
            target_part=target.part_number,
            pack_path=target.pack_path,
            frequency=body.frequency,
            connect_mode=body.connect_mode,
            reset_mode=body.reset_mode,
            board=body.board,
            hpm_flash_cfg=body.hpm_flash_cfg,
        )
        filename = "read-0x{:08X}-{}.bin".format(address, body.size)
        return StreamingResponse(
            services.job_manager.iter_memory(request, address, chunk_sizes),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(body.size),
            },
        )

    @router.get("/packs/status")
    async def pack_status() -> object:
        refresh = getattr(services.catalog, "refresh", None)
        status = await _blocking(refresh if callable(refresh) else services.catalog.status)
        return _json_primitive(status)

    @router.post("/packs/index/update")
    async def pack_index_update(request: Request) -> object:
        if _pack_stream_requested(request):
            return _pack_stream_response(
                services,
                lambda on_event: _refresh_pack_index(services, on_event),
            )
        events: List[Dict[str, object]] = []
        result = await _blocking(
            _refresh_pack_index,
            services,
            lambda event: events.append(dict(event)),
        )
        return {
            "result": _json_primitive(result, hide_paths=True),
            "events": _json_primitive(events, hide_paths=True),
        }

    @router.post("/packs/install")
    async def pack_install(request: Request, body: PackInstallBody) -> object:
        from mklink.hpm_config import is_hpm_target

        if is_hpm_target(body.part_number):
            return {
                "result": {"status": "installed", "part_number": body.part_number},
                "events": [{"type": "log", "message": "HPM 使用内置 ROM API，无需 Pack"}],
            }
        if _pack_stream_requested(request):
            return _pack_stream_response(
                services,
                lambda on_event: services.pack_manager.install(
                    body.part_number,
                    on_event,
                ),
                refresh_catalog=True,
            )
        events: List[Dict[str, object]] = []
        result = await _blocking(
            services.pack_manager.install,
            body.part_number,
            lambda event: events.append(dict(event)),
        )
        refresh = getattr(services.catalog, "refresh", None)
        if callable(refresh):
            await _blocking(refresh)
        return {
            "result": _json_primitive(result, hide_paths=True),
            "events": _json_primitive(events, hide_paths=True),
        }

    @router.post("/packs/import")
    async def pack_import(request: Request, file: UploadFile = File(...)) -> object:
        temporary = None  # type: Optional[Path]
        stream_handoff = False
        try:
            temporary, _digest, _size = await _blocking(
                _stream_upload, file, services.paths, (".pack",), services.upload_limit
            )
            if _pack_stream_requested(request):
                await file.close()
                stream_handoff = True
                source = temporary
                return _pack_stream_response(
                    services,
                    lambda on_event: services.pack_manager.import_pack(
                        source,
                        on_event,
                    ),
                    refresh_catalog=True,
                    cleanup=lambda: _unlink(source),
                )
            events: List[Dict[str, object]] = []
            result = await _blocking(
                services.pack_manager.import_pack,
                temporary,
                lambda event: events.append(dict(event)),
            )
            refresh = getattr(services.catalog, "refresh", None)
            if callable(refresh):
                await _blocking(refresh)
            return {
                "result": _json_primitive(result, hide_paths=True),
                "events": _json_primitive(events, hide_paths=True),
            }
        finally:
            if not stream_handoff:
                await run_in_threadpool(_unlink, temporary)
                await file.close()

    @router.post("/packs/cancel")
    async def pack_cancel() -> object:
        await _blocking(services.pack_manager.cancel)
        return {"status": "cancelled"}

    @router.delete("/packs/{pack_id}/{version}")
    async def pack_remove(pack_id: str, version: str) -> object:
        if "." not in pack_id:
            raise HTTPException(status_code=422, detail="pack_id must contain vendor and pack name")
        vendor, pack = pack_id.split(".", 1)
        await _blocking(
            services.pack_manager.remove,
            vendor,
            pack,
            version,
            in_use=lambda _pack_id, _version: _pack_in_use(services.job_manager),
        )
        return {"status": "removed", "pack_id": pack_id, "version": version}

    @router.get("/algorithms")
    async def custom_flm_list(part_number: str) -> object:
        from mklink.hpm_config import is_hpm_target

        if is_hpm_target(part_number):
            return []
        if services.custom_flms is None:
            return []
        records = await _blocking(services.custom_flms.list, part_number)
        return [_custom_flm_payload(record) for record in records]

    @router.post("/algorithms")
    async def custom_flm_add(
        file: UploadFile = File(...),
        part_number: str = Form(...),
    ) -> object:
        from mklink.hpm_config import is_hpm_target

        if is_hpm_target(part_number):
            _raise_http(FlashError(
                FlashErrorCode.TARGET_NOT_SUPPORTED,
                "HPM targets use the ROM API and cannot load FLM algorithms",
            ))
        if services.custom_flms is None:
            raise HTTPException(status_code=503, detail="custom FLM storage is unavailable")
        if await _blocking(_pack_in_use, services.job_manager):
            _raise_http(FlashError(
                FlashErrorCode.PROBE_BUSY,
                "custom FLM configuration is in use by an online flash job",
            ))
        temporary = None  # type: Optional[Path]
        try:
            temporary, _digest, _size = await _blocking(
                _stream_upload,
                file,
                services.paths,
                (".flm",),
                min(services.upload_limit, 8 * 1024 * 1024),
            )
            record = await _blocking(
                _add_custom_flm_configuration,
                services,
                temporary,
                Path(file.filename or "algorithm.flm").name,
                part_number,
            )
            return _custom_flm_payload(record)
        finally:
            await run_in_threadpool(_unlink, temporary)
            await file.close()

    @router.delete("/algorithms/{algorithm_id}")
    async def custom_flm_remove(algorithm_id: str, part_number: str) -> object:
        from mklink.hpm_config import is_hpm_target

        if is_hpm_target(part_number):
            _raise_http(FlashError(
                FlashErrorCode.TARGET_NOT_SUPPORTED,
                "HPM targets use the ROM API and cannot load FLM algorithms",
            ))
        if services.custom_flms is None:
            raise HTTPException(status_code=503, detail="custom FLM storage is unavailable")
        if await _blocking(_pack_in_use, services.job_manager):
            _raise_http(FlashError(
                FlashErrorCode.PROBE_BUSY,
                "custom FLM configuration is in use by an online flash job",
            ))
        await _blocking(
            _remove_custom_flm_configuration,
            services,
            part_number,
            algorithm_id,
        )
        return {"status": "removed"}

    @router.post("/images/inspect")
    async def image_inspect(
        file: UploadFile = File(...),
        part_number: str = Form(...),
        base_address: Optional[str] = Form(None),
        captured_from_target: bool = Form(False),
    ) -> object:
        temporary = None  # type: Optional[Path]
        try:
            temporary, _digest, _size = await _blocking(
                _stream_upload, file, services.paths, (".hex", ".bin"), services.upload_limit
            )
            return await inspect_source(
                temporary,
                part_number,
                base_address,
                captured_from_target,
            )
        finally:
            await run_in_threadpool(_unlink, temporary)
            await file.close()

    @router.post("/images/inspect-path")
    async def image_inspect_path(body: LocalImageBody) -> object:
        try:
            source = await _blocking(
                _local_firmware_path, body.path, services.upload_limit
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error))
        return await inspect_source(source, body.part_number, body.base_address)

    @router.get("/images/source-status")
    async def image_source_status(path: str = Query(...)) -> object:
        try:
            source = await _blocking(
                _local_firmware_path, path, services.upload_limit
            )
            stat = await _blocking(source.stat)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error))
        return {
            "available": True,
            "file_name": source.name,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    @router.get("/images/{image_id}/preview")
    async def image_preview(
        image_id: str,
        offset: int = Query(0, ge=0),
        length: int = Query(4096, ge=0, le=4096),
    ) -> object:
        inspection = await _blocking(services.image_inspector.validate_unchanged, image_id)
        address = inspection.start + offset
        preview = await _blocking(services.image_inspector.preview, image_id, address, length)
        return {
            "address": preview.address,
            "length": len(preview.data),
            "data_base64": base64.b64encode(preview.data).decode("ascii"),
            "present": list(preview.present),
        }

    @router.post("/jobs")
    async def job_start(body: JobBody) -> object:
        if not body.probe_id or not body.target_part:
            raise HTTPException(status_code=422, detail="probe_id and target_part are required")
        await _blocking(_selected_probe, services.probe_provider, body.probe_id)
        target = await _blocking(_resolved_target, services.catalog, body.target_part)
        job_id, snapshot = await _blocking(
            _start_job_with_configuration,
            services,
            body,
            target,
        )
        return {"job_id": job_id, "job": _safe_job_snapshot(snapshot)}

    @router.get("/jobs/active")
    async def job_active() -> object:
        return _safe_job_snapshot(await _blocking(_active_snapshot, services.job_manager))

    @router.get("/jobs/{job_id}")
    async def job_get(job_id: str) -> object:
        return _safe_job_snapshot(await _blocking(services.job_manager.get, job_id))

    @router.post("/jobs/{job_id}/stop")
    async def job_stop(job_id: str) -> object:
        return _safe_job_snapshot(await _blocking(services.job_manager.stop, job_id))

    @router.get("/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        request: Request,
        after: int = Query(0, ge=0),
    ) -> StreamingResponse:
        await _blocking(services.job_manager.get, job_id)

        async def stream():
            cursor = after
            while True:
                if await request.is_disconnected():
                    return
                try:
                    events = await run_in_threadpool(
                        services.job_manager.wait_for_events,
                        job_id,
                        cursor,
                        services.heartbeat_interval,
                    )
                except Exception as error:
                    if isinstance(error, KeyError):
                        return
                    payload = {"code": FlashErrorCode.UNKNOWN_ERROR.value, "message": "event stream failed"}
                    yield "event: error\ndata: {}\n\n".format(json.dumps(payload, separators=(",", ":")))
                    return
                for event in events:
                    if event.sequence <= cursor:
                        continue
                    cursor = event.sequence
                    payload = json.dumps(_json_primitive(event), separators=(",", ":"))
                    yield "id: {}\nevent: {}\ndata: {}\n\n".format(event.sequence, event.event, payload)
                snapshot = await run_in_threadpool(services.job_manager.get, job_id)
                if snapshot.state in _TERMINAL_STATES:
                    return
                if not events:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router


def default_target_memory_provider(
    part_number: str,
    paths: Optional[object] = None,
) -> Sequence[MemoryRegion]:
    """Resolve exact builtin or cached-pack flash regions without opening USB."""
    needle = part_number.casefold()
    if paths is not None:
        try:
            from mklink.cmsis_dap.pack_catalog import PackCatalog

            installed = [
                record
                for record in PackCatalog(paths).search(part_number, installed=True)
                if record.part_number.casefold() == needle and record.pack_path
            ]
            if len(installed) == 1:
                regions = _pack_memory_regions(part_number, Path(installed[0].pack_path))
                if regions:
                    return regions
        except (ImportError, OSError, TypeError, ValueError):
            pass
    try:
        from mklink.cmsis_dap.builtin_flm_bundle import discover_builtin_flm_algorithms

        algorithms = discover_builtin_flm_algorithms(part_number)
        regions = []
        for algorithm_index, algorithm in enumerate(algorithms):
            sector_sizes = algorithm.sector_sizes
            if not sector_sizes:
                regions.append(MemoryRegion(
                    "daplink-flm-{}".format(algorithm_index),
                    algorithm.flash_start,
                    algorithm.flash_size,
                    True,
                    True,
                    None,
                ))
                continue
            for sector_index, (offset, sector_size) in enumerate(sector_sizes):
                next_offset = (
                    sector_sizes[sector_index + 1][0]
                    if sector_index + 1 < len(sector_sizes)
                    else algorithm.flash_size
                )
                if 0 <= offset < next_offset <= algorithm.flash_size and sector_size > 0:
                    regions.append(MemoryRegion(
                        "daplink-flm-{}-{}".format(algorithm_index, sector_index),
                        algorithm.flash_start + offset,
                        next_offset - offset,
                        True,
                        True,
                        sector_size,
                    ))
        if regions:
            return regions
    except (ImportError, OSError, TypeError, ValueError):
        pass
    try:
        TARGET = import_pyocd_attr("pyocd.target", "TARGET")

        entries = TARGET.items() if hasattr(TARGET, "items") else ((name, TARGET[name]) for name in TARGET.get_all_target_names())
        matches = []
        for name, target_type in entries:
            candidates = {
                str(name).casefold(),
                str(getattr(target_type, "PART_NUMBER", "")).casefold(),
            }
            if needle in candidates:
                matches.append(target_type)
        if len(matches) == 1:
            memory_map = getattr(matches[0], "MEMORY_MAP", None)
            regions = _memory_map_regions(memory_map)
            if regions:
                return regions
    except ImportError:
        pass

    if paths is not None:
        regions = _cached_index_regions(part_number, Path(getattr(paths, "index_file")))
        if regions:
            return regions
    raise FlashError(FlashErrorCode.TARGET_NOT_SUPPORTED, "target memory map is unavailable or ambiguous")


def _memory_map_regions(memory_map: object) -> List[MemoryRegion]:
    if memory_map is None:
        return []
    result = []
    for region in memory_map:
        is_flash = bool(getattr(region, "is_flash", False))
        start = getattr(region, "start", None)
        length = getattr(region, "length", None)
        if is_flash and isinstance(start, int) and isinstance(length, int) and length > 0:
            sector_size = getattr(region, "sector_size", None)
            if not isinstance(sector_size, int) or isinstance(sector_size, bool) or sector_size <= 0:
                sector_size = getattr(region, "blocksize", None)
            if not isinstance(sector_size, int) or isinstance(sector_size, bool) or sector_size <= 0:
                flm = getattr(region, "flm", None)
                ranges = getattr(flm, "iter_sector_size_ranges", None)
                if callable(ranges):
                    flm_offset = _pack_flm_address_offset(
                        start,
                        length,
                        getattr(flm, "flash_start", None),
                        getattr(flm, "flash_size", None),
                    )
                    flm_regions = []
                    for index, (sector_range, range_sector_size) in enumerate(ranges()):
                        range_start = max(start, int(sector_range.start) + flm_offset)
                        range_end = min(start + length, int(sector_range.end) + 1 + flm_offset)
                        if range_start < range_end and isinstance(range_sector_size, int) and range_sector_size > 0:
                            name = str(getattr(region, "name", "flash"))
                            flm_regions.append(MemoryRegion(
                                name if index == 0 else "{}-{}".format(name, index),
                                range_start,
                                range_end - range_start,
                                True,
                                True,
                                range_sector_size,
                            ))
                    if flm_regions:
                        result.extend(flm_regions)
                        continue
            result.append(MemoryRegion(str(getattr(region, "name", "flash")), start, length, True, True, sector_size))
    return result


def _pack_memory_regions(part_number: str, pack_path: Path) -> List[MemoryRegion]:
    """Load a Pack target memory map; pyOCD derives sector geometry from its FLM."""
    CmsisPack = import_pyocd_attr(
        "pyocd.target.pack.cmsis_pack", "CmsisPack"
    )

    pack = CmsisPack(str(pack_path))
    matches = [
        device for device in pack.devices
        if str(device.part_number).casefold() == part_number.casefold()
    ]
    if len(matches) != 1:
        return []
    return _memory_map_regions(matches[0].memory_map)


def _cached_index_regions(part_number: str, index_file: Path) -> List[MemoryRegion]:
    try:
        payload = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    candidates = payload.get("targets", payload.get("devices", payload)) if isinstance(payload, dict) else {}
    if not isinstance(candidates, dict):
        return []
    exact = [value for key, value in candidates.items() if str(key).casefold() == part_number.casefold()]
    if len(exact) != 1 or not isinstance(exact[0], dict):
        return []
    algorithms = exact[0].get("algorithms", exact[0].get("flash_algorithms", ()))
    if isinstance(algorithms, dict):
        algorithms = list(algorithms.values())
    if not isinstance(algorithms, list):
        return []
    result = []
    for index, algorithm in enumerate(algorithms):
        if not isinstance(algorithm, dict):
            continue
        start = algorithm.get("start", algorithm.get("flash_start"))
        size = algorithm.get("size", algorithm.get("flash_size"))
        try:
            parsed_start = int(start, 0) if isinstance(start, str) else int(start)
            parsed_size = int(size, 0) if isinstance(size, str) else int(size)
        except (TypeError, ValueError):
            continue
        if parsed_start >= 0 and parsed_size > 0:
            sector = algorithm.get("sector_size")
            try:
                parsed_sector = int(sector, 0) if isinstance(sector, str) else int(sector) if sector is not None else None
            except (TypeError, ValueError):
                parsed_sector = None
            result.append(MemoryRegion("flash-{}".format(index), parsed_start, parsed_size, True, True, parsed_sector))
    return result
