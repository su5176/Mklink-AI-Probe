"""Process-wide serialization for lazy pyOCD imports.

pyOCD imports a broad, cyclic module graph.  Importing different pyOCD
submodules concurrently from FastAPI worker threads can make Python 3.14
detect a cross-thread module-lock deadlock.  Keep imports lazy, but ensure
that only one thread initializes any part of that graph at a time.
"""

from __future__ import annotations

from importlib import import_module as _import_module
from threading import RLock
from types import ModuleType
from typing import Any


_PYOCD_IMPORT_LOCK = RLock()


def import_pyocd_module(module_name: str) -> ModuleType:
    """Import a pyOCD module without racing another lazy pyOCD import."""

    name = str(module_name).strip()
    if name != "pyocd" and not name.startswith("pyocd."):
        raise ValueError("Only pyOCD modules may use the serialized importer")
    with _PYOCD_IMPORT_LOCK:
        return _import_module(name)


def import_pyocd_attr(module_name: str, attribute: str) -> Any:
    """Return one attribute from a lazily imported pyOCD module."""

    with _PYOCD_IMPORT_LOCK:
        return getattr(import_pyocd_module(module_name), attribute)
