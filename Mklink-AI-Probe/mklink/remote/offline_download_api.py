"""FastAPI routes for structured MKLink offline-download deployment."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Dict, List, Mapping, Optional
import uuid

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from mklink.cmsis_dap.pyocd_runtime import import_pyocd_attr
from mklink.offline_download import (
    OfflineAlgorithm,
    OfflineDownloadConfig,
    OfflineDownloadError,
    deploy_offline_bundle,
    generate_offline_script,
    offline_trigger_command,
    parse_offline_config,
)


_UPLOAD_CHUNK = 1024 * 1024
_MAX_UPLOAD_SIZE = 256 * 1024 * 1024
_MAX_TOTAL_UPLOAD_SIZE = 512 * 1024 * 1024
_ADDRESS_SPACE_SIZE = 1 << 32
_TOKEN_SAFE = re.compile(r"^[A-Za-z0-9._:-]+$")


def detect_probe_model(port: Optional[str] = None, bridge: Optional[object] = None) -> dict:
    from mklink.discovery import find_mklink_cdc_port
    from mklink.firmware_check import read_bridge_version, read_device_version

    last_error: Optional[BaseException] = None
    for attempt in range(2):
        resolved_port = port or (None if bridge is not None else find_mklink_cdc_port())
        if bridge is not None or resolved_port:
            try:
                version = (
                    read_bridge_version(bridge)
                    if bridge is not None
                    else read_device_version(resolved_port)
                )
            except (ConnectionError, TimeoutError, OSError) as error:
                last_error = error
            else:
                if version is not None:
                    if version.major not in (2, 3, 4):
                        raise OfflineDownloadError(
                            "cmd.get_version() returned an unsupported version"
                        )
                    return {"model": f"V{version.major}", "version": str(version)}
        if attempt == 0:
            time.sleep(0.5)
    if last_error is not None:
        raise OfflineDownloadError(f"cmd.get_version() failed: {last_error}")
    raise OfflineDownloadError("cmd.get_version() did not return a version")


def _hex(value: int) -> str:
    return f"0x{value:08X}"


def _profile_candidates(part_number: str, disk_root: Optional[Path]) -> list[dict]:
    from mklink.discovery import check_flm_on_microkeen, resolve_keil_flm_path
    from mklink.profiles import load_mcu_profiles, match_mcu_by_device

    profiles = load_mcu_profiles()
    key = match_mcu_by_device(part_number, profiles)
    if not key:
        return []
    profile = profiles.get(key) or {}
    flm_path = str(profile.get("flm_path") or "")
    file_name = Path(flm_path).name
    if not file_name:
        return []
    local_path = resolve_keil_flm_path(file_name)
    on_probe, _probe_path = check_flm_on_microkeen(file_name)
    if disk_root is not None:
        on_probe = (disk_root / "FLM" / file_name).is_file()
    return [{
        "id": f"profile-{key}",
        "file_name": file_name,
        "flash_base": _hex(int(str(profile.get("flash_base") or "0"), 0)),
        "ram_base": _hex(int(str(profile.get("ram_base") or "0"), 0)),
        "source_kind": "profile" if local_path else "existing",
        "source_token": f"profile:{key}" if local_path else None,
        "origin": "MCU profile",
        "available": bool(local_path or on_probe),
        "on_probe": on_probe,
    }]


def _installed_pack_paths(paths: object) -> list[tuple[str, str, Path]]:
    state_file = Path(getattr(paths, "state_file"))
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    installed = payload.get("installed") if isinstance(payload, Mapping) else None
    if not isinstance(installed, Mapping):
        return []
    result = []
    for pack_id, versions in installed.items():
        if not isinstance(versions, Mapping):
            continue
        for version, raw_path in versions.items():
            path = Path(str(raw_path))
            if path.is_file():
                result.append((str(pack_id), str(version), path))
    return result


def _default_ram_start(device: object) -> int:
    for region in device.memory_map:
        if bool(getattr(region, "is_ram", False)):
            return int(region.start)
    return 0


def _pack_device_algorithms(pack_path: Path, part_number: str) -> list[tuple[object, object, int]]:
    CmsisPack = import_pyocd_attr(
        "pyocd.target.pack.cmsis_pack", "CmsisPack"
    )

    pack = CmsisPack(str(pack_path))
    devices = [
        device for device in pack.devices
        if str(device.part_number).casefold() == part_number.casefold()
    ]
    if len(devices) != 1:
        return []
    device = devices[0]
    default_ram = _default_ram_start(device)
    result = []
    for index, element in enumerate(getattr(device, "_info").algos):
        name = element.attrib.get("name")
        start = element.attrib.get("start")
        if not name or start is None:
            continue
        ram_start = element.attrib.get("RAMstart")
        result.append((device, element, default_ram if ram_start is None else int(ram_start, 0)))
    return result


def _pack_candidates(paths: object, part_number: str) -> list[dict]:
    candidates = []
    for pack_id, version, pack_path in _installed_pack_paths(paths):
        for index, (_device, element, ram_start) in enumerate(
            _pack_device_algorithms(pack_path, part_number)
        ):
            name = str(element.attrib["name"])
            token = f"pack:{pack_id}:{version}:{part_number}:{index}"
            candidates.append({
                "id": "pack-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
                "file_name": Path(name).name,
                "flash_base": _hex(int(element.attrib["start"], 0)),
                "ram_base": _hex(ram_start),
                "source_kind": "pack",
                "source_token": token,
                "origin": f"{pack_id}@{version}",
                "available": True,
                "on_probe": False,
            })
    unique = {}
    for candidate in candidates:
        key = (
            candidate["file_name"].casefold(),
            candidate["flash_base"],
            candidate["ram_base"],
        )
        unique.setdefault(key, candidate)
    return list(unique.values())


def discover_algorithms(paths: object, part_number: str, disk_root: Optional[Path]) -> list[dict]:
    from mklink.cmsis_dap.algorithm_catalog import discover_flash_algorithms

    catalog = discover_flash_algorithms(part_number, paths=paths)
    candidates = [{
        "id": "catalog-" + algorithm.algorithm_id[:12],
        "file_name": algorithm.file_name,
        "flash_base": _hex(algorithm.flash_start),
        "ram_base": _hex(algorithm.ram_start),
        "source_kind": "pack",
        "source_token": algorithm.source_token,
        "origin": algorithm.source_name,
        "available": True,
        "on_probe": False,
    } for algorithm in catalog]
    combined = candidates + _profile_candidates(part_number, disk_root)
    unique = {}
    for candidate in combined:
        key = (
            candidate["file_name"].casefold(),
            candidate["flash_base"],
            candidate["ram_base"],
            candidate["source_token"],
        )
        unique.setdefault(key, candidate)
    return list(unique.values())


def _merged_flash_ranges(regions: object) -> tuple[tuple[int, int], ...]:
    ranges = []
    for region in regions:
        start = getattr(region, "start", None)
        length = getattr(region, "length", None)
        if (
            not bool(getattr(region, "is_flash", False))
            or not bool(getattr(region, "writable", False))
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(length, int)
            or isinstance(length, bool)
            or start < 0
            or length <= 0
            or start >= _ADDRESS_SPACE_SIZE
            or length > _ADDRESS_SPACE_SIZE - start
        ):
            continue
        ranges.append((start, start + length))
    ranges.sort()
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _range_is_covered(start: int, end: int, ranges: object) -> bool:
    return any(region_start <= start and end <= region_end for region_start, region_end in ranges)


def _selected_algorithm_range(
    algorithm: OfflineAlgorithm,
    catalog_algorithms: object,
    flash_ranges: tuple[tuple[int, int], ...],
) -> tuple[int, int]:
    records = list(catalog_algorithms)
    matches = []
    if algorithm.source_token and algorithm.source_kind != "upload":
        matches = [
            record for record in records
            if str(getattr(record, "source_token", "")) == algorithm.source_token
        ]
    if not matches and algorithm.source_kind != "upload":
        matches = [
            record for record in records
            if str(getattr(record, "file_name", "")).casefold()
            == algorithm.file_name.casefold()
            and int(getattr(record, "flash_start", -1)) == algorithm.flash_base
        ]
    valid_matches = []
    for record in matches:
        start = getattr(record, "flash_start", None)
        size = getattr(record, "flash_size", None)
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(size, int)
            and not isinstance(size, bool)
            and 0 <= start < _ADDRESS_SPACE_SIZE
            and 0 < size <= _ADDRESS_SPACE_SIZE - start
        ):
            valid_matches.append((start, start + size))
    if valid_matches:
        starts = {start for start, _end in valid_matches}
        if starts != {algorithm.flash_base}:
            raise OfflineDownloadError(
                f"selected FLM base does not match its catalog metadata: {algorithm.file_name}"
            )
        # Duplicate Pack sources can describe the same FLM. The shortest matching
        # range is the conservative common boundary.
        return algorithm.flash_base, min(end for _start, end in valid_matches)

    # Uploaded, profile, or already-present FLMs may not have catalog metadata.
    # The target memory map is still authoritative for the region beginning at
    # the configured FLM base, and keeps this fallback bounded.
    containing = [
        (max(start, algorithm.flash_base), end)
        for start, end in flash_ranges
        if start <= algorithm.flash_base < end
    ]
    if not containing:
        raise OfflineDownloadError(
            f"selected FLM base is outside writable target Flash: {algorithm.file_name}"
        )
    return min(containing, key=lambda item: item[1] - item[0])


def _validate_bin_firmware_ranges(
    config: OfflineDownloadConfig,
    online_services: object,
    firmware_sources: Optional[Mapping[str, Path]] = None,
) -> None:
    resolved_sources = firmware_sources or {}
    bin_sources = [
        (
            firmware,
            resolved_sources.get(firmware.id)
            or (Path(firmware.source_path) if firmware.source_path else None),
            firmware.id not in resolved_sources,
        )
        for firmware in config.firmwares
        if firmware.format == "bin"
    ]
    bin_sources = [
        (firmware, source, require_bin_suffix)
        for firmware, source, require_bin_suffix in bin_sources
        if source is not None
    ]
    if not bin_sources:
        return
    # HPM offline images use the ROM API and have no selected CMSIS FLM. Keep
    # that existing workflow outside this ARM/FLM-specific safety gate.
    if config.is_hpm:
        return
    if not config.target_part:
        raise OfflineDownloadError("BIN firmware validation requires target_part")

    from mklink.remote.online_flash_api import _resolved_target, _target_flash_configuration

    try:
        target = _resolved_target(online_services.catalog, config.target_part)
        regions, _fingerprint, _paths = _target_flash_configuration(
            online_services,
            target.part_number,
        )
    except Exception as error:
        raise OfflineDownloadError(
            f"target Flash metadata is unavailable for {config.target_part}: {error}"
        ) from error

    flash_ranges = _merged_flash_ranges(regions)
    if not flash_ranges:
        raise OfflineDownloadError(
            f"target has no writable Flash range: {target.part_number}"
        )
    algorithms = {algorithm.id.casefold(): algorithm for algorithm in config.algorithms}
    used_algorithm_ids = {
        firmware.algorithm_id.casefold() for firmware, _source, _suffix in bin_sources
    }
    catalog_algorithms = ()
    if any(
        algorithm.id.casefold() in used_algorithm_ids
        and algorithm.source_kind in ("pack", "existing")
        for algorithm in config.algorithms
    ):
        # Catalog metadata gives a tighter FLM size when available. A damaged
        # unrelated Pack must not block a configuration whose target memory map
        # and selected FLM base still provide a conservative safe boundary.
        try:
            from mklink.cmsis_dap.algorithm_catalog import discover_flash_algorithms

            catalog_algorithms = tuple(
                discover_flash_algorithms(target.part_number, paths=online_services.paths)
            )
        except Exception:
            catalog_algorithms = ()
    algorithm_ranges = {}
    for firmware, raw_source, require_bin_suffix in bin_sources:
        source = Path(raw_source).expanduser().resolve()
        if (
            require_bin_suffix and source.suffix.casefold() != ".bin"
        ) or not source.is_file():
            raise OfflineDownloadError(
                f"BIN firmware source is unavailable: {firmware.file_name}"
            )
        size = source.stat().st_size
        if size <= 0 or size > _MAX_UPLOAD_SIZE:
            raise OfflineDownloadError(
                f"BIN firmware source has an invalid size: {firmware.file_name}"
            )
        start = firmware.base_address
        if start is None:
            raise OfflineDownloadError("BIN firmware requires a base address")
        if start >= _ADDRESS_SPACE_SIZE or size > _ADDRESS_SPACE_SIZE - start:
            raise OfflineDownloadError(
                f"BIN address range overflows 32-bit address space: {firmware.file_name}"
            )
        end = start + size
        if not _range_is_covered(start, end, flash_ranges):
            raise OfflineDownloadError(
                "BIN range 0x{:08X}-0x{:08X} is outside writable Flash for {}: {}".format(
                    start,
                    end - 1,
                    target.part_number,
                    firmware.file_name,
                )
            )
        selected = algorithms[firmware.algorithm_id.casefold()]
        coverage = algorithm_ranges.get(selected.id.casefold())
        if coverage is None:
            coverage = _selected_algorithm_range(
                selected,
                catalog_algorithms,
                flash_ranges,
            )
            algorithm_ranges[selected.id.casefold()] = coverage
        if not _range_is_covered(start, end, (coverage,)):
            raise OfflineDownloadError(
                "BIN range 0x{:08X}-0x{:08X} exceeds selected FLM coverage "
                "0x{:08X}-0x{:08X}: {}".format(
                    start,
                    end - 1,
                    coverage[0],
                    coverage[1] - 1,
                    selected.file_name,
                )
            )


def _profile_source(token: str, disk_root: Path) -> Path:
    from mklink.discovery import resolve_keil_flm_path
    from mklink.profiles import load_mcu_profiles

    if not token.startswith("profile:"):
        raise OfflineDownloadError("invalid profile FLM token")
    key = token.split(":", 1)[1]
    profile = load_mcu_profiles().get(key) or {}
    file_name = Path(str(profile.get("flm_path") or "")).name
    if not file_name:
        raise OfflineDownloadError("profile does not define an FLM file")
    resolved = resolve_keil_flm_path(file_name)
    if resolved:
        return Path(resolved)
    existing = disk_root / "FLM" / file_name
    if existing.is_file():
        return existing
    raise OfflineDownloadError(f"FLM file is unavailable: {file_name}")


def _pack_source(paths: object, token: str, destination: Path) -> Path:
    if not _TOKEN_SAFE.fullmatch(token) or not token.startswith(("pack:", "catalog:", "custom:")):
        raise OfflineDownloadError("invalid Pack FLM token")
    if token.startswith(("catalog:", "custom:")):
        from mklink.cmsis_dap.algorithm_catalog import (
            discover_flash_algorithms,
            extract_algorithm,
            target_from_source_token,
        )

        try:
            part_number = target_from_source_token(token)
        except ValueError:
            raise OfflineDownloadError("invalid catalog FLM token")
        matches = [
            algorithm
            for algorithm in discover_flash_algorithms(part_number, paths=paths)
            if algorithm.source_token == token
        ]
        if len(matches) != 1:
            raise OfflineDownloadError("catalog FLM token is unavailable")
        payload = extract_algorithm(matches[0])
        if not isinstance(payload, bytes):
            raise OfflineDownloadError("catalog FLM extraction failed")
        destination.write_bytes(payload)
        return destination
    try:
        _prefix, pack_id, version, part_number, raw_index = token.rsplit(":", 4)
        index = int(raw_index)
    except (ValueError, TypeError):
        raise OfflineDownloadError("invalid Pack FLM token")
    matches = [
        path for candidate_id, candidate_version, path in _installed_pack_paths(paths)
        if candidate_id == pack_id and candidate_version == version
    ]
    if len(matches) != 1:
        raise OfflineDownloadError("installed Pack for FLM token is unavailable")
    algorithms = _pack_device_algorithms(matches[0], part_number)
    if index < 0 or index >= len(algorithms):
        raise OfflineDownloadError("Pack FLM token index is invalid")
    device, element, _ram_start = algorithms[index]
    with device.get_file(str(element.attrib["name"])) as source:
        destination.write_bytes(source.read())
    return destination


def _copy_upload(upload: UploadFile, destination: Path, total: list[int]) -> Path:
    size = 0
    with destination.open("wb") as stream:
        while True:
            chunk = upload.file.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            total[0] += len(chunk)
            if size > _MAX_UPLOAD_SIZE or total[0] > _MAX_TOTAL_UPLOAD_SIZE:
                raise OfflineDownloadError("offline upload size limit exceeded")
            stream.write(chunk)
    return destination


def _redact_trigger_line(raw: str) -> str:
    return re.sub(
        r"(?i)(IDCODE\s*:\s*)0x[0-9a-f]+",
        r"\1<masked>",
        str(raw).strip(),
    )[:500]


def _redact_trigger_output(response: str) -> list[str]:
    return [
        line
        for line in (_redact_trigger_line(raw) for raw in response.splitlines()[-100:])
        if line
    ]


def create_offline_download_router(
    online_services: object,
    resource_manager: object,
    device_provider: Optional[object] = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/offline-download", tags=["offline-download"])
    background_trigger_tasks: set[asyncio.Task] = set()

    def _connected_bridge(port: Optional[str]) -> Optional[object]:
        device = device_provider() if callable(device_provider) else None
        if device is None or not bool(getattr(device, "connected", False)):
            return None
        device_port = getattr(device, "port", None)
        if port and device_port and str(port) != str(device_port):
            return None
        return getattr(device, "_bridge", None)

    def _send_trigger(
        command: str,
        active_bridge: Optional[object],
        resolved_port: Optional[str],
        on_output: Optional[Callable[[str], None]] = None,
    ) -> str:
        from mklink.bridge import MKLinkSerialBridge

        if active_bridge is not None:
            if on_output is None:
                return active_bridge.send_command(command, timeout=600, echo=True)
            return active_bridge.send_command(
                command,
                timeout=600,
                echo=False,
                on_output=on_output,
            )
        bridge = MKLinkSerialBridge(resolved_port)
        try:
            if not bridge.connect():
                raise ConnectionError("Unable to connect to MKLink CDC port")
            if on_output is None:
                return bridge.send_command(command, timeout=600, echo=True)
            return bridge.send_command(
                command,
                timeout=600,
                echo=False,
                on_output=on_output,
            )
        finally:
            bridge.close()

    def _trigger_stream(
        command: str,
        active_bridge: Optional[object],
        resolved_port: Optional[str],
    ) -> StreamingResponse:
        from mklink.remote.resource_manager import ResourceGroup

        async def stream():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            owner = f"user:offline-download:trigger:{uuid.uuid4().hex}"
            accepting_output = True

            def emit(raw: str) -> None:
                if not accepting_output:
                    return
                line = _redact_trigger_line(raw)
                if line:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "line", "line": line},
                    )

            async def run() -> None:
                try:
                    resource_manager.acquire_many(
                        [ResourceGroup.MKLINK_BRIDGE, ResourceGroup.TARGET_DEBUG],
                        owner,
                        preempt=False,
                        preempt_user_dashboard=True,
                    )
                    response = await asyncio.to_thread(
                        _send_trigger,
                        command,
                        active_bridge,
                        resolved_port,
                        emit,
                    )
                    lines = _redact_trigger_output(response)
                    text = "\n".join(lines).casefold()
                    await queue.put({
                        "type": "result",
                        "result": {
                            "status": (
                                "completed"
                                if "finished" in text and "aborted" not in text
                                else "failed"
                            ),
                            "lines": lines,
                        },
                    })
                except Exception as error:
                    await queue.put({
                        "type": "error",
                        "status": 409,
                        "detail": str(error),
                    })
                finally:
                    resource_manager.release(owner)

            task = asyncio.create_task(run())
            background_trigger_tasks.add(task)
            task.add_done_callback(background_trigger_tasks.discard)
            try:
                while True:
                    message = await queue.get()
                    yield json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
                    if message.get("type") in ("result", "error"):
                        break
                await task
            finally:
                accepting_output = False
                if task.done():
                    await task

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @router.get("/status")
    async def status() -> object:
        from mklink.discovery import find_microkeen_disk

        disk = await asyncio.to_thread(find_microkeen_disk)
        root = Path(disk) if disk else None
        return {
            "available": root is not None,
            "disk_path": str(root) if root else None,
            "python_dir": str(root / "python") if root else None,
            "flm_dir": str(root / "FLM") if root else None,
        }

    @router.get("/algorithms")
    async def algorithms(part_number: str) -> object:
        from mklink.discovery import find_microkeen_disk

        disk = await asyncio.to_thread(find_microkeen_disk)
        root = Path(disk) if disk else None
        try:
            return await asyncio.to_thread(
                discover_algorithms,
                online_services.paths,
                part_number,
                root,
            )
        except (OfflineDownloadError, OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error))

    @router.post("/preview")
    async def preview(payload: dict = Body(...)) -> object:
        try:
            config = parse_offline_config(payload)
            await asyncio.to_thread(
                _validate_bin_firmware_ranges,
                config,
                online_services,
            )
            return {
                "model": config.model,
                "script_name": config.script_name,
                "script": generate_offline_script(config),
            }
        except OfflineDownloadError as error:
            raise HTTPException(status_code=422, detail=str(error))

    @router.post("/deploy")
    async def deploy(
        config_json: str = Form(...),
        firmware_files: List[UploadFile] = File(default=[]),
        flm_files: List[UploadFile] = File(default=[]),
    ) -> object:
        from mklink.discovery import find_microkeen_disk

        try:
            payload = json.loads(config_json)
            if not isinstance(payload, Mapping):
                raise OfflineDownloadError("offline config must be an object")
            config = parse_offline_config(payload)
            disk = await asyncio.to_thread(find_microkeen_disk)
            if not disk:
                raise OfflineDownloadError("MICROKEEN disk is unavailable")
            disk_root = Path(disk)
            total = [0]
            with tempfile.TemporaryDirectory(prefix="mklink-offline-") as raw_temp:
                temp = Path(raw_temp)
                uploaded_firmwares = []
                for index, upload in enumerate(firmware_files):
                    uploaded_firmwares.append(
                        await asyncio.to_thread(
                            _copy_upload, upload, temp / f"firmware-{index}", total
                        )
                    )
                firmware_sources: Dict[str, Path] = {}
                for firmware in config.firmwares:
                    if firmware.source_path:
                        source = Path(firmware.source_path).expanduser().resolve()
                        if source.suffix.casefold() != f".{firmware.format}" or not source.is_file():
                            raise OfflineDownloadError(
                                f"local firmware source is unavailable: {firmware.file_name}"
                            )
                        if source.stat().st_size <= 0 or source.stat().st_size > _MAX_UPLOAD_SIZE:
                            raise OfflineDownloadError(
                                f"local firmware source has an invalid size: {firmware.file_name}"
                            )
                        firmware_sources[firmware.id] = source
                    elif firmware.upload_index is not None and firmware.upload_index < len(uploaded_firmwares):
                        firmware_sources[firmware.id] = uploaded_firmwares[firmware.upload_index]
                    else:
                        raise OfflineDownloadError(
                            f"missing firmware source: {firmware.file_name}"
                        )
                await asyncio.to_thread(
                    _validate_bin_firmware_ranges,
                    config,
                    online_services,
                    firmware_sources,
                )
                uploaded_flms = []
                for index, upload in enumerate(flm_files):
                    uploaded_flms.append(
                        await asyncio.to_thread(
                            _copy_upload, upload, temp / f"flm-{index}", total
                        )
                    )
                algorithm_sources: Dict[str, Path] = {}
                for algorithm in config.algorithms:
                    if algorithm.source_kind == "upload":
                        if algorithm.upload_index is None or algorithm.upload_index >= len(uploaded_flms):
                            raise OfflineDownloadError("missing uploaded FLM source")
                        algorithm_sources[algorithm.id] = uploaded_flms[algorithm.upload_index]
                    elif algorithm.source_kind == "profile":
                        algorithm_sources[algorithm.id] = await asyncio.to_thread(
                            _profile_source, algorithm.source_token or "", disk_root
                        )
                    elif algorithm.source_kind == "pack":
                        algorithm_sources[algorithm.id] = await asyncio.to_thread(
                            _pack_source,
                            online_services.paths,
                            algorithm.source_token or "",
                            temp / f"pack-{algorithm.id}.flm",
                        )
                return await asyncio.to_thread(
                    deploy_offline_bundle,
                    config,
                    disk_root,
                    firmware_sources=firmware_sources,
                    algorithm_sources=algorithm_sources,
                )
        except (OfflineDownloadError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=422, detail=str(error))
        finally:
            for upload in list(firmware_files) + list(flm_files):
                await upload.close()

    @router.post("/trigger")
    async def trigger(
        request: Request,
        payload: Optional[dict] = Body(default=None),
    ) -> object:
        from mklink.discovery import find_mklink_cdc_port
        from mklink.remote.resource_manager import ResourceGroup

        values = payload or {}
        raw_port = values.get("port")
        port = str(raw_port) if raw_port else None
        try:
            if values:
                model = str(values.get("model") or "").upper()
                command = offline_trigger_command(
                    model,
                    str(values.get("script_name") or "offline_download.py"),
                )
            else:
                command = "load.offline()"
        except OfflineDownloadError as error:
            raise HTTPException(status_code=422, detail=str(error))

        active_bridge = _connected_bridge(port)
        resolved_port = port or (
            None if active_bridge is not None else await asyncio.to_thread(find_mklink_cdc_port)
        )
        if active_bridge is None and not resolved_port:
            raise HTTPException(status_code=400, detail="MKLink CDC port was not found")
        if "application/x-ndjson" in request.headers.get("accept", "").casefold():
            return _trigger_stream(command, active_bridge, resolved_port)

        owner = f"user:offline-download:trigger:{uuid.uuid4().hex}"
        try:
            resource_manager.acquire_many(
                [ResourceGroup.MKLINK_BRIDGE, ResourceGroup.TARGET_DEBUG],
                owner,
                preempt=False,
                preempt_user_dashboard=True,
            )

            response = await asyncio.to_thread(
                _send_trigger,
                command,
                active_bridge,
                resolved_port,
            )
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error))
        finally:
            resource_manager.release(owner)
        lines = _redact_trigger_output(response)
        text = "\n".join(lines).casefold()
        return {
            "status": "completed" if "finished" in text and "aborted" not in text else "failed",
            "lines": lines,
        }

    return router
