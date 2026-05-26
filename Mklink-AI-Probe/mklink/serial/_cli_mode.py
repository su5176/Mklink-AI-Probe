"""串口调试 CLI 交互终端。"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mklink.serial._monitor import SerialEvent, SerialMonitor

_COLORS = {
    "reset": "\033[0m",
    "gray": "\033[90m",
    "green": "\033[32m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "bold": "\033[1m",
}

_DEFAULT_HIGHLIGHTS: dict[str, str] = {
    r"(?i)ERROR|FAIL": "red",
    r"(?i)WARN": "yellow",
    r"(?i)\bOK\b|PASS": "green",
}


def _enable_ansi_windows() -> None:
    """Enable ANSI escape sequence processing on Windows 10+."""
    if os.name != "nt":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def _color(name: str, text: str) -> str:
    return f"{_COLORS[name]}{text}{_COLORS['reset']}"


class CLIMode:
    def __init__(
        self,
        monitor: SerialMonitor,
        mode: str = "ascii",
        filter_pattern: str | None = None,
        highlight_rules: dict[str, str] | None = None,
    ):
        self._monitor = monitor
        self._mode = mode
        self._filter_pattern = filter_pattern
        self._filter_re: re.Pattern[str] | None = (
            re.compile(filter_pattern) if filter_pattern else None
        )
        self._highlights = highlight_rules if highlight_rules is not None else dict(_DEFAULT_HIGHLIGHTS)
        self._output_lock = threading.Lock()
        self._input_buffer = ""
        self._running = False

    def run(self) -> None:
        """Main loop: display received data + accept user input. Blocks until user exits."""
        _enable_ansi_windows()
        self._running = True

        old_callback = self._monitor._event_callback
        self._monitor._event_callback = self._on_event

        try:
            self._print_header()
            self._monitor.start()
            self._input_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self._monitor._event_callback = old_callback
            self._monitor.stop()
            sys.stdout.write(_COLORS["reset"] + "\n")
            sys.stdout.flush()

    def _print_header(self) -> None:
        ports_desc = ", ".join(
            f"{cfg['port']} ({cfg.get('baudrate', 115200)} "
            f"{cfg.get('bytesize', 8)}{cfg.get('parity', 'N')[0]}{cfg.get('stopbits', 1)})"
            for cfg in self._monitor._port_configs
        )
        filter_desc = self._filter_pattern or "None"
        log_desc = "ON" if self._monitor._logger else "OFF"

        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            f"║  MKLink Serial Terminal{' ' * 39}║",
            f"║  Port(s): {ports_desc:<51}║",
            f"║  Mode: {self._mode.upper():<6}| Filter: {filter_desc:<14}| Log: {log_desc:<9}║",
            "║  Ctrl+Q: Quit | Ctrl+H: Toggle HEX | Ctrl+F: Set Filter     ║",
            "║  Ctrl+L: Clear | >hex AA55: Send HEX                         ║",
            "╚══════════════════════════════════════════════════════════════╝",
        ]
        sys.stdout.write("\n".join(lines) + "\n\n")
        sys.stdout.flush()

    def _on_event(self, event: SerialEvent) -> None:
        if self._filter_re:
            text = event.raw.decode("latin-1", errors="replace")
            if not self._filter_re.search(text):
                return

        line = self._format_event(event)
        line = self._apply_highlights(line)

        with self._output_lock:
            sys.stdout.write(f"\r\033[K{line}\n")
            if self._input_buffer:
                sys.stdout.write(f"> {self._input_buffer}")
            sys.stdout.flush()

    def _format_event(self, event: SerialEvent) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        ms = f"{event.timestamp % 1:.3f}"[1:]
        ts_str = _color("gray", f"[{ts}{ms}]")

        dir_color = "green" if event.direction == "RX" else "blue"
        dir_str = _color(dir_color, event.direction)

        port_str = event.port + ":"

        if self._mode == "hex":
            data_str = " ".join(f"{b:02X}" for b in event.raw)
        else:
            data_str = event.raw.decode("utf-8", errors="replace").rstrip("\r\n")

        parts = [ts_str, dir_str, port_str, data_str]

        if event.parsed and event.parsed.fields:
            decoded = ", ".join(
                f"{k}={v}" for k, v in event.parsed.fields.items()
            )
            parts.append(_color("cyan", f"→ {decoded}"))

        return " ".join(parts)

    def _apply_highlights(self, line: str) -> str:
        for pattern, color_name in self._highlights.items():
            if re.search(pattern, line):
                line = f"{_COLORS[color_name]}{line}{_COLORS['reset']}"
                break
        return line

    def _input_loop(self) -> None:
        if os.name == "nt":
            self._input_loop_windows()
        else:
            self._input_loop_posix()

    def _input_loop_windows(self) -> None:
        import msvcrt

        while self._running:
            if not msvcrt.kbhit():
                time.sleep(0.02)
                continue

            ch = msvcrt.getwch()

            if ch == "\x11":  # Ctrl+Q
                break
            elif ch == "\x03":  # Ctrl+C
                break
            elif ch == "\x08" and len(ch) == 1:  # Ctrl+H
                self._toggle_mode()
            elif ch == "\x0c":  # Ctrl+L
                self._clear_screen()
            elif ch == "\x06":  # Ctrl+F
                self._prompt_filter()
            elif ch == "\r":
                self._handle_send()
            elif ch == "\x7f" or ch == "\x08":
                # Backspace
                if self._input_buffer:
                    self._input_buffer = self._input_buffer[:-1]
                    with self._output_lock:
                        sys.stdout.write(f"\r\033[K> {self._input_buffer}")
                        sys.stdout.flush()
            elif ch >= " ":
                self._input_buffer += ch
                with self._output_lock:
                    sys.stdout.write(f"\r\033[K> {self._input_buffer}")
                    sys.stdout.flush()

    def _input_loop_posix(self) -> None:
        import select
        import tty
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running:
                rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not rlist:
                    continue
                ch = sys.stdin.read(1)

                if ch == "\x11" or ch == "\x03":
                    break
                elif ch == "\x08":
                    self._toggle_mode()
                elif ch == "\x0c":
                    self._clear_screen()
                elif ch == "\x06":
                    self._prompt_filter()
                elif ch in ("\r", "\n"):
                    self._handle_send()
                elif ch == "\x7f":
                    if self._input_buffer:
                        self._input_buffer = self._input_buffer[:-1]
                        with self._output_lock:
                            sys.stdout.write(f"\r\033[K> {self._input_buffer}")
                            sys.stdout.flush()
                elif ch >= " ":
                    self._input_buffer += ch
                    with self._output_lock:
                        sys.stdout.write(f"\r\033[K> {self._input_buffer}")
                        sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _toggle_mode(self) -> None:
        self._mode = "hex" if self._mode == "ascii" else "ascii"
        with self._output_lock:
            sys.stdout.write(f"\r\033[K[Mode: {self._mode.upper()}]\n")
            sys.stdout.flush()

    def _clear_screen(self) -> None:
        with self._output_lock:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            self._print_header()

    def _prompt_filter(self) -> None:
        with self._output_lock:
            sys.stdout.write("\r\033[KFilter regex (empty=clear): ")
            sys.stdout.flush()

        buf = ""
        if os.name == "nt":
            import msvcrt
            while True:
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    break
                elif ch == "\x7f" or ch == "\x08":
                    buf = buf[:-1]
                else:
                    buf += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()
        else:
            buf = sys.stdin.readline().strip()

        if buf:
            try:
                self._filter_re = re.compile(buf)
                self._filter_pattern = buf
            except re.error:
                with self._output_lock:
                    sys.stdout.write(f"\n{_color('red', 'Invalid regex')}\n")
                    sys.stdout.flush()
                return
        else:
            self._filter_re = None
            self._filter_pattern = None

        with self._output_lock:
            desc = self._filter_pattern or "None"
            sys.stdout.write(f"\n[Filter: {desc}]\n")
            sys.stdout.flush()

    def _handle_send(self) -> None:
        line = self._input_buffer.strip()
        self._input_buffer = ""

        with self._output_lock:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        if not line:
            data = b"\r\n"
        elif line.startswith(">hex "):
            hex_str = line[5:].replace(" ", "")
            try:
                data = bytes.fromhex(hex_str)
            except ValueError:
                with self._output_lock:
                    sys.stdout.write(_color("red", "Invalid hex input") + "\n")
                    sys.stdout.flush()
                return
        elif line.startswith(">file "):
            filepath = line[6:].strip()
            try:
                with open(filepath, "rb") as f:
                    data = f.read()
            except OSError as e:
                with self._output_lock:
                    sys.stdout.write(_color("red", f"File error: {e}") + "\n")
                    sys.stdout.flush()
                return
        else:
            data = (line + "\r\n").encode("utf-8")

        self._monitor.send(data)
