"""Structured MKLink offline-download configuration and USB deployment."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil
import tempfile
from typing import Mapping, Optional, Sequence, Union

from mklink.offline_security import OfflineSecurityPlan, resolve_offline_security


_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODELS = frozenset(("V2", "V3", "V4"))
_SOURCE_KINDS = frozenset(("upload", "pack", "profile", "existing"))


class OfflineDownloadError(ValueError):
    """Invalid offline configuration or deployment state."""


@dataclass(frozen=True)
class OfflineAlgorithm:
    id: str
    file_name: str
    flash_base: int
    ram_base: int
    source_kind: str
    source_token: Optional[str]
    upload_index: Optional[int]
    source_path: Optional[str]


@dataclass(frozen=True)
class OfflineFirmware:
    id: str
    file_name: str
    format: str
    base_address: Optional[int]
    algorithm_id: str
    upload_index: Optional[int]
    source_path: Optional[str]


@dataclass(frozen=True)
class OfflineDownloadConfig:
    model: str
    requested_script_name: str
    auto_download_count: int
    wait_idcode_timeout_ms: int
    swd_clock_hz: int
    target_part: Optional[str]
    board: Optional[str]
    hpm_flash_cfg: Optional[tuple[str, str, str, str]]
    erase_all_before_download: bool
    algorithms: tuple[OfflineAlgorithm, ...]
    firmwares: tuple[OfflineFirmware, ...]
    security: Optional[OfflineSecurityPlan]

    @property
    def script_name(self) -> str:
        return script_filename(self.model, self.requested_script_name)

    @property
    def is_hpm(self) -> bool:
        from mklink.hpm_config import is_hpm_target

        return is_hpm_target(self.target_part)


def _parse_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise OfflineDownloadError(f"{label} must be an integer")
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise OfflineDownloadError(f"{label} must be an integer")
    if parsed < 0:
        raise OfflineDownloadError(f"{label} must be nonnegative")
    return parsed


def _positive_int(value: object, label: str, minimum: int, maximum: int) -> int:
    parsed = _parse_int(value, label)
    if parsed < minimum or parsed > maximum:
        raise OfflineDownloadError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def _strict_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise OfflineDownloadError(f"{label} must be a boolean")
    return value


def _identifier(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text or not _SAFE_FILE_NAME.fullmatch(text):
        raise OfflineDownloadError(f"{label} is invalid")
    return text


def _file_name(value: object, label: str, suffixes: tuple[str, ...]) -> str:
    text = _identifier(value, label)
    if Path(text).suffix.casefold() not in suffixes:
        expected = "/".join(suffixes)
        raise OfflineDownloadError(f"{label} must use {expected}")
    return text


def script_filename(model: str, requested_name: str) -> str:
    normalized = str(model).upper()
    if normalized not in _MODELS:
        raise OfflineDownloadError("probe model must be V2, V3, or V4")
    if normalized in ("V2", "V3"):
        return "offline_download.py"
    return _file_name(requested_name or "offline_download.py", "script name", (".py",))


def offline_trigger_command(model: str, requested_name: str) -> str:
    normalized = str(model).upper()
    if normalized in ("V2", "V3"):
        return "load.offline()"
    name = script_filename(normalized, requested_name)
    return f'load.offline("Python/{name}")'


def parse_offline_config(
    payload: Mapping[str, object],
    *,
    resolved_model: Optional[str] = None,
) -> OfflineDownloadConfig:
    raw_model = str(payload.get("model") or "auto").upper()
    model = str(resolved_model or raw_model).upper()
    if model == "AUTO" or model not in _MODELS:
        raise OfflineDownloadError("probe model could not be detected")

    count = _positive_int(
        payload.get("auto_download_count", 1),
        "automatic download count",
        1,
        9999,
    )
    if model == "V2" and count != 1:
        raise OfflineDownloadError("V2 automatic download count must be 1")
    timeout = _positive_int(
        payload.get("wait_idcode_timeout_ms", 10000),
        "IDCODE timeout",
        500,
        600000,
    )
    if timeout % 500:
        raise OfflineDownloadError("IDCODE timeout must be a multiple of 500 ms")
    swd_clock = _positive_int(
        payload.get("swd_clock_hz", 10000000),
        "SWD clock",
        100000,
        10000000,
    )
    target_part = str(payload.get("target_part") or "").strip() or None
    from mklink.hpm_config import is_hpm_target, normalize_hpm_configuration

    hpm_target = is_hpm_target(target_part)
    erase_all_before_download = _strict_bool(
        payload.get("erase_all_before_download", False),
        "erase-all-before-download",
    )
    if hpm_target and erase_all_before_download:
        raise OfflineDownloadError("HPM ROM API does not support FLM chip erase")
    raw_board = str(payload.get("board") or "").strip()
    raw_hpm_flash_cfg = payload.get("hpm_flash_cfg")
    if not hpm_target and (raw_board or raw_hpm_flash_cfg not in (None, "")):
        raise OfflineDownloadError("HPM board and flash config are only valid for HPM targets")
    board = None
    hpm_flash_cfg = None
    if hpm_target:
        try:
            board, hpm_flash_cfg = normalize_hpm_configuration(
                target_part,
                board=raw_board or None,
                flash_cfg=(
                    None if raw_hpm_flash_cfg in (None, "") else raw_hpm_flash_cfg
                ),
            )
        except (TypeError, ValueError) as error:
            raise OfflineDownloadError(str(error)) from error

    raw_algorithms = payload.get("algorithms")
    if not isinstance(raw_algorithms, Sequence) or isinstance(raw_algorithms, (str, bytes)):
        raise OfflineDownloadError("at least one FLM algorithm is required")
    algorithms = []
    seen_algorithm_ids = set()
    seen_algorithm_names = set()
    for index, raw in enumerate(raw_algorithms):
        if not isinstance(raw, Mapping):
            raise OfflineDownloadError("FLM algorithm entry must be an object")
        algorithm_id = _identifier(raw.get("id") or f"algorithm-{index}", "algorithm id")
        if algorithm_id.casefold() in seen_algorithm_ids:
            raise OfflineDownloadError("algorithm ids must be unique")
        seen_algorithm_ids.add(algorithm_id.casefold())
        file_name = _file_name(raw.get("file_name"), "FLM file name", (".flm",))
        if file_name.casefold() in seen_algorithm_names:
            raise OfflineDownloadError("FLM file names must be unique")
        seen_algorithm_names.add(file_name.casefold())
        source_kind = str(raw.get("source_kind") or "upload").casefold()
        if source_kind not in _SOURCE_KINDS:
            raise OfflineDownloadError("unsupported FLM source kind")
        upload_index = raw.get("upload_index")
        parsed_upload_index = (
            _parse_int(upload_index, "FLM upload index")
            if upload_index is not None
            else None
        )
        source_token = str(raw.get("source_token") or "").strip() or None
        source_path = str(raw.get("source_path") or "").strip() or None
        if source_kind == "upload" and (
            (parsed_upload_index is None) == (source_path is None)
        ):
            raise OfflineDownloadError(
                "local FLM requires exactly one upload index or local source path"
            )
        if source_kind != "upload" and source_path is not None:
            raise OfflineDownloadError("only local FLM may use a source path")
        if source_kind in ("pack", "profile") and not source_token:
            raise OfflineDownloadError(f"{source_kind} FLM requires a source token")
        algorithms.append(
            OfflineAlgorithm(
                id=algorithm_id,
                file_name=file_name,
                flash_base=_parse_int(raw.get("flash_base"), "FLM flash base"),
                ram_base=_parse_int(raw.get("ram_base"), "FLM RAM base"),
                source_kind=source_kind,
                source_token=source_token,
                upload_index=parsed_upload_index,
                source_path=source_path,
            )
        )
    if hpm_target and algorithms:
        raise OfflineDownloadError("HPM targets do not use FLM algorithms")
    if not hpm_target and not algorithms:
        raise OfflineDownloadError("at least one FLM algorithm is required")

    raw_firmwares = payload.get("firmwares")
    if not isinstance(raw_firmwares, Sequence) or isinstance(raw_firmwares, (str, bytes)):
        raise OfflineDownloadError("at least one firmware file is required")
    firmwares = []
    seen_firmware_ids = set()
    seen_firmware_names = set()
    for index, raw in enumerate(raw_firmwares):
        if not isinstance(raw, Mapping):
            raise OfflineDownloadError("firmware entry must be an object")
        firmware_id = _identifier(raw.get("id") or f"firmware-{index}", "firmware id")
        if firmware_id.casefold() in seen_firmware_ids:
            raise OfflineDownloadError("firmware ids must be unique")
        seen_firmware_ids.add(firmware_id.casefold())
        file_name = _file_name(raw.get("file_name"), "firmware file name", (".bin", ".hex"))
        if file_name.casefold() in seen_firmware_names:
            raise OfflineDownloadError("firmware file names must be unique")
        seen_firmware_names.add(file_name.casefold())
        image_format = str(raw.get("format") or Path(file_name).suffix[1:]).casefold()
        if image_format not in ("bin", "hex") or Path(file_name).suffix.casefold() != f".{image_format}":
            raise OfflineDownloadError("firmware format does not match its file name")
        algorithm_id = str(raw.get("algorithm_id") or "").strip()
        if not hpm_target and algorithm_id.casefold() not in seen_algorithm_ids:
            raise OfflineDownloadError("firmware references an unknown FLM algorithm")
        base_address = None
        if image_format == "bin":
            if raw.get("base_address") in (None, ""):
                raise OfflineDownloadError("BIN firmware requires a base address")
            base_address = _parse_int(raw.get("base_address"), "BIN base address")
        elif hpm_target:
            raise OfflineDownloadError("HPM ROM API only supports BIN firmware")
        raw_upload_index = raw.get("upload_index")
        source_path = str(raw.get("source_path") or "").strip() or None
        upload_index = (
            _parse_int(raw_upload_index, "firmware upload index")
            if raw_upload_index is not None
            else None
        )
        if (upload_index is None) == (source_path is None):
            raise OfflineDownloadError(
                "firmware requires exactly one upload index or local source path"
            )
        firmwares.append(
            OfflineFirmware(
                id=firmware_id,
                file_name=file_name,
                format=image_format,
                base_address=base_address,
                algorithm_id=algorithm_id,
                upload_index=upload_index,
                source_path=source_path,
            )
        )
    if not firmwares:
        raise OfflineDownloadError("at least one firmware file is required")

    try:
        security = resolve_offline_security(
            payload,
            model=model,
            part_number=target_part,
        )
    except ValueError as error:
        raise OfflineDownloadError(str(error)) from error

    return OfflineDownloadConfig(
        model=model,
        requested_script_name=str(payload.get("script_name") or "offline_download.py"),
        auto_download_count=count,
        wait_idcode_timeout_ms=timeout,
        swd_clock_hz=swd_clock,
        target_part=target_part,
        board=board,
        hpm_flash_cfg=hpm_flash_cfg,
        erase_all_before_download=erase_all_before_download,
        algorithms=tuple(algorithms),
        firmwares=tuple(firmwares),
        security=security,
    )


def _program_lines(config: OfflineDownloadConfig, indent: str) -> list[str]:
    if config.is_hpm:
        lines = []
        if config.board:
            setup_call = f'hpm.board("{config.board}")'
        else:
            cfg = config.hpm_flash_cfg or ()
            setup_call = "hpm.flash_cfg({})".format(",".join(cfg))
        lines.extend((
            f"{indent}if {setup_call} != 0:",
            f'{indent}    print("HPM flash configuration failed")',
            f"{indent}    abort = True",
            f"{indent}    break",
        ))
        for firmware in config.firmwares:
            call = f'hpm.program("{firmware.file_name}", 0x{firmware.base_address:08X})'
            lines.extend((
                f"{indent}if {call} != 0:",
                f'{indent}    print("HPM program failed: {firmware.file_name}")',
                f"{indent}    abort = True",
                f"{indent}    break",
            ))
        return lines

    algorithms = {algorithm.id: algorithm for algorithm in config.algorithms}
    lines = []
    active_algorithm = None
    if config.erase_all_before_download:
        erase_algorithm = algorithms[config.firmwares[0].algorithm_id]
        lines.extend(
            (
                f'{indent}if load.flm("FLM/{erase_algorithm.file_name}", '
                f"0x{erase_algorithm.flash_base:08X}, 0x{erase_algorithm.ram_base:08X}) != 0:",
                f'{indent}    print("load flm failed: {erase_algorithm.file_name}")',
                f"{indent}    abort = True",
                f"{indent}    break",
                f"{indent}if cmd.erase_chip_flash(0x{erase_algorithm.flash_base:08X}) != 0:",
                f'{indent}    print("chip erase failed: 0x{erase_algorithm.flash_base:08X}")',
                f"{indent}    abort = True",
                f"{indent}    break",
            )
        )
        active_algorithm = erase_algorithm.id
    for firmware in config.firmwares:
        algorithm = algorithms[firmware.algorithm_id]
        if active_algorithm != algorithm.id:
            lines.extend(
                (
                    f'{indent}if load.flm("FLM/{algorithm.file_name}", '
                    f"0x{algorithm.flash_base:08X}, 0x{algorithm.ram_base:08X}) != 0:",
                    f'{indent}    print("load flm failed: {algorithm.file_name}")',
                    f"{indent}    abort = True",
                    f"{indent}    break",
                )
            )
            active_algorithm = algorithm.id
        if firmware.format == "hex":
            call = f'load.hex("{firmware.file_name}")'
        else:
            call = f'load.bin("{firmware.file_name}", 0x{firmware.base_address:08X})'
        lines.extend(
            (
                f"{indent}if {call} != 0:",
                f'{indent}    print("load {firmware.format} failed: {firmware.file_name}")',
                f"{indent}    abort = True",
                f"{indent}    break",
            )
        )
    return lines


def _security_lines(
    security: OfflineSecurityPlan,
    *,
    action: str,
    indent: str,
) -> list[str]:
    config_file = f"CFG/{security.config_dir}/{action}.cfg"
    result_name = f"security_{action}_rc"
    return [
        (
            f'{indent}if load.flm("FLM/{security.algorithm_file_name}", '
            f"0x{security.option_address:08X}, 0x{security.ram_base:08X}) != 0:"
        ),
        f'{indent}    print("load option flm failed: {security.algorithm_file_name}")',
        f"{indent}    abort = True",
        f"{indent}    break",
        f'{indent}{result_name} = cmd.{action}("{config_file}")',
        f"{indent}if {result_name} != 0:",
        f'{indent}    print("security {action} failed:", {result_name})',
        f"{indent}    abort = True",
        f"{indent}    break",
    ]


def _security_api_preflight_lines(indent: str) -> list[str]:
    missing = "CFG/__mklink_security_api_probe_missing__.cfg"
    return [
        f'{indent}security_unlock_api = cmd.unlock("{missing}")',
        f'{indent}security_lock_api = cmd.lock("{missing}")',
        f"{indent}if security_unlock_api != -101 or security_lock_api != -101:",
        (
            f'{indent}    print("security command ABI check failed:", '
            "security_unlock_api, security_lock_api)"
        ),
        f"{indent}    abort = True",
        f"{indent}    break",
    ]


def _generate_v2_script(config: OfflineDownloadConfig) -> str:
    lines = [
        "import PikaStdLib",
        "import time",
        "import cmd",
        "import hpm" if config.is_hpm else "import load",
        "",
        f"cmd.set_swd_clock({config.swd_clock_hz})",
        "abort = False",
        "for i in range(1):",
    ]
    lines.extend(_program_lines(config, "    "))
    lines.extend(
        (
            "if not abort:",
            '    print("offline download finished")',
            "    cmd.set_reset()",
            "    cmd.set_beep_on()",
            "    time.sleep_ms(1000)",
            "    cmd.set_beep_off()",
            "else:",
            '    print("offline download aborted")',
            "",
        )
    )
    return "\n".join(lines)


def generate_offline_script(config: OfflineDownloadConfig) -> str:
    if config.model == "V2":
        return _generate_v2_script(config)
    lines = [
        "import PikaStdLib",
        "import time",
        "import cmd",
        "import hpm" if config.is_hpm else "import load",
        "",
        f"AUTO_DOWNLOAD_COUNT = {config.auto_download_count}",
        f"WAIT_IDCODE_TIMEOUT = {config.wait_idcode_timeout_ms}",
        f"cmd.set_swd_clock({config.swd_clock_hz})",
        "",
        "abort = False",
        "for i in range(AUTO_DOWNLOAD_COUNT):",
        "    if abort:",
        "        break",
        '    print("=== Auto Download Round:", i + 1, "===")',
        "    elapsed = 0",
        "    while True:",
        "        idcode = cmd.get_idcode()",
        "        if idcode not in (0, 0xFFFFFFFF):",
        "            break",
        "        if elapsed >= WAIT_IDCODE_TIMEOUT:",
        '            print("wait idcode online timeout")',
        "            abort = True",
        "            break",
        "        time.sleep_ms(500)",
        "        elapsed += 500",
        "    if abort:",
        "        break",
        '    print("IDCODE: 0x%08X" % idcode)',
    ]
    if config.security is not None:
        # Missing-file calls stop before target access and verify that the
        # generated Pika bindings preserve the signed security return code.
        insert_at = lines.index("    elapsed = 0")
        lines[insert_at:insert_at] = _security_api_preflight_lines("    ")
    if config.security is not None and config.security.unlock_before_download:
        lines.extend(_security_lines(config.security, action="unlock", indent="    "))
    lines.extend(_program_lines(config, "    "))
    if config.security is not None and config.security.lock_after_download:
        lines.extend(_security_lines(config.security, action="lock", indent="    "))
    lines.extend(
        (
            "    if abort or i + 1 >= AUTO_DOWNLOAD_COUNT:",
            "        break",
            "    elapsed = 0",
            "    while True:",
            "        idcode = cmd.get_idcode()",
            "        if idcode in (0, 0xFFFFFFFF):",
            "            break",
            "        if elapsed >= WAIT_IDCODE_TIMEOUT:",
            '            print("wait idcode offline timeout")',
            "            abort = True",
            "            break",
            "        time.sleep_ms(500)",
            "        elapsed += 500",
            "if not abort:",
            '    print("auto download finished")',
            "    cmd.set_reset()",
            "    cmd.cpu_run()",
            "    cmd.set_beep_on()",
            "    time.sleep_ms(1000)",
            "    cmd.set_beep_off()",
            "else:",
            '    print("auto download aborted")',
            "",
        )
    )
    return "\n".join(lines)


SourceCollection = Union[Sequence[Path], Mapping[str, Path]]


def _source_for(
    sources: SourceCollection,
    *,
    item_id: str,
    upload_index: Optional[int],
    description: str,
) -> Path:
    try:
        if isinstance(sources, Mapping):
            value = sources[item_id]
        else:
            if upload_index is None:
                raise IndexError
            value = sources[upload_index]
    except (KeyError, IndexError):
        raise OfflineDownloadError(f"missing {description} source")
    path = Path(value)
    if not path.is_file():
        raise OfflineDownloadError(f"{description} source does not exist")
    return path


def _relative_destination(relative: Path) -> Path:
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise OfflineDownloadError("offline destination is invalid")
    return relative


def _remove_probe_file(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        path.chmod(0o666)
        path.unlink()


def _same_file_content(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            chunk = left.read(1024 * 1024)
            if chunk != right.read(1024 * 1024):
                return False
            if not chunk:
                return True


def _transactional_copy(
    disk_root: Path,
    files: Sequence[tuple[Path, Optional[Path], Optional[bytes]]],
) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="mklink-offline-staging-") as raw_stage:
        stage = Path(raw_stage)
        backup_root = stage / "backup"
        staged_root = stage / "files"
        installed = []
        backups = []
        try:
            for relative, source, content in files:
                relative = _relative_destination(relative)
                staged = staged_root / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                if source is not None:
                    shutil.copy2(source, staged)
                else:
                    staged.write_bytes(content or b"")

            for relative, _source, _content in files:
                relative = _relative_destination(relative)
                destination = disk_root / relative
                staged = staged_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    # A browser may still hold the selected USB file open. Reuse
                    # identical content instead of deleting or rewriting it.
                    if _same_file_content(destination, staged):
                        continue
                    backup = backup_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup)
                    _remove_probe_file(destination)
                    backups.append((destination, backup))
                installed.append(destination)
                shutil.copy2(staged, destination)
            return [
                relative.as_posix() for relative, _source, _content in files
            ]
        except BaseException as error:
            for destination in reversed(installed):
                try:
                    if destination.exists():
                        destination.unlink()
                except OSError:
                    pass
            for destination, backup in reversed(backups):
                try:
                    if backup.exists():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, destination)
                except OSError:
                    pass
            if isinstance(error, OSError):
                raise OfflineDownloadError(
                    f"offline file operation failed: {relative.as_posix()}: {error}"
                ) from error
            raise


def deploy_offline_bundle(
    config: OfflineDownloadConfig,
    disk_root: Union[str, Path],
    *,
    firmware_sources: SourceCollection,
    algorithm_sources: SourceCollection,
) -> dict:
    disk = Path(disk_root).resolve()
    if not disk.is_dir():
        raise OfflineDownloadError("MICROKEEN disk is unavailable")

    plan = []
    seen_destinations = set()
    for firmware in config.firmwares:
        relative = Path(firmware.file_name)
        source = _source_for(
            firmware_sources,
            item_id=firmware.id,
            upload_index=firmware.upload_index,
            description="firmware",
        )
        key = str(relative).casefold()
        if key in seen_destinations:
            raise OfflineDownloadError("offline destination names must be unique")
        seen_destinations.add(key)
        plan.append((relative, source, None))

    for algorithm in config.algorithms:
        relative = Path("FLM") / algorithm.file_name
        if algorithm.source_kind == "existing":
            if not (disk / relative).is_file():
                raise OfflineDownloadError(f"existing FLM is missing: {algorithm.file_name}")
            continue
        source = _source_for(
            algorithm_sources,
            item_id=algorithm.id,
            upload_index=algorithm.upload_index,
            description="FLM",
        )
        key = str(relative).casefold()
        if key in seen_destinations:
            raise OfflineDownloadError("offline destination names must be unique")
        seen_destinations.add(key)
        plan.append((relative, source, None))

    if config.security is not None:
        security = config.security
        try:
            digest = hashlib.sha256(security.algorithm_path.read_bytes()).hexdigest()
        except OSError as error:
            raise OfflineDownloadError("built-in option-byte FLM is unavailable") from error
        if digest != security.algorithm_sha256:
            raise OfflineDownloadError("built-in option-byte FLM integrity check failed")
        security_files = [
            (
                Path("FLM") / security.algorithm_file_name,
                security.algorithm_path,
                None,
            ),
        ]
        if security.unlock_before_download:
            security_files.append((
                Path("CFG") / security.config_dir / "unlock.cfg",
                None,
                security.unlock_config,
            ))
        if security.lock_after_download:
            security_files.append((
                Path("CFG") / security.config_dir / "lock.cfg",
                None,
                security.lock_config,
            ))
        for relative, source, content in security_files:
            key = str(relative).casefold()
            if key in seen_destinations:
                raise OfflineDownloadError("offline destination names must be unique")
            seen_destinations.add(key)
            plan.append((relative, source, content))

    script_relative = Path("python") / config.script_name
    plan.append((script_relative, None, generate_offline_script(config).encode("utf-8")))
    deployed = _transactional_copy(disk, plan)
    return {
        "status": "deployed",
        "model": config.model,
        "script_name": config.script_name,
        "files": deployed,
    }
