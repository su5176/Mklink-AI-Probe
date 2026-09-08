"""Project-scoped SuperWatch favorites. Store names, never runtime addresses."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import threading

_LOCK = threading.RLock()
MAX_PINS = 128
_MAX_BYTES = 128 * 1024


class PreferencesConflict(ValueError):
    pass


def _path(project_root: str) -> Path:
    root = Path(project_root).resolve()
    path = root / '.mklink' / 'superwatch_pins.json'
    for part in (path.parent, path):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('SuperWatch preferences must not redirect outside the project')
    return path


def normalize_pins(pins: object) -> list[str]:
    if not isinstance(pins, list) or len(pins) > MAX_PINS:
        raise ValueError(f'Expected at most {MAX_PINS} pinned variable names')
    result = []
    for name in pins:
        if (not isinstance(name, str) or not name.strip() or len(name) > 512
                or any(ord(char) < 32 for char in name)):
            raise ValueError('Invalid pinned variable name')
        name = name.strip()
        if name not in result:
            result.append(name)
    return result


def load_pins(project_root: str) -> dict:
    with _LOCK:
        path = _path(project_root)
        try:
            with path.open('rb') as source:
                raw = source.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            raw = b''
        if len(raw) > _MAX_BYTES:
            raise ValueError('SuperWatch preferences are too large')
        data = json.loads(raw.decode('utf-8-sig')) if raw else {'version': 1, 'pins': []}
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('Unsupported SuperWatch preferences format')
        return {'pins': normalize_pins(data.get('pins')), 'revision': hashlib.sha256(raw).hexdigest()}


def save_pins(project_root: str, pins: object, revision: str) -> dict:
    pins = normalize_pins(pins)
    with _LOCK:
        current = load_pins(project_root)
        if current['revision'] != revision:
            raise PreferencesConflict('Pinned variables changed in another window; reload and retry')
        path = _path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f'.{path.name}.{secrets.token_hex(8)}.tmp'
        try:
            with temporary.open('x', encoding='utf-8') as output:
                json.dump({'version': 1, 'pins': pins}, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            _path(project_root)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return load_pins(project_root)
