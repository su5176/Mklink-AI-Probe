"""Low-rate content change detection, independent of probe I/O and timestamps."""
from pathlib import Path

from mklink.file_content import source_fingerprint


class SourceMonitor:
    def __init__(self):
        self.device = None
        self.previous = {}

    def changed(self, device, project: dict) -> list[str]:
        if device is not self.device:
            self.device, self.previous = device, {}
        catalog = getattr(device, "symbol_catalog", None)
        axf = (getattr(device, "_axf", None) or project.get("axf_path")
               or project.get("elf_path") or project.get("out_path"))
        paths = [path for path in (axf, project.get("map_path")) if path]
        current = {}
        changed = []
        for path in dict.fromkeys(paths):
            try:
                current[path] = source_fingerprint(path)
            except OSError:
                current[path] = None
            previous = self.previous.get(path, current[path])
            if path not in self.previous and catalog is not None and Path(path) == Path(catalog.axf_path):
                previous = catalog.fingerprint.to_dict()
            if previous != current[path]:
                changed.append(path)
        self.previous = current
        return changed
