"""串口协议 Profile 加载与验证。"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["ProfileError", "load_profile", "validate_profile", "save_profile", "find_profile"]

_VALID_CRC_ALGORITHMS = {"crc8", "crc16_modbus", "crc16_ccitt", "crc32", "checksum8", "checksum16"}
_VALID_CRC_SCOPES = {"payload", "all"}
_VALID_FIELD_TYPES = {"uint8", "int8", "uint16", "int16", "uint32", "int32", "float32"}
_VALID_FIELD_SIZES = {1, 2, 4}
_VALID_ENDIANS = {"little", "big"}


class ProfileError(Exception):
    """Profile loading or validation error."""


def _is_hex_string(s: str) -> bool:
    if not isinstance(s, str) or len(s) == 0 or len(s) % 2 != 0:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def _validate_frame(frame: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(frame, dict):
        errors.append("frame must be a dict")
        return errors

    if "header" not in frame:
        errors.append("frame.header is required")
    elif not _is_hex_string(frame["header"]):
        errors.append("frame.header must be a valid hex string with even length")

    if "tail" in frame and not _is_hex_string(frame["tail"]):
        errors.append("frame.tail must be a valid hex string with even length")

    if "length_field" in frame:
        lf = frame["length_field"]
        if not isinstance(lf, dict):
            errors.append("frame.length_field must be a dict")
        else:
            if "offset" not in lf or not isinstance(lf["offset"], int):
                errors.append("frame.length_field.offset must be an int")
            if "size" not in lf or lf.get("size") not in (1, 2):
                errors.append("frame.length_field.size must be 1 or 2")
            if "includes_header" not in lf or not isinstance(lf["includes_header"], bool):
                errors.append("frame.length_field.includes_header must be a bool")

    if "crc" in frame:
        crc = frame["crc"]
        if not isinstance(crc, dict):
            errors.append("frame.crc must be a dict")
        else:
            if "algorithm" not in crc or crc.get("algorithm") not in _VALID_CRC_ALGORITHMS:
                errors.append(
                    f"frame.crc.algorithm must be one of: {', '.join(sorted(_VALID_CRC_ALGORITHMS))}"
                )
            if "offset" not in crc or not isinstance(crc["offset"], int):
                errors.append("frame.crc.offset must be an int")
            if "scope" not in crc or crc.get("scope") not in _VALID_CRC_SCOPES:
                errors.append(
                    f"frame.crc.scope must be one of: {', '.join(sorted(_VALID_CRC_SCOPES))}"
                )

    return errors


def _validate_field(field: dict, index: int) -> list[str]:
    errors: list[str] = []
    prefix = f"fields[{index}]"

    if not isinstance(field, dict):
        errors.append(f"{prefix} must be a dict")
        return errors

    if "name" not in field or not isinstance(field["name"], str):
        errors.append(f"{prefix}.name is required and must be a string")
    if "offset" not in field or not isinstance(field["offset"], int) or field.get("offset", -1) < 0:
        errors.append(f"{prefix}.offset is required and must be an int >= 0")
    if "size" not in field or field.get("size") not in _VALID_FIELD_SIZES:
        errors.append(f"{prefix}.size is required and must be one of: 1, 2, 4")
    if "type" not in field or field.get("type") not in _VALID_FIELD_TYPES:
        errors.append(
            f"{prefix}.type is required and must be one of: {', '.join(sorted(_VALID_FIELD_TYPES))}"
        )

    if "scale" in field and not isinstance(field["scale"], (int, float)):
        errors.append(f"{prefix}.scale must be a number")
    if "unit" in field and not isinstance(field["unit"], str):
        errors.append(f"{prefix}.unit must be a string")
    if "endian" in field and field["endian"] not in _VALID_ENDIANS:
        errors.append(f"{prefix}.endian must be 'little' or 'big'")

    if "enum" in field:
        enum = field["enum"]
        if not isinstance(enum, dict):
            errors.append(f"{prefix}.enum must be a dict")
        else:
            for key in enum:
                if not isinstance(key, str) or not key.startswith("0x"):
                    errors.append(f"{prefix}.enum keys must be hex strings (e.g. '0x01')")
                    break

    return errors


def _validate_auto_reply(rule: dict, index: int) -> list[str]:
    errors: list[str] = []
    prefix = f"auto_reply[{index}]"

    if not isinstance(rule, dict):
        errors.append(f"{prefix} must be a dict")
        return errors

    has_match = any(k in rule for k in ("match_hex", "match_regex", "match_contains"))
    if not has_match:
        errors.append(f"{prefix} must have at least one of: match_hex, match_regex, match_contains")

    has_reply = any(k in rule for k in ("reply_hex", "reply_ascii"))
    if not has_reply:
        errors.append(f"{prefix} must have at least one of: reply_hex, reply_ascii")

    if "match_hex" in rule and not _is_hex_string(rule["match_hex"]):
        errors.append(f"{prefix}.match_hex must be a valid hex string")

    if "reply_hex" in rule and not _is_hex_string(rule["reply_hex"]):
        errors.append(f"{prefix}.reply_hex must be a valid hex string")

    if "delay" in rule and not isinstance(rule["delay"], (int, float)):
        errors.append(f"{prefix}.delay must be a number")

    return errors


def validate_profile(profile: dict) -> list[str]:
    """Validate profile structure. Returns list of error messages (empty = valid)."""
    errors: list[str] = []

    if not isinstance(profile, dict):
        return ["profile must be a dict"]

    if "name" not in profile or not isinstance(profile.get("name"), str):
        errors.append("name is required and must be a string")
    if "version" not in profile or not isinstance(profile.get("version"), str):
        errors.append("version is required and must be a string")

    if "frame" in profile:
        errors.extend(_validate_frame(profile["frame"]))

    if "fields" in profile:
        fields = profile["fields"]
        if not isinstance(fields, list):
            errors.append("fields must be a list")
        else:
            for i, field in enumerate(fields):
                errors.extend(_validate_field(field, i))

    if "auto_reply" in profile:
        auto_reply = profile["auto_reply"]
        if not isinstance(auto_reply, list):
            errors.append("auto_reply must be a list")
        else:
            for i, rule in enumerate(auto_reply):
                errors.extend(_validate_auto_reply(rule, i))

    return errors


def load_profile(path: str) -> dict:
    """Load and validate a profile from a JSON file. Raises ProfileError on failure."""
    p = Path(path)
    if not p.exists():
        raise ProfileError(f"Profile file not found: {path}")

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ProfileError(f"Cannot read profile file: {e}") from e

    try:
        profile = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProfileError(f"Invalid JSON in profile: {e}") from e

    errors = validate_profile(profile)
    if errors:
        raise ProfileError(f"Profile validation failed:\n  - " + "\n  - ".join(errors))

    return profile


def save_profile(profile: dict, path: str) -> None:
    """Save profile dict to JSON file with pretty formatting."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def find_profile(search_dir: str) -> str | None:
    """Search for a profile file in the given directory and parent dirs.

    Looks for: .mklink/serial_profile.json, serial_profile.json, *.serial-profile.json
    Returns the first found path, or None.
    """
    current = Path(search_dir).resolve()

    for _ in range(4):  # current + up to 3 parent levels
        candidate = current / ".mklink" / "serial_profile.json"
        if candidate.is_file():
            return str(candidate)

        candidate = current / "serial_profile.json"
        if candidate.is_file():
            return str(candidate)

        matches = list(current.glob("*.serial-profile.json"))
        if matches:
            return str(matches[0])

        parent = current.parent
        if parent == current:
            break
        current = parent

    return None
