"""Small, dependency-free YMODEM sender used by the serial assistant.

The transport is intentionally expressed as two callbacks so the protocol can
share the serial monitor's already-open port.  ``read`` returns the next bytes
received before its timeout, while ``write`` must write the complete payload or
raise an exception.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable


SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_REQUEST = 0x43
CPMEOF = 0x1A


class YModemError(RuntimeError):
    """Base class for deterministic YMODEM transfer failures."""


class YModemTimeout(YModemError):
    """The receiver did not complete a protocol step in time."""


class YModemCancelled(YModemError):
    """The local caller or remote receiver cancelled the transfer."""


@dataclass(frozen=True)
class YModemProgress:
    """A serializable progress snapshot for API and terminal presentation."""

    phase: str
    sent_bytes: int
    total_bytes: int
    block: int = 0
    retries: int = 0

    @property
    def percent(self) -> int:
        if self.total_bytes <= 0:
            return 100 if self.phase == "completed" else 0
        return min(100, (self.sent_bytes * 100) // self.total_bytes)


ReadCallback = Callable[[float], bytes]
WriteCallback = Callable[[bytes], None]
ProgressCallback = Callable[[YModemProgress], None]


def crc16_xmodem(data: bytes) -> int:
    """Return the CRC-16/XMODEM value for *data*."""

    crc = 0
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _packet(marker: int, block: int, payload: bytes) -> bytes:
    expected = 128 if marker == SOH else 1024
    if len(payload) != expected:
        raise ValueError(f"YMODEM packet payload must contain {expected} bytes")
    crc = crc16_xmodem(payload)
    sequence = block & 0xFF
    return bytes((marker, sequence, 0xFF - sequence)) + payload + crc.to_bytes(2, "big")


class YModemSender:
    """Send one file using YMODEM batch framing and CRC-16/XMODEM."""

    def __init__(
        self,
        read: ReadCallback,
        write: WriteCallback,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
        handshake_timeout: float = 10.0,
        packet_timeout: float = 3.0,
        retries: int = 10,
    ):
        if handshake_timeout <= 0 or packet_timeout <= 0:
            raise ValueError("YMODEM timeouts must be positive")
        if retries < 0:
            raise ValueError("YMODEM retries must be nonnegative")
        self._read = read
        self._write = write
        self._cancel_event = cancel_event or threading.Event()
        self._progress_callback = progress_callback
        self._handshake_timeout = float(handshake_timeout)
        self._packet_timeout = float(packet_timeout)
        self._retries = int(retries)
        self._pending = bytearray()
        self._cancel_sent = False

    def send_file(self, path: str | os.PathLike[str], *, name: str | None = None) -> None:
        source_path = Path(path)
        file_name = name if name is not None else source_path.name
        with source_path.open("rb") as source:
            self.send(source, file_name, source_path.stat().st_size)

    def send(self, source: BinaryIO, name: str, size: int) -> None:
        if not isinstance(size, int) or size < 0:
            raise ValueError("YMODEM file size must be a nonnegative integer")
        header = self._header_payload(name, size)
        sent = 0
        block = 1
        try:
            self._progress("waiting", sent, size)
            self._wait_with_retries({CRC_REQUEST}, self._handshake_timeout, "receiver handshake")

            self._send_packet(_packet(SOH, 0, header), "file header", sent, size, 0)
            self._wait_with_retries({CRC_REQUEST}, self._handshake_timeout, "data request")
            self._progress("transferring", sent, size, block)

            while sent < size:
                self._check_cancelled()
                chunk_size = min(1024, size - sent)
                data = source.read(chunk_size)
                if not isinstance(data, (bytes, bytearray)) or len(data) != chunk_size:
                    raise YModemError("source ended before the declared YMODEM file size")
                payload = bytes(data).ljust(1024, bytes((CPMEOF,)))
                self._send_packet(
                    _packet(STX, block, payload), "data block", sent, size, block,
                )
                sent += chunk_size
                self._progress("transferring", sent, size, block)
                block = (block + 1) & 0xFF

            self._progress("finishing", sent, size, block)
            self._finish_file(sent, size, block)
            self._wait_with_retries({CRC_REQUEST}, self._handshake_timeout, "final header request")
            self._send_packet(
                _packet(SOH, 0, bytes(128)), "empty final header", sent, size, 0,
            )
            self._progress("completed", sent, size, block)
        except YModemCancelled:
            self._send_cancel()
            raise
        except Exception:
            self._send_cancel()
            raise

    def take_pending_rx(self) -> bytes:
        """Return bytes received after the final consumed protocol control.

        A receiver may put its last ACK and reboot banner in one serial read.
        ``_wait_control`` consumes only through the ACK, leaving the banner in
        ``_pending``.  The serial monitor calls this after a successful send so
        those ordinary terminal bytes can return to its single reader thread.
        """
        # Some receivers append a redundant CRC request (or duplicate ACK)
        # after accepting the empty batch header.  Those leading bytes still
        # belong to YMODEM and must not appear as terminal text.
        while self._pending and self._pending[0] in (
            ACK, NAK, CAN, CRC_REQUEST,
        ):
            self._pending.pop(0)
        pending = bytes(self._pending)
        self._pending.clear()
        return pending

    @staticmethod
    def _header_payload(name: str, size: int) -> bytes:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("YMODEM filename must be non-empty and cannot contain NUL")
        safe_name = name.replace("\\", "/").rsplit("/", 1)[-1]
        encoded_name = safe_name.encode("utf-8")
        encoded_size = str(size).encode("ascii")
        raw = encoded_name + b"\x00" + encoded_size + b"\x00"
        if len(raw) > 128:
            raise ValueError("YMODEM filename and size do not fit the 128-byte header")
        return raw.ljust(128, b"\x00")

    def _send_packet(
        self,
        packet: bytes,
        stage: str,
        sent: int,
        total: int,
        block: int,
    ) -> None:
        for attempt in range(self._retries + 1):
            self._check_cancelled()
            self._write(packet)
            try:
                response = self._wait_control({ACK, NAK}, self._packet_timeout, stage)
            except YModemTimeout:
                if attempt == self._retries:
                    raise
            else:
                if response == ACK:
                    return
                if attempt == self._retries:
                    raise YModemError(f"receiver rejected {stage} after {attempt + 1} attempts")
            self._progress("retrying", sent, total, block, attempt + 1)
        raise AssertionError("unreachable YMODEM packet retry state")

    def _finish_file(self, sent: int, total: int, block: int) -> None:
        for attempt in range(self._retries + 1):
            self._check_cancelled()
            self._write(bytes((EOT,)))
            try:
                response = self._wait_control({ACK, NAK}, self._packet_timeout, "end of file")
            except YModemTimeout:
                if attempt == self._retries:
                    raise
            else:
                if response == ACK:
                    return
                # The canonical YMODEM handshake answers the first EOT with NAK
                # and the second with ACK.  Looping also tolerates extra NAKs.
                if attempt == self._retries:
                    raise YModemError("receiver rejected end of file")
            self._progress("retrying", sent, total, block, attempt + 1)

    def _wait_with_retries(self, expected: set[int], timeout: float, stage: str) -> int:
        last_error: YModemTimeout | None = None
        for _ in range(self._retries + 1):
            try:
                return self._wait_control(expected, timeout, stage)
            except YModemTimeout as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _wait_control(self, expected: set[int], timeout: float, stage: str) -> int:
        deadline = time.monotonic() + timeout
        while True:
            self._check_cancelled()
            while self._pending:
                value = self._pending.pop(0)
                # MicroBoot's receiver sends one CAN when flash erase/write or
                # packet handling fails.  Treat the first CAN as authoritative
                # instead of requiring the optional duplicated CAN convention.
                if value == CAN:
                    raise YModemCancelled(f"receiver cancelled during {stage}")
                if value in expected:
                    return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise YModemTimeout(f"timeout waiting for {stage}")
            chunk = self._read(remaining)
            if chunk:
                self._pending.extend(chunk)

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise YModemCancelled("YMODEM transfer cancelled")

    def _send_cancel(self) -> None:
        if self._cancel_sent:
            return
        self._cancel_sent = True
        try:
            self._write(bytes((CAN, CAN)))
        except Exception:
            pass

    def _progress(
        self,
        phase: str,
        sent: int,
        total: int,
        block: int = 0,
        retries: int = 0,
    ) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback(YModemProgress(phase, sent, total, block, retries))
