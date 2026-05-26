"""串口数据文件日志记录器。"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import IO


def _is_printable_ascii(data: bytes) -> bool:
    return all(b in range(0x20, 0x7F) or b in (0x0A, 0x0D, 0x09) for b in data)


def _format_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


class FileLogger:
    def __init__(self, path: str, format: str = "txt", max_size: int = 0) -> None:
        self._path = Path(path)
        self._format = format
        self._max_size = max_size
        self._file: IO[str] | None = None
        self._lock = threading.Lock()
        self._csv_fields: list[str] | None = None

    def start(self) -> None:
        with self._lock:
            self._file = open(self._path, "w", encoding="utf-8", newline="")
            if self._format == "csv":
                self._csv_fields = None

    def log(self, direction: str, port: str, data: bytes, decoded: dict | None = None) -> None:
        with self._lock:
            if self._file is None:
                return

            now = datetime.now()

            if self._format == "txt":
                self._write_txt(now, direction, port, data, decoded)
            else:
                self._write_csv(now, direction, port, data, decoded)

            self._file.flush()
            self._maybe_rotate()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._file.close()
                self._file = None

    def __enter__(self) -> FileLogger:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _write_txt(
        self,
        now: datetime,
        direction: str,
        port: str,
        data: bytes,
        decoded: dict | None,
    ) -> None:
        ts = _format_timestamp(now)
        if _is_printable_ascii(data):
            text = data.decode("ascii").rstrip("\r\n")
            line = f"[{ts}] {direction} {port}: {text} (ASCII)\n"
        else:
            hex_str = " ".join(f"{b:02X}" for b in data)
            line = f"[{ts}] {direction} {port}: {hex_str}\n"

        self._file.write(line)  # type: ignore[union-attr]

        if decoded:
            parts = []
            for key, info in decoded.items():
                val = info.get("value", "")
                unit = info.get("unit", "")
                parts.append(f"{key}={val}{unit}")
            self._file.write(f"  → {', '.join(parts)}\n")  # type: ignore[union-attr]

    def _write_csv(
        self,
        now: datetime,
        direction: str,
        port: str,
        data: bytes,
        decoded: dict | None,
    ) -> None:
        if self._csv_fields is None:
            if decoded:
                self._csv_fields = list(decoded.keys())
            else:
                self._csv_fields = []
            header = "timestamp,direction,port,raw_hex,ascii"
            if self._csv_fields:
                header += "," + ",".join(self._csv_fields)
            self._file.write(header + "\n")  # type: ignore[union-attr]

        ts = _format_timestamp(now)
        raw_hex = data.hex().upper()
        ascii_val = data.decode("ascii") if _is_printable_ascii(data) else ""

        row = f"{ts},{direction},{port},{raw_hex},{ascii_val}"

        if self._csv_fields:
            for field in self._csv_fields:
                if decoded and field in decoded:
                    val = decoded[field].get("value", "")
                    unit = decoded[field].get("unit", "")
                    row += f",{val}{unit}"
                else:
                    row += ","

        self._file.write(row + "\n")  # type: ignore[union-attr]

    def _maybe_rotate(self) -> None:
        if self._max_size <= 0 or self._file is None:
            return

        self._file.flush()
        try:
            size = os.fstat(self._file.fileno()).st_size
        except OSError:
            size = self._path.stat().st_size

        if size < self._max_size:
            return

        self._file.close()

        now = datetime.now()
        stem = self._path.stem
        suffix = self._path.suffix
        rotated_name = f"{stem}_{now.strftime('%Y%m%d_%H%M%S')}{suffix}"
        rotated_path = self._path.parent / rotated_name
        self._path.rename(rotated_path)

        self._file = open(self._path, "w", encoding="utf-8", newline="")
        if self._format == "csv" and self._csv_fields is not None:
            header = "timestamp,direction,port,raw_hex,ascii"
            if self._csv_fields:
                header += "," + ",".join(self._csv_fields)
            self._file.write(header + "\n")
