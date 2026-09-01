"""
mklink.mcp_server — MCP (Model Context Protocol) server exposing mklink's
embedded-debug capabilities as vendor-neutral tools.

Architecture
------------
Independent process speaking stdio transport. Holds a single ``Device``
singleton (lazily connected via the ``connect`` tool). Hardware access is
serialized across this MCP process and any concurrent ``mklink serve``
(FastAPI) process by the file-based ``SerialLock`` (bridge.py:39) — they
never collide on the probe.

This is the **能力/管道 (capability/plumbing)** layer of the mklink plugin:

    Plugin shell  (.claude-plugin/plugin.json + .mcp.json)
    ├─ MCP layer  (this file)  — atomic tools + encoded decision logic
    ├─ Skill layer (SKILL.md + references/) — orchestration methodology
    └─ Shared SDK (mklink.device.Device + subsystems) — reused by MCP & CLI

Design principle: MCP tools do *atomic operations + smart defaults*; the
Skill teaches *when/how to orchestrate* them.

Run
---
    python -m mklink mcp
or auto-loaded by Claude Code via the plugin's ``.mcp.json`` (skills-dir
plugin, no marketplace required).

Tools are registered with ``@mcp.tool()`` and grouped by capability
(_register_* helpers) so Phase 2/3 additions stay isolated.
"""
from __future__ import annotations

from contextlib import contextmanager
import atexit
from functools import wraps
import logging
import sys
import threading
from typing import Any, Iterator, TextIO

from pydantic import StrictInt, StrictStr

log = logging.getLogger("mklink.mcp")

MCP_MAX_DIRECT_READ_BYTES = 4096
MCP_MAX_BATCH_REGIONS = 16
MCP_MAX_BATCH_TOTAL_BYTES = 4096
MCP_MAX_WRITE_BYTES = 4096
MCP_MAX_FLUSH_WRITES = 8
MCP_MAX_FLUSH_ITEM_BYTES = 12 * 1024
MCP_MAX_FLUSH_TOTAL_BYTES = 12 * 1024
MCP_MAX_CAPTURE_SECONDS = 30.0
MCP_MAX_SEARCH_BYTES = 64 * 1024
MCP_MAX_RTT_WRITE_BYTES = 256
MCP_MAX_RTT_PATTERN_BYTES = 256


class _McpProtocolStdout:
    """Keep JSON-RPC on stdout while routing ordinary prints to stderr."""

    def __init__(self, protocol_stream: TextIO, diagnostic_stream: TextIO) -> None:
        self._protocol_stream = protocol_stream
        self._diagnostic_stream = diagnostic_stream

    @property
    def buffer(self) -> Any:
        return self._protocol_stream.buffer

    def write(self, text: str) -> int:
        return self._diagnostic_stream.write(text)

    def flush(self) -> None:
        self._diagnostic_stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._diagnostic_stream, name)


@contextmanager
def _isolate_stdio_protocol() -> Iterator[None]:
    protocol_stdout = sys.stdout
    sys.stdout = _McpProtocolStdout(protocol_stdout, sys.stderr)
    try:
        yield
    finally:
        sys.stdout = protocol_stdout

# --------------------------------------------------------------------------
# Lazy Device singleton (double-checked locking).
# Mirrors controller-vtfp-builder vtfp_core/mcp/mcp_server_base.py:71-96.
# --------------------------------------------------------------------------
_lock = threading.Lock()
_operation_lock = threading.RLock()
_holder: dict[str, Any] = {"device": None, "kwargs": {}, "quarantine": None}


@contextmanager
def _hardware_operation(tool_name: str, *, recovery: bool = False) -> Iterator[None]:
    """Serialize probe I/O and quarantine a session after an uncertain timeout."""
    if not _operation_lock.acquire(blocking=False):
        raise RuntimeError(
            "Another MKLink hardware tool is active. Do not issue parallel "
            "calls to one probe; wait for it to finish or stop the stream."
        )
    try:
        with _lock:
            quarantine = _holder.get("quarantine")
        if quarantine is not None and not recovery:
            failed_tool = quarantine.get("tool", "unknown")
            raise RuntimeError(
                "MKLink hardware access is quarantined after "
                f"{failed_tool} timed out. Do not retry hardware commands; "
                "call device_status, then disconnect before reconnecting."
            )
        try:
            yield
        except TimeoutError as exc:
            with _lock:
                _holder["quarantine"] = {
                    "active": True,
                    "tool": tool_name,
                    "reason": str(exc) or "hardware operation timed out",
                }
            raise
    finally:
        _operation_lock.release()


def _exclusive_hardware_tool(function=None, *, recovery: bool = False):
    """Reject concurrent or quarantined probe tools before they reach I/O."""
    def decorate(callback):
        @wraps(callback)
        def guarded(*args, **kwargs):
            with _hardware_operation(callback.__name__, recovery=recovery):
                return callback(*args, **kwargs)
        return guarded

    if function is None:
        return decorate
    return decorate(function)


def _bounded_duration(value: float, field: str = "duration") -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not 0 < duration <= MCP_MAX_CAPTURE_SECONDS:
        raise ValueError(
            f"{field} must be greater than 0 and at most "
            f"{MCP_MAX_CAPTURE_SECONDS:g} seconds"
        )
    return duration


def _validate_memory_range(address: int, size: int, *, max_size: int) -> None:
    if type(address) is not int or not 0 <= address <= 0xFFFFFFFF:
        raise ValueError("address must be a 32-bit non-negative integer")
    if type(size) is not int or not 0 < size <= max_size:
        raise ValueError(f"size must be between 1 and {max_size} bytes")
    if address + size > 0x100000000:
        raise ValueError("address + size exceeds the 32-bit address space")


def _capabilities() -> dict[str, Any]:
    return {
        "one_probe_calls": "serial-only; never call hardware tools in parallel",
        "connect": "reuse one session; disconnect when finished",
        "direct_read_max_bytes": MCP_MAX_DIRECT_READ_BYTES,
        "batch_read_max_regions": MCP_MAX_BATCH_REGIONS,
        "batch_read_max_total_bytes": MCP_MAX_BATCH_TOTAL_BYTES,
        "write_memory_max_bytes": MCP_MAX_WRITE_BYTES,
        "flush_memory_max_regions": MCP_MAX_FLUSH_WRITES,
        "flush_memory_max_item_bytes": MCP_MAX_FLUSH_ITEM_BYTES,
        "flush_memory_max_total_bytes": MCP_MAX_FLUSH_TOTAL_BYTES,
        "capture_max_seconds": MCP_MAX_CAPTURE_SECONDS,
        "rtt_search_max_bytes": MCP_MAX_SEARCH_BYTES,
        "rtt_write_max_utf8_bytes": MCP_MAX_RTT_WRITE_BYTES,
        "rtt_pattern_max_utf8_bytes": MCP_MAX_RTT_PATTERN_BYTES,
        "failure_recovery": (
            "a timed-out hardware tool quarantines the session; check "
            "device_status, then disconnect before reconnecting"
        ),
    }


def configure_device(**kwargs: Any) -> None:
    """Set Device constructor kwargs (port/axf/mcu/project_root)."""
    with _lock:
        _holder["kwargs"] = dict(kwargs)


def _get_device() -> Any:
    """Return the lazy Device singleton, constructing it if absent."""
    d = _holder["device"]
    if d is not None:
        return d
    with _lock:
        d = _holder["device"]
        if d is not None:
            return d
        from mklink.device import Device
        d = Device(**_holder["kwargs"])
        _holder["device"] = d
        return d


def _connected_device() -> Any:
    """Return a connected Device, else raise DeviceNotConnectedError.

    Hardware tools call this first so a cold call surfaces a clear
    "call connect() first" message instead of an opaque AttributeError.
    """
    dev = _holder["device"]
    if dev is None or not dev.connected:
        from mklink.device import DeviceNotConnectedError
        raise DeviceNotConnectedError(
            "No connected device. Call the `connect` tool first."
        )
    return dev


def _reset_device() -> None:
    """Drop the cached Device (after disconnect / on connect failure)."""
    with _lock:
        d = _holder["device"]
        if d is not None:
            try:
                d.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                log.exception("error closing device during reset")
        _holder["device"] = None
        _holder["kwargs"] = {}
        _holder["quarantine"] = None


# MCP hosts normally terminate the stdio child after the conversation ends.
# Keep the explicit `disconnect` tool for normal sessions, but also close the
# cached Device when the host exits without sending that final tool call.
atexit.register(_reset_device)


# --------------------------------------------------------------------------
# Serialization helpers (MCP speaks JSON; bytes must be carried as hex)
# --------------------------------------------------------------------------
def _hex(data: bytes) -> str:
    return data.hex()


def _from_hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").replace("\n", ""))


def _idcode(dev: Any) -> str | None:
    return f"0x{dev.idcode:08X}" if dev.connected else None


# ==========================================================================
# Tool groups
# ==========================================================================
def _register_health_tools(mcp: Any) -> None:
    @mcp.tool()
    def ping() -> dict:
        """Health check for the mklink MCP server.

        Call this first to confirm the server is alive before invoking any
        hardware tool. Requires no device connection. Also reports the
        effective built-in ELF/DWARF backend and optional external GNU tool
        availability. AXF features use the bundled backend by default.
        """
        from importlib.metadata import version, PackageNotFoundError
        from mklink.toolchain import status as toolchain_status
        from mklink.update_check import check_for_update
        try:
            ver = version("mklink")
        except PackageNotFoundError:  # pragma: no cover
            ver = "unknown"
        return {
            "ok": True,
            "server": "mklink-ai-probe",
            "transport": "stdio",
            "sdk_version": ver,
            "update": check_for_update(),
            "limits": _capabilities(),
            **toolchain_status(),
        }


def _register_connection_tools(mcp: Any) -> None:
    @mcp.tool()
    def discover_probes() -> list[dict]:
        """List all MKLink/MicroLink probes currently attached via USB.

        Returns one entry per probe with keys: port, description,
        manufacturer. Call this when the user is unsure which COM port to
        use, or to confirm a probe is physically connected before flashing.
        """
        import mklink
        return mklink.discover_all()

    @mcp.tool()
    @_exclusive_hardware_tool
    def connect(
        port: str | None = None,
        axf: str | None = None,
        mcu: str | None = None,
        project_root: str = ".",
        elf_backend: str | None = None,
    ) -> dict:
        """Connect to an MKLink probe and establish a debug session.

        Args:
            port: COM port (e.g. "COM5"). Auto-detected if omitted.
            axf: Path to AXF/ELF firmware file — REQUIRED for variable
                read/write, type info, and memory map. Pass it whenever the
                user wants symbolic debugging, not just raw memory/flash.
            mcu: MCU profile hint (e.g. "stm32f4"). Usually auto-detected
                from IDCODE; set only if detection fails.
            project_root: Project root holding ``.mklink/`` config (mcu_key,
                swd_clock, rtt_config.json). Defaults to current dir.
            elf_backend: Explicit ``builtin`` or ``external`` selection.

        Replaces any existing session (releases the serial lock first).
        """
        import mklink
        requested = {
            "port": port, "axf": axf, "mcu": mcu,
            "project_root": project_root, "elf_backend": elf_backend,
        }
        current = _holder["device"]
        if (
            current is not None
            and current.connected
            and _holder["kwargs"] == requested
        ):
            return {
                "connected": True,
                "reused": True,
                "port": current.port,
                "idcode": _idcode(current),
                "mcu": current.mcu_name,
                "axf_loaded": bool(getattr(current, "_dwarf_info", None)),
                "elf_backend": current.axf_status.get("elf_backend"),
                "limits": _capabilities(),
            }
        _reset_device()
        dev = mklink.connect(
            port=port,
            axf=axf,
            mcu=mcu,
            project_root=project_root,
            elf_backend=elf_backend,
        )
        with _lock:
            _holder["device"] = dev
            _holder["kwargs"] = {
                "port": port, "axf": axf, "mcu": mcu,
                "project_root": project_root, "elf_backend": elf_backend,
            }
        axf_loaded = bool(getattr(dev, "_dwarf_info", None))
        out: dict = {
            "connected": dev.connected,
            "port": dev.port,
            "idcode": _idcode(dev),
            "mcu": dev.mcu_name if dev.connected else None,
            "axf_loaded": axf_loaded,
            "elf_backend": dev.axf_status.get("elf_backend"),
            "reused": False,
            "limits": _capabilities(),
        }
        if not axf_loaded and axf:
            # AXF was requested but symbols did not load; surface the parser
            # error instead of deferring it until read_variable.
            status = getattr(dev, "axf_status", {}) or {}
            out["axf_error"] = (
                status.get("error") if isinstance(status, dict) else None
            ) or getattr(dev, "_axf_error", None) or "unknown"
            out.update({
                key: status.get(key)
                for key in (
                    "elf_backend",
                    "builtin_elf_available",
                    "external_elf_available",
                    "readelf_available",
                    "addr2line_available",
                )
            })
        return out

    @mcp.tool()
    @_exclusive_hardware_tool(recovery=True)
    def disconnect() -> dict:
        """Disconnect from the probe and release the serial lock.

        Always call this when done, so other processes (GUI, CLI) can access
        the probe. Idempotent — safe to call when already disconnected.
        """
        dev = _holder["device"]
        was = bool(dev and dev.connected)
        _reset_device()
        return {"disconnected": True, "was_connected": was}

    @mcp.tool()
    def device_status() -> dict:
        """Query the current connection state without touching hardware.

        Use to check whether a session is alive before a long operation, or
        to recover context after a tool error. No side effects.
        """
        with _lock:
            quarantine = _holder.get("quarantine")
        dev = _holder["device"]
        if dev is None:
            result = {"connected": False, "hint": "no device; call connect() first"}
        else:
            result = {
                "connected": dev.connected,
                "port": dev.port,
                "idcode": _idcode(dev),
                "mcu": dev.mcu_name if dev.connected else None,
                "state": str(dev.state),
                "axf_loaded": bool(getattr(dev, "_dwarf_info", None)),
            }
        result["quarantined"] = quarantine is not None
        if quarantine is not None:
            result["quarantine"] = dict(quarantine)
        return result


def _register_project_tools(mcp: Any) -> None:
    @mcp.tool()
    def detect_mcu_profile(
        project_root: str = ".",
        device: str | None = None,
        port: str | None = None,
        flm: str | None = None,
        write_profile: bool = True,
        copy_flm: bool = True,
        read_idcode: bool = False,
    ) -> dict:
        """Detect or create an MCU profile and resolve its FLM.

        Use before flashing a project whose MCU is not already present in
        ``mcu_profiles.json``. If multiple internal FLM algorithms are found,
        returns ``status=needs_selection`` with candidates; call again with
        ``flm`` set to the selected algorithm path to persist it.
        """
        from mklink.mcu_detect import detect_mcu_profile as _detect

        arguments = {
            "project_root": project_root,
            "device": device,
            "port": port,
            "flm": flm,
            "write_profile": write_profile,
            "copy_flm": copy_flm,
            "read_idcode": read_idcode,
        }
        if read_idcode:
            with _hardware_operation("detect_mcu_profile"):
                return _detect(**arguments)
        return _detect(**arguments)


def _register_flash_tools(mcp: Any) -> None:
    from mklink.observe_bridge import flash_facts, observe_operation

    @mcp.tool()
    @_exclusive_hardware_tool
    def flash(
        firmware: str,
        target_part: str | None = None,
        base_address: int | None = None,
        board: str | None = None,
        hpm_flash_cfg: list[str] | None = None,
        verify: bool = True,
        reset_after: bool = True,
    ) -> dict:
        """Flash a HEX or BIN firmware image to the target MCU.

        One-shot: resolves MCU profile + FLM + SWD clock from project config,
        erases/programs/verifies, optionally resets. Prefer this over manual
        erase+write sequences.

        Args:
            firmware: Path to .hex or .bin file.
            target_part: Exact MCU part number. When supplied, the shared
                user-Pack/builtin-Pack/custom-FLM catalog is used without
                requiring Keil on the host. HPMicro targets instead use the
                device-side HPM ROM API and never load an FLM.
            base_address: Required BIN load address when unavailable from
                project configuration. HPM SDK images commonly use 0x80000400.
            board: Optional HPM board name such as hpm5301evklite.
            hpm_flash_cfg: Optional four-word HPM flash configuration used
                when no board name is supplied.
            verify: Read back and compare after programming (default True).
            reset_after: Reset the target to entry point after flash (default
                True). Set False to keep the CPU halted for inspection.
        """
        with observe_operation(
            "program.flash", capability="program", action_class="emit",
        ) as observation:
            dev = _connected_device()
            result = dev.flash(
                firmware,
                target_part=target_part,
                base_address=base_address,
                board=board,
                hpm_flash_cfg=hpm_flash_cfg,
                verify=verify,
                reset_after=reset_after,
                progress_callback=observation.progress,
            )
            observation.complete(facts=flash_facts(
                result,
                verify_requested=verify,
                reset_requested=reset_after,
            ))
            return result

    @mcp.tool()
    @_exclusive_hardware_tool
    def erase_chip() -> dict:
        """Erase the entire target flash. Destructive — no confirmation
        beyond this call. Returns {"erased": bool}."""
        dev = _connected_device()
        return {"erased": dev.erase_chip()}

    @mcp.tool()
    @_exclusive_hardware_tool
    def erase_sector(address: int) -> dict:
        """Erase a single flash sector containing ``address``.

        Args:
            address: Sector address (e.g. 0x08004000).
        """
        dev = _connected_device()
        return {"erased": dev.erase_sector(address), "address": f"0x{address:08X}"}

    @mcp.tool()
    @_exclusive_hardware_tool
    def reset() -> dict:
        """Reset the target MCU (system reset, re-runs from entry point)."""
        dev = _connected_device()
        dev.reset()
        return {"reset": True}

    @mcp.tool()
    @_exclusive_hardware_tool
    def set_power_on(
        voltage_mv: int,
        confirm_5v: bool = False,
        confirm_user: bool = False,
    ) -> dict:
        """Enable MKLink VCC output at exactly 1.8 V, 3.3 V, or 5 V.

        Args:
            voltage_mv: One of 1800, 3300, or 5000 millivolts.
            confirm_user: Must be True for every request, and only after the
                user explicitly approves this exact voltage for this call.
                Never reuse an earlier confirmation or infer consent.
            confirm_5v: Must be True for every 5000 mV request, and only after
                the user has verified that the connected target is 5 V
                tolerant.  Applying 5 V to a 3.3 V target can destroy it.
        """
        if confirm_user is not True:
            raise ValueError(
                "VCC output requires explicit user confirmation for this "
                "voltage; ask the user, then pass confirm_user=True only "
                "after they approve this request"
            )
        dev = _connected_device()
        dev.set_power_on(voltage_mv, confirm_5v=confirm_5v)
        return {"power_on": True, "voltage_mv": voltage_mv}

    @mcp.tool()
    @_exclusive_hardware_tool
    def reboot_probe() -> dict:
        """Reboot the MKLink probe itself and release the current session.

        This is different from ``reset``, which resets only the target MCU.
        The probe disconnects and must be connected again after enumeration.
        """
        dev = _connected_device()
        try:
            dev.reboot()
        finally:
            _reset_device()
        return {"rebooted": True, "connected": False}


def _register_memory_tools(mcp: Any) -> None:
    from mklink.mcp_stream_bridge import (
        McpStreamSidecar,
        publish_mcp_memory,
        publish_mcp_memory_gap,
        publish_mcp_memory_regions,
    )
    from mklink.observe_bridge import (
        memory_dump_facts,
        memory_read_facts,
        memory_write_facts,
        observe_operation,
    )
    from mklink.remote.stream_protocol import canonical_memory_address

    @mcp.tool()
    @_exclusive_hardware_tool
    def read_memory(address: int, size: int) -> dict:
        """Read ``size`` bytes of target RAM/peripheral memory at ``address``.

        Args:
            address: Read address, e.g. 0x20000000 (RAM) or 0xE000ED28
                (SCB.CFSR peripheral register).
            size: Number of bytes, from 1 through 4096. Larger reads are slow
                over SWD; use bounded dump_memory captures instead.

        Returns hex-encoded bytes. Decode on the client side as needed.
        """
        with observe_operation(
            "memory.read",
            capability="target.memory",
            action_class="observe",
        ) as observation:
            _validate_memory_range(
                address, size, max_size=MCP_MAX_DIRECT_READ_BYTES
            )
            dev = _connected_device()
            data = dev.read_memory(address, size)
            if len(data) != size:
                raise RuntimeError("memory read returned incomplete data")
            response = {
                "address": canonical_memory_address(address),
                "size": size,
                "bytes_read": len(data),
                "hex": _hex(data),
            }
            try:
                private_published = publish_mcp_memory("read", address, data)
            except Exception:
                private_published = False
            if not private_published:
                try:
                    publish_mcp_memory_gap("publish_drop_count", 1)
                except Exception:
                    pass
            observation.complete(facts=memory_read_facts(
                response["address"],
                requested_bytes=size,
                bytes_read=len(data),
            ))
            return response

    @mcp.tool()
    @_exclusive_hardware_tool
    def read_memory_regions(regions: list[dict]) -> dict:
        """Read up to 16 RAM/peripheral regions in one logical snapshot.

        The host merges only overlapping or exactly contiguous addresses, so
        the common 16-scalar layout uses one REPL/SWD transaction. Disjoint
        addresses remain separate and preserve request order. This tool is
        preferred over repeated ``read_memory`` calls.

        Args:
            regions: List of {"address": int, "size": int}; at most 16
                entries and 4096 returned bytes in total.
        """
        if not isinstance(regions, list) or not regions:
            raise ValueError("regions must be a non-empty list")
        if len(regions) > MCP_MAX_BATCH_REGIONS:
            raise ValueError(
                f"regions must contain at most {MCP_MAX_BATCH_REGIONS} entries"
            )
        pairs: list[tuple[int, int]] = []
        for index, region in enumerate(regions):
            if not isinstance(region, dict) or set(region) != {"address", "size"}:
                raise ValueError(
                    f"regions[{index}] must contain exactly address and size"
                )
            address, size = region["address"], region["size"]
            _validate_memory_range(
                address, size, max_size=MCP_MAX_BATCH_TOTAL_BYTES
            )
            pairs.append((address, size))
        total = sum(size for _, size in pairs)
        if total > MCP_MAX_BATCH_TOTAL_BYTES:
            raise ValueError(
                f"total requested bytes must not exceed "
                f"{MCP_MAX_BATCH_TOTAL_BYTES}"
            )
        dev = _connected_device()
        payloads = dev.read_memory_regions(pairs)
        return {
            "region_count": len(pairs),
            "total_bytes": sum(len(payload) for payload in payloads),
            "regions": [
                {
                    "address": f"0x{address:08X}",
                    "size": size,
                    "hex": payload.hex(),
                }
                for (address, size), payload in zip(pairs, payloads)
            ],
        }

    @mcp.tool()
    @_exclusive_hardware_tool
    def write_memory(address: int, data_hex: str) -> dict:
        """Write bytes to target RAM at ``address`` and verify by reading back.

        Args:
            address: Write address, e.g. 0x20001000.
            data_hex: Hex string of bytes to write, e.g. "DEADBEEF" or
                "de ad be ef" (whitespace tolerated).
        """
        with observe_operation(
            "memory.write",
            capability="target.memory",
            action_class="emit",
        ) as observation:
            data = _from_hex(data_hex)
            _validate_memory_range(
                address, len(data), max_size=MCP_MAX_WRITE_BYTES
            )
            dev = _connected_device()
            dev.write_memory(address, data)
            actual = dev.read_memory(address, len(data))
            if actual != data:
                from mklink.device import DeviceError
                raise DeviceError(
                    f"write verification failed at 0x{address:08X}: "
                    f"expected {data.hex()}, got {actual.hex()}"
                )
            response = {
                "address": canonical_memory_address(address),
                "bytes_written": len(data),
                "verified": True,
            }
            observation.complete(facts=memory_write_facts(
                response["address"],
                bytes_written=len(data),
            ))
            return response

    @mcp.tool()
    def dump_memory(
        regions: list[dict],
        sample_count: int = 1,
        timeout: float = 10.0,
    ) -> dict:
        """Capture a bounded number of complete dump-memory samples.

        This reuses the already-connected probe bridge.  It is a bounded
        capture, not a persistent stream: ``sample_count`` is 1..64 and the
        total captured bytes across all samples cannot exceed 512 KiB.

        Args:
            regions: 1..8 closed objects of ``{"address": int, "size": int}``.
            sample_count: Complete one-shot samples to capture (default 1).
            timeout: Per-sample timeout in seconds, from 0.001 through 60.
        """
        import math
        import secrets

        from mklink.dump_memory import (
            DumpMemoryReadError,
            MAX_TOTAL_DATA_SIZE,
            exclusive_dump_memory_capture,
            read_dump_memory_regions_once,
        )
        from mklink.remote.stream_protocol import MAX_MEMORY_REGIONS

        with observe_operation(
            "memory.dump",
            capability="target.memory",
            action_class="observe",
        ) as observation:
            if not isinstance(regions, list) or not 1 <= len(regions) <= MAX_MEMORY_REGIONS:
                raise ValueError(
                    f"regions must contain 1..{MAX_MEMORY_REGIONS} entries"
                )
            if (
                isinstance(sample_count, bool)
                or not isinstance(sample_count, int)
                or not 1 <= sample_count <= 64
            ):
                raise ValueError("sample_count must be between 1 and 64")
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not 0.001 <= float(timeout) <= 60.0
            ):
                raise ValueError("timeout must be between 0.001 and 60 seconds")

            pairs: list[tuple[int, int]] = []
            per_sample_bytes = 0
            for index, region in enumerate(regions):
                if not isinstance(region, dict) or set(region) != {"address", "size"}:
                    raise ValueError(
                        f"regions[{index}] must contain only address and size"
                    )
                address = region["address"]
                size = region["size"]
                if (
                    isinstance(address, bool)
                    or not isinstance(address, int)
                    or not 0 <= address <= 0xFFFFFFFFFFFFFFFF
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size <= 0
                    or address + size > 0x10000000000000000
                ):
                    raise ValueError(f"regions[{index}] has an invalid address/size range")
                per_sample_bytes += size
                pairs.append((address, size))
            if per_sample_bytes > MAX_TOTAL_DATA_SIZE:
                raise ValueError(
                    f"one sample exceeds the {MAX_TOTAL_DATA_SIZE}-byte device limit"
                )
            max_capture_bytes = 512 * 1024
            if per_sample_bytes * sample_count > max_capture_bytes:
                raise ValueError(
                    f"capture exceeds the {max_capture_bytes}-byte MCP limit"
                )

            dev = _connected_device()
            operation_id = f"op-{secrets.token_hex(8)}"
            samples = []
            with exclusive_dump_memory_capture():
                for sample_index in range(sample_count):
                    try:
                        payloads = read_dump_memory_regions_once(
                            dev._bridge,
                            pairs,
                            timeout=float(timeout),
                        )
                        if (
                            len(payloads) != len(pairs)
                            or any(
                                not isinstance(payload, bytes) or len(payload) != size
                                for (_address, size), payload in zip(pairs, payloads)
                            )
                        ):
                            raise DumpMemoryReadError(
                                "dump_memory returned incomplete region coverage",
                                gap_fact="region_gap_count",
                            )
                    except DumpMemoryReadError as exc:
                        try:
                            publish_mcp_memory_gap(exc.gap_fact, exc.gap_count)
                        except Exception:
                            pass
                        raise
                    try:
                        private_published = publish_mcp_memory_regions(
                            "dump",
                            list(zip((address for address, _size in pairs), payloads)),
                            sample_index=sample_index,
                            sample_count=sample_count,
                            operation_id=operation_id,
                        )
                    except Exception:
                        private_published = False
                    if not private_published:
                        try:
                            publish_mcp_memory_gap("publish_drop_count", 1)
                        except Exception:
                            pass
                    samples.append({
                        "sample_index": sample_index,
                        "regions": [
                            {
                                "address": canonical_memory_address(address),
                                "size": len(payload),
                                "data_hex": payload.hex().upper(),
                            }
                            for (address, _size), payload in zip(pairs, payloads)
                        ],
                    })
            response = {
                "sample_count": sample_count,
                "region_count": len(pairs),
                "total_bytes": per_sample_bytes * sample_count,
                "samples": samples,
            }
            observation.complete(facts=memory_dump_facts(
                canonical_memory_address(pairs[0][0]),
                total_bytes=response["total_bytes"],
                region_count=len(pairs),
                sample_count=sample_count,
            ))
            return response


def _register_variable_tools(mcp: Any) -> None:
    from mklink.mcp_stream_bridge import publish_mcp_superwatch

    @mcp.tool()
    @_exclusive_hardware_tool
    def read_variable(name: str) -> Any:
        """Read a named global variable by DWARF symbol (requires AXF/ELF).

        Args:
            name: Variable path, e.g. "sensor_count" or "config.threshold".
                Supports struct member paths.

        Returns the decoded value (int/float/str/enum). The AXF/ELF must have
        been passed to ``connect(axf=...)`` or loaded via ``load_symbols``.
        """
        dev = _connected_device()
        value = dev.read_variable(name)
        publish_mcp_superwatch(name, value)
        return value

    @mcp.tool()
    @_exclusive_hardware_tool
    def write_variable(name: str, value: int) -> dict:
        """Write an integer value to a named global variable (requires AXF/ELF).

        Args:
            name: Variable path (same form as read_variable).
            value: Integer value to write (enum values are written as their
                underlying integer).
        """
        dev = _connected_device()
        dev.write_variable(name, value)
        return {"name": name, "value": value}

    @mcp.tool()
    @_exclusive_hardware_tool
    def read_register(name: str) -> dict:
        """Read a memory-mapped peripheral register by name.

        Args:
            name: Register name, e.g. "SCB.CFSR", "RCC.CR", "GPIOA.MODER".
        """
        dev = _connected_device()
        val = dev.read_register(name)
        publish_mcp_superwatch(name, val)
        return {"name": name, "value": f"0x{val:08X}", "value_int": val}


def _register_debug_tools(mcp: Any) -> None:
    @mcp.tool()
    @_exclusive_hardware_tool
    def halt() -> dict:
        """Halt the target CPU (write DHCSR DBG_HALT). Use before inspecting
        registers/memory to get a consistent snapshot."""
        dev = _connected_device()
        dev.halt()
        return {"halted": True}

    @mcp.tool()
    @_exclusive_hardware_tool
    def resume() -> dict:
        """Resume target CPU execution after a halt or breakpoint hit."""
        dev = _connected_device()
        dev.resume()
        return {"resumed": True}

    @mcp.tool()
    @_exclusive_hardware_tool
    def step() -> dict:
        """Single-step one instruction on the halted target."""
        dev = _connected_device()
        dev.step()
        return {"stepped": True}

    @mcp.tool()
    @_exclusive_hardware_tool
    def set_breakpoint(address: int, slot: int | None = None) -> dict:
        """Set an FPB hardware breakpoint at ``address``.

        Args:
            address: Code address to break at.
            slot: Optional FPB comparator slot. Auto-assigned if omitted.
        """
        dev = _connected_device()
        used = dev.set_breakpoint(address, slot)
        return {"address": f"0x{address:08X}", "slot": used}

    @mcp.tool()
    @_exclusive_hardware_tool
    def clear_breakpoint(slot: int) -> dict:
        """Clear the hardware breakpoint in comparator ``slot``."""
        dev = _connected_device()
        dev.clear_breakpoint(slot)
        return {"cleared_slot": slot}

    @mcp.tool()
    @_exclusive_hardware_tool
    def clear_all_breakpoints() -> dict:
        """Clear every hardware breakpoint."""
        dev = _connected_device()
        n = dev.clear_all_breakpoints()
        return {"cleared": n}

    @mcp.tool()
    @_exclusive_hardware_tool
    def read_core_registers() -> dict[str, int]:
        """Read all Cortex-M core registers (R0–R15, xPSR, etc.) of the
        halted target. Halt first for a meaningful snapshot."""
        dev = _connected_device()
        return dev.read_core_registers()


def _register_symbol_tools(mcp: Any) -> None:
    @mcp.tool()
    @_exclusive_hardware_tool
    def load_symbols(
        axf_path: str,
        elf_backend: str | None = None,
    ) -> dict:
        """Load/refresh DWARF symbol info from an AXF/ELF file.

        Use when ``connect`` was made without ``axf`` and the user now wants
        variable access, or after rebuilding firmware to refresh symbols.

        Args:
            axf_path: Path to the .axf/.elf file.
            elf_backend: Explicit ``builtin`` or ``external`` selection.
        """
        dev = _connected_device()
        return dev.parse_axf(axf_path, elf_backend=elf_backend)

    @mcp.tool()
    def symbols_status() -> dict:
        """Report whether DWARF symbols are loaded and their counts.
        No hardware access — safe to call anytime."""
        dev = _holder["device"]
        if dev is None:
            return {"loaded": False, "hint": "no device; call connect() first"}
        return dev.axf_status

    @mcp.tool()
    @_exclusive_hardware_tool
    def memory_map() -> dict:
        """Return the firmware's memory sections (FLASH/RAM regions) parsed
        from the AXF/ELF. Requires symbols loaded (connect with axf=)."""
        dev = _connected_device()
        return dev.memory_map()


def _register_rtt_tools(mcp: Any) -> None:
    from mklink.mcp_stream_bridge import publish_mcp_rtt
    from mklink.observe_bridge import observe_operation, rtt_facts

    @mcp.tool()
    @_exclusive_hardware_tool
    def rtt_start(
        addr: str | None = None,
        channel: StrictInt = 0,
        search_size: StrictInt = 1024,
        mode: str = "auto",
    ) -> dict:
        """Start an RTT session to capture target printf/log output.

        Args:
            addr: RTT control-block address. Required when mode="static";
                otherwise resolved from .mklink/rtt_config.json.
            channel: RTT channel number, 0..2 on V4 firmware (default 0).
            search_size: Probe scan window in bytes (default 1024). Only
                used in dynamic mode.
            mode: RTT control-block storage strategy — **decision encoded
                here** (see references/rtt-static-mode.md):
                - "auto" (default): read rtt_storage_mode from
                  .mklink/rtt_config.json (0=dynamic, 1=static).
                - "dynamic": probe searches search_size bytes for the
                  _SEGGER_RTT signature. Use for stock firmware.
                - "static": CB is at a fixed address set via the
                  SEGGER_RTT_SECTION macro; pass addr explicitly.
        """
        if type(channel) is not int or not 0 <= channel < 3:
            raise ValueError(
                "channel must be between 0 and 2 for V4 probe firmware"
            )
        if type(search_size) is not int or not 0 <= search_size <= MCP_MAX_SEARCH_BYTES:
            raise ValueError(
                f"search_size must be between 0 and {MCP_MAX_SEARCH_BYTES} bytes"
            )
        mode_map = {"auto": None, "dynamic": 0, "static": 1}
        if mode not in mode_map:
            raise ValueError(
                f"mode must be one of {list(mode_map)}, got {mode!r}"
            )
        with observe_operation(
            "console.rtt.start", capability="console.rtt", action_class="observe",
        ) as observation:
            dev = _connected_device()
            result = dev.rtt_start(
                addr, channel=channel, search_size=search_size,
                mode=mode_map[mode],
            )
            observation.complete(facts=[
                {"name": "channel", "value": channel, "unit": "count"},
            ])
            return result

    @mcp.tool()
    @_exclusive_hardware_tool
    def rtt_read(duration: float = 10.0) -> dict:
        """Read output from a running RTT session for ``duration`` seconds.

        RTT must already be started (call rtt_start first). Returns text the
        target wrote to the up channel.
        """
        duration = _bounded_duration(duration)
        with observe_operation(
            "console.rtt.read", capability="console.rtt", action_class="observe",
        ) as observation:
            dev = _connected_device()
            output = dev.rtt_read(duration)
            publish_mcp_rtt(output)
            observation.complete(facts=rtt_facts(output, duration=duration))
            return {"output": output}

    @mcp.tool()
    @_exclusive_hardware_tool
    def rtt_write(data: str) -> dict:
        """Write text to the target's RTT down channel (stdin equivalent).

        Args:
            data: UTF-8 text to send (1..256 encoded bytes). This is an
                interactive command channel, not a file-transfer path.
        """
        if type(data) is not str:
            raise ValueError("data must be UTF-8 text")
        try:
            byte_length = len(data.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("data must be valid UTF-8 text") from exc
        if not 1 <= byte_length <= MCP_MAX_RTT_WRITE_BYTES:
            raise ValueError(
                f"data must contain 1..{MCP_MAX_RTT_WRITE_BYTES} UTF-8 bytes"
            )
        if "RTTView.stop()" in data:
            raise ValueError(
                "data contains the probe-reserved RTTView.stop() sequence"
            )
        with observe_operation(
            "console.rtt.write", capability="console.rtt", action_class="emit",
        ) as observation:
            dev = _connected_device()
            sent = dev.rtt_write(data)
            observation.complete(facts=[
                {
                    "name": "bytes_written",
                    "value": len(data.encode("utf-8")),
                    "unit": "bytes",
                },
                {"name": "accepted", "value": bool(sent), "ok": bool(sent)},
            ])
            return {"sent": sent}

    @mcp.tool()
    @_exclusive_hardware_tool
    def rtt_stop() -> dict:
        """Stop the RTT session and return any buffered output."""
        with observe_operation(
            "console.rtt.stop", capability="console.rtt", action_class="observe",
        ) as observation:
            dev = _connected_device()
            output = dev.rtt_stop()
            publish_mcp_rtt(output)
            observation.complete(facts=rtt_facts(output))
            return {"output": output}

    @mcp.tool()
    @_exclusive_hardware_tool
    def capture_rtt(
        duration: float = 5.0,
        pattern: StrictStr | None = None,
    ) -> dict:
        """One-shot RTT capture: auto start → read → return (session stays up).

        Use this when you want a single snapshot rather than streaming — MCP
        has no native SSE, so capture-by-time is the idiomatic pattern.

        Args:
            duration: Seconds to capture (default 5.0).
            pattern: If given, return early once this substring appears in
                the output (e.g. "System ready"). It is matched literally
                and must contain 1..256 UTF-8 bytes.
        """
        duration = _bounded_duration(duration)
        if pattern is not None:
            if type(pattern) is not str:
                raise ValueError("pattern must be UTF-8 text or None")
            try:
                byte_length = len(pattern.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("pattern must be valid UTF-8 text") from exc
            if not 1 <= byte_length <= MCP_MAX_RTT_PATTERN_BYTES:
                raise ValueError(
                    "pattern must contain 1.."
                    f"{MCP_MAX_RTT_PATTERN_BYTES} UTF-8 bytes"
                )
        with observe_operation(
            "console.rtt.capture", capability="console.rtt", action_class="observe",
        ) as observation:
            dev = _connected_device()
            out = dev.wait_for_rtt(
                pattern, timeout=duration, start_if_needed=True,
            )
            matched = pattern is not None and pattern in out
            publish_mcp_rtt(out)
            observation.complete(facts=rtt_facts(
                out, duration=duration, matched=matched,
            ))
            return {"output": out, "matched": matched}


def _register_systemview_tools(mcp: Any) -> None:
    from mklink.mcp_stream_bridge import publish_mcp_systemview
    from mklink.observe_bridge import (
        observe_operation,
        systemview_analysis_facts,
        systemview_capture_facts,
    )

    @mcp.tool()
    @_exclusive_hardware_tool
    def systemview_start(
        addr: str | None = None,
        channel: StrictInt = 1,
        search_size: StrictInt = 1024,
        mode: str = "auto",
    ) -> dict:
        """Start a SystemView RTOS-trace capture from RTT channel 1.

        The target RTOS must have SEGGER_SYSVIEW integrated (hooks writing
        trace packets into the RTT "SysView" up-buffer, channel 1 by default).
        mklink reads the raw bytes and decodes them itself — no J-Link or
        SEGGER PC tool required. To integrate SEGGER_SYSVIEW into an RT-Thread
        project, run ``systemview-integrate`` (CLI) first.

        Args:
            addr: RTT control-block address (resolved from
                .mklink/rtt_config.json when omitted, shared with RTT).
            channel: SystemView up-channel, 0..2 on V4 firmware (default 1).
            search_size: Probe scan window in bytes (default 1024).
            mode: "auto"/"dynamic"/"static" — same semantics as rtt_start.
        """
        if type(channel) is not int or not 0 <= channel < 3:
            raise ValueError(
                "channel must be between 0 and 2 for V4 probe firmware"
            )
        if type(search_size) is not int or not 0 <= search_size <= MCP_MAX_SEARCH_BYTES:
            raise ValueError(
                f"search_size must be between 0 and {MCP_MAX_SEARCH_BYTES} bytes"
            )
        mode_map = {"auto": None, "dynamic": 0, "static": 1}
        if mode not in mode_map:
            raise ValueError(
                f"mode must be one of {list(mode_map)}, got {mode!r}"
            )
        with observe_operation(
            "console.rtt.systemview.start",
            capability="console.rtt",
            action_class="observe",
        ) as observation:
            dev = _connected_device()
            result = dev.systemview_start(
                addr, channel=channel, search_size=search_size,
                mode=mode_map[mode],
            )
            observation.complete(facts=[
                {"name": "channel", "value": channel, "unit": "count"},
            ])
            return result

    @mcp.tool()
    @_exclusive_hardware_tool
    def systemview_read(duration: float = 3.0) -> dict:
        """Read & decode SystemView events for ``duration`` seconds.

        Uses a persistent decoder (accumulates absolute timestamps and
        task/ISR name maps across calls). Returns decoded RTOS events:
        task switches, ISR enter/exit, idle, timer, user events, etc.
        Call systemview_start first.
        """
        duration = _bounded_duration(duration)
        with observe_operation(
            "console.rtt.systemview.read",
            capability="console.rtt",
            action_class="observe",
        ) as observation:
            dev = _connected_device()
            result = dev.systemview_read(duration)
            publish_mcp_systemview(result)
            observation.complete(facts=systemview_capture_facts(result))
            return result

    @mcp.tool()
    @_exclusive_hardware_tool
    def systemview_stop() -> dict:
        """Stop the SystemView capture."""
        with observe_operation(
            "console.rtt.systemview.stop",
            capability="console.rtt",
            action_class="observe",
        ):
            dev = _connected_device()
            dev.systemview_stop()
            return {"status": "stopped"}

    @mcp.tool()
    @_exclusive_hardware_tool
    def capture_systemview(duration: float = 5.0) -> dict:
        """One-shot SystemView capture: start → read → stop → return events.

        Self-contained snapshot of RTOS behavior for ``duration`` seconds.
        The decoded events reveal task scheduling, ISR timing, per-task CPU
        time (from task_start_exec/task_stop_exec intervals), and kernel-object
        events — use to diagnose priority inversion, starvation, or latency.
        """
        duration = _bounded_duration(duration)
        with observe_operation(
            "console.rtt.systemview.capture",
            capability="console.rtt",
            action_class="observe",
        ) as observation:
            dev = _connected_device()
            dev.systemview_start()
            try:
                result = dev.systemview_read(duration)
            finally:
                dev.systemview_stop()
            publish_mcp_systemview(result)
            observation.complete(facts=systemview_capture_facts(result))
            return result

    @mcp.tool()
    @_exclusive_hardware_tool
    def systemview_analyze(duration: float = 5.0) -> dict:
        """Capture SystemView for ``duration`` seconds and analyze the RTOS state.

        Returns a structured RTOS runtime report (an AI agent / skill can interpret
        it): per-task CPU%, switch counts & slice stats, ISR count/timing, idle %,
        context-switch rate, and anomaly flags (CPU starvation, excessive switching,
        ISR too heavy/long, near-capacity). Analysis methodology follows the SEGGER
        SystemView User Guide (UM08027): per-task/ISR time, ISR latency, scheduling.
        """
        from mklink.systemview_analyzer import analyze_events
        duration = _bounded_duration(duration)
        with observe_operation(
            "console.rtt.systemview.analyze",
            capability="console.rtt",
            action_class="observe",
        ) as observation:
            dev = _connected_device()
            dev.systemview_start()
            try:
                result = dev.systemview_read(duration)
            finally:
                dev.systemview_stop()
            publish_mcp_systemview(result)
            report = analyze_events(result.get("events", []))
            report["capture"] = {
                "event_count": result.get("event_count", 0),
                "synced": result.get("synced"),
                "dropped": result.get("dropped_bytes", 0) + result.get("dropped_packets", 0),
                "cpu_freq": result.get("cpu_freq"),
            }
            observation.complete(facts=systemview_analysis_facts(report))
            return report

    @mcp.tool()
    def systemview_analyze_events(events: list) -> dict:
        """Analyze an already-decoded SystemView event list (offline, no device).

        Args:
            events: list of decoded event dicts (as returned by systemview_read or
                systemview_decode ``events``). Useful for the AI to analyze a
                previously captured trace without re-capturing.
        """
        from mklink.systemview_analyzer import analyze_events
        return analyze_events(events)

    @mcp.tool()
    @_exclusive_hardware_tool
    def systemview_report(
        duration: float = 5.0,
        out_path: str = "systemview_report.html",
    ) -> dict:
        """Capture SystemView and generate a self-contained HTML analysis report.

        Produces a shareable HTML file (CPU% bars per task, task table, ISR stats,
        anomalies, and a task-switch Gantt timeline) — open in any browser. The
        report is written to ``out_path`` and the path is returned.
        """
        from mklink.systemview_analyzer import analyze_events
        from mklink.systemview_report import generate_html_report
        from pathlib import Path
        duration = _bounded_duration(duration)
        with observe_operation(
            "console.rtt.systemview.report",
            capability="console.rtt",
            action_class="observe",
        ) as observation:
            dev = _connected_device()
            dev.systemview_start()
            try:
                result = dev.systemview_read(duration)
            finally:
                dev.systemview_stop()
            events = result.get("events", [])
            # 任务名解析
            ids = list({e["task_id"] for e in events if "task_id" in e})
            if ids:
                try:
                    names = dev.systemview_resolve_task_names(ids)
                    for e in events:
                        if e.get("task_id") in names:
                            e["task_name"] = names[e["task_id"]]
                except Exception:
                    pass
            publish_mcp_systemview(result)
            report = analyze_events(events)
            html_str = generate_html_report(report, events, meta={"cpu_freq": result.get("cpu_freq")})
            out = Path(out_path).resolve()
            out.write_text(html_str, encoding="utf-8")
            response = {"path": str(out), "events": len(events),
                        "tasks": report["summary"].get("task_count", 0),
                        "anomalies": len(report.get("anomalies", []))}
            observation.complete(facts=[
                {"name": "event_count", "value": response["events"], "unit": "count"},
                {"name": "task_count", "value": response["tasks"], "unit": "count"},
                {"name": "anomaly_count", "value": response["anomalies"], "unit": "count"},
            ])
            return response

    @mcp.tool()
    @_exclusive_hardware_tool
    def systemview_integrate(
        project_root: str,
        sv_dir: str = "segger_systemview",
    ) -> dict:
        """Integrate SEGGER SystemView into an RT-Thread project (RTOS tracing).

        Bundles the SEGGER_SYSVIEW core + RT-Thread adaptation into the project,
        registers the sources in the Keil .uvprojx, adds the USE_SYSTEMVIEW macro,
        and injects the header into main.c. RT-Thread then auto-initializes
        SystemView on boot and streams trace events to RTT channel 1 — no manual
        init code needed. Requires RTT already integrated (run rtt-integrate first
        if not). Failing steps roll back automatically.

        Args:
            project_root: Target project root (must contain a .uvprojx and
                applications/main.c or src/main.c).
            sv_dir: Directory inside the project to hold the SystemView sources
                (default "segger_systemview").
        """
        from mklink.systemview_integration import (
            check_systemview_sources_bundled, full_systemview_integrate,
        )
        if not check_systemview_sources_bundled():
            return {"success": False,
                    "errors": ["技能目录中缺少 SystemView 源文件 (systemview_sources/)"]}
        return full_systemview_integrate(project_root, sv_dir=sv_dir)

    @mcp.tool()
    def systemview_decode(hex_bytes: str) -> dict:
        """Decode raw SystemView bytes (hex string) offline — no device needed.

        Useful for validating the decoder or replaying a captured RTT channel-1
        dump without hardware. Feed the hex of the raw bytes captured from the
        "SysView" up-buffer.

        Args:
            hex_bytes: Hex-encoded raw SystemView byte stream
                (e.g. "00000000000000000000180b..." ).
        """
        from mklink.systemview_parser import SystemViewParser
        try:
            raw = bytes.fromhex(hex_bytes)
        except ValueError as e:
            raise ValueError(f"invalid hex string: {e}") from e
        p = SystemViewParser()
        events = p.feed(raw)
        return {
            "events": events,
            "event_count": len(events),
            "bytes_read": len(raw),
            "synced": p.synced,
            "abs_time": p.abs_time,
            "cpu_freq": p.cpu_freq,
            "dropped_bytes": p.dropped_bytes,
            "dropped_packets": p.dropped_packets,
        }


def _register_hardfault_tools(mcp: Any) -> None:
    @mcp.tool()
    @_exclusive_hardware_tool
    def check_hardfault() -> dict:
        """Read SCB.CFSR / SCB.HFSR. Non-zero means a HardFault occurred.

        Cheap pre-check before decode_hardfault. Returns {"fault": False}
        when no fault registers are set.
        """
        dev = _connected_device()
        regs = dev.check_hardfault()
        if not regs:
            return {"fault": False}
        return {
            "fault": True,
            "SCB.CFSR": f"0x{regs['SCB.CFSR']:08X}",
            "SCB.HFSR": f"0x{regs['SCB.HFSR']:08X}",
        }

    @mcp.tool()
    @_exclusive_hardware_tool
    def decode_hardfault() -> dict:
        """Decode a HardFault into a structured report with source locations.

        Auto-reads CFSR/HFSR, expands them to human-readable flag names
        (e.g. "Imprecise data access violation"), and — if an AXF/ELF is
        loaded — walks the exception stack frame and resolves PC/LR through
        the selected ELF backend to pinpoint the source line. This encodes the full
        HardFault debugging playbook (references/commands-memory.md).
        """
        dev = _connected_device()
        rep = dev.decode_hardfault()
        if rep is None:
            return {"fault": False, "summary": "No fault registers set"}
        return {
            "fault": True,
            "cfsr": f"0x{rep.cfsr:08X}",
            "hfsr": f"0x{rep.hfsr:08X}",
            "cfsr_flags": rep.cfsr_flags,
            "hfsr_flags": rep.hfsr_flags,
            "stack_frame": rep.stack_frame,
            "source_locations": rep.source_locations,
            "summary": rep.summary,
            "fault_function": rep.fault_function,
            "fault_location": rep.fault_location,
            "exception_stack": rep.exception_stack,
            "call_stack": rep.call_stack,
            "core_registers": rep.core_registers,
        }


# ==========================================================================
# Phase 3: flush_memory (auto-chunked) + Modbus RTU + generic serial.
# These reach Device-blind subsystems directly (mklink.modbus / mklink.serial)
# without polluting the Device facade. Modbus/serial are INDEPENDENT serial
# ports (separate cross-process locks), NOT the MKLink SWD probe.
# ==========================================================================

# ---- flush_memory helpers (encode flush-memory.md boundary + PIKA_LINE_BUFF) ----
_FLUSH_CMD_MAX = 230          # cli.py:1314 — PIKA_LINE_BUFF safe bound
_FLUSH_NONREPEAT_CHUNK = 30   # ~180 chars expanded, headroom under 230


def _flush_data_expr(data: bytes) -> tuple[str, bool]:
    """Build the PikaScript data expression for one flush tuple.

    All-same-byte payloads use the short ``bytes([0xVV])*N`` form (carries up
    to 12 KiB in one command); anything else expands to a literal (caller
    pre-splits these into ≤30B chunks). Returns (expression, is_short_form).
    """
    if data and all(b == data[0] for b in data):
        return f"bytes([0x{data[0]:02X}])*{len(data)}", True
    literal = ", ".join(f"0x{b:02X}" for b in data)
    return f"bytes([{literal}])", False


def _plan_flush_batches(
    writes: list[tuple[int, bytes]],
) -> list[list[tuple[int, bytes]]]:
    """Split (addr, data) writes into batches whose command string stays
    under _FLUSH_CMD_MAX. Non-repeat payloads >30B are pre-split into 30B
    chunks; batches then greedily packed (≤8 items, ≤230 chars). Encodes the
    chunking strategy from references/flush-memory.md §5.
    """
    from mklink.remote.stream_protocol import canonical_memory_address

    items: list[tuple[int, bytes]] = []
    for addr, data in writes:
        if not data:
            continue
        _, is_short = _flush_data_expr(data)
        if is_short:
            items.append((addr, data))
        else:
            for off in range(0, len(data), _FLUSH_NONREPEAT_CHUNK):
                items.append((addr + off, data[off:off + _FLUSH_NONREPEAT_CHUNK]))

    batches: list[list[tuple[int, bytes]]] = []
    cur: list[tuple[int, bytes]] = []
    cur_len = len("cmd.flush_memory([])")
    for addr, data in items:
        tup = f"({canonical_memory_address(addr)}, {_flush_data_expr(data)[0]})"
        add = len(tup) + (2 if cur else 0)
        if cur and (cur_len + add > _FLUSH_CMD_MAX or len(cur) >= 8):
            batches.append(cur)
            cur = []
            cur_len = len("cmd.flush_memory([])")
            add = len(tup)
        cur.append((addr, data))
        cur_len += add
    if cur:
        batches.append(cur)
    return batches


def _register_flush_tools(mcp: Any) -> None:
    from mklink.observe_bridge import memory_flush_facts, observe_operation
    from mklink.remote.stream_protocol import canonical_memory_address

    @mcp.tool()
    @_exclusive_hardware_tool
    def flush_memory(writes: list[dict]) -> dict:
        """Write multiple discontiguous RAM regions silently via cmd.flush_memory.

        **Value-add over the CLI: auto-chunks.** The CLI rejects any single
        command over 230 chars (PIKA_LINE_BUFF overflow → REPL deadlock);
        this tool splits automatically:
          - all-same-byte payloads (zero-fill, 0xFF fill) → short expression;
          - non-repeat data → 30-byte chunks, ≤8 addresses/batch, ≤230 chars;
          - sends batch-by-batch, waiting for the device prompt between each.

        Host safety limits are enforced before device lookup or I/O: at most
        8 input regions, at most 12288 bytes in any one region, and at most
        12288 bytes total per tool call. Split larger writes into sequential
        calls and wait for each call to finish before sending the next.

        The command is silent, but it must not run concurrently with any
        dump/RTT/SystemView stream on the same probe. Stop and release the
        stream first, then issue the write in a normal command session.

        Args:
            writes: List of {"address": int, "data_hex": str}, e.g.
                [{"address": 0x20002000, "data_hex": "DEADBEEF"}].
        """
        from mklink.cli import _parse_flush_response
        if not isinstance(writes, list) or not writes:
            raise ValueError("writes must be a non-empty list")
        if len(writes) > MCP_MAX_FLUSH_WRITES:
            raise ValueError(
                f"writes must contain at most {MCP_MAX_FLUSH_WRITES} regions"
            )
        with observe_operation(
            "memory.flush",
            capability="target.memory",
            action_class="emit",
        ) as observation:
            parsed: list[tuple[int, bytes]] = []
            for i, w in enumerate(writes):
                if not isinstance(w, dict) or "address" not in w or "data_hex" not in w:
                    raise ValueError(f"writes[{i}] must contain address and data_hex")
                try:
                    if isinstance(w["address"], bool):
                        raise ValueError
                    address = int(w["address"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"writes[{i}].address must be an integer") from exc
                data_hex = w["data_hex"]
                if not isinstance(data_hex, str):
                    raise ValueError(f"writes[{i}].data_hex must be a hex string")
                try:
                    data = _from_hex(data_hex)
                except ValueError as exc:
                    raise ValueError(
                        f"writes[{i}].data_hex must be valid hex: {exc}"
                    ) from exc
                _validate_memory_range(
                    address, len(data), max_size=MCP_MAX_FLUSH_ITEM_BYTES
                )
                parsed.append((address, data))
            total = sum(len(data) for _, data in parsed)
            if total > MCP_MAX_FLUSH_TOTAL_BYTES:
                raise ValueError(
                    "total write data must not exceed "
                    f"{MCP_MAX_FLUSH_TOTAL_BYTES} bytes"
                )
            dev = _connected_device()
            batches = _plan_flush_batches(parsed)
            results = []
            try:
                for bi, batch in enumerate(batches):
                    tuple_strs = [
                        f"({canonical_memory_address(a)}, {_flush_data_expr(d)[0]})"
                        for a, d in batch
                    ]
                    cmd = f"cmd.flush_memory([{', '.join(tuple_strs)}])"
                    resp = dev._bridge.send_command(cmd, timeout=10.0)
                    ok, msg = _parse_flush_response(resp)
                    results.append({
                        "batch": bi + 1, "items": len(batch),
                        "bytes": sum(len(d) for _, d in batch),
                        "ok": ok, "message": msg,
                    })
            except Exception:
                successful_batches = sum(result["ok"] for result in results)
                facts = memory_flush_facts(
                    canonical_memory_address(parsed[0][0]) if parsed else None,
                    total_bytes=total,
                    region_count=len(parsed),
                    batch_count=len(batches),
                    successful_batches=successful_batches,
                    failed_batches=len(batches) - successful_batches,
                )
                observation.fail("memory_flush_transport_failed", facts=facts)
                raise
            response = {
                "ok": all(r["ok"] for r in results),
                "batches": len(batches),
                "total_bytes": total,
                "results": results,
            }
            failed_batches = sum(not result["ok"] for result in results)
            facts = memory_flush_facts(
                canonical_memory_address(parsed[0][0]) if parsed else None,
                total_bytes=total,
                region_count=len(parsed),
                batch_count=len(batches),
                successful_batches=len(batches) - failed_batches,
                failed_batches=failed_batches,
            )
            if response["ok"]:
                observation.complete(facts=facts)
            else:
                observation.fail("memory_flush_failed", facts=facts)
            return response


# ---- Modbus RTU (independent serial port session) ----
_modbus_lock = threading.Lock()
_modbus_holder: dict[str, Any] = {"client": None}


def _get_modbus() -> Any:
    c = _modbus_holder["client"]
    if c is None or not getattr(c, "_is_open", False):
        from mklink.modbus._client import ModbusError
        raise ModbusError("No Modbus session. Call modbus_open first.")
    return c


def _register_modbus_tools(mcp: Any) -> None:
    @mcp.tool()
    def modbus_open(
        port: str,
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 1.0,
        retries: int = 3,
    ) -> dict:
        """Open a Modbus RTU serial session on ``port``.

        Independent of the MKLink probe — use for a USB-RS485 adapter or any
        Modbus RTU device. Holds a cross-process lock on this port.
        """
        from mklink.modbus._client import ModbusClient
        with _modbus_lock:
            old = _modbus_holder["client"]
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
            c = ModbusClient(
                port=port, baudrate=baudrate, parity=parity,
                stopbits=stopbits, timeout=timeout, retries=retries,
            )
            ok = c.open()
            _modbus_holder["client"] = c if ok else None
        return {"open": ok, "port": port, "baudrate": baudrate}

    @mcp.tool()
    def modbus_close() -> dict:
        """Close the Modbus session and release the port lock."""
        with _modbus_lock:
            c = _modbus_holder["client"]
            if c is None:
                return {"closed": True, "was_open": False}
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            _modbus_holder["client"] = None
        return {"closed": True, "was_open": True}

    @mcp.tool()
    def modbus_read(
        address: int,
        count: int,
        slave: int,
        function: int = 3,
    ) -> dict:
        """Read Modbus data from a slave.

        Args:
            address: Register/coil start address.
            count: Number of registers/coils to read.
            slave: Slave/unit ID (1..247).
            function: 1=read coils, 2=read discrete inputs, 3=read holding
                registers (default), 4=read input registers.
        """
        c = _get_modbus()
        if function == 1:
            data = c.read_coils(address, count, slave=slave)
        elif function == 2:
            data = c.read_discrete_inputs(address, count, slave=slave)
        elif function == 3:
            data = c.read_holding_registers(address, count, slave=slave)
        elif function == 4:
            data = c.read_input_registers(address, count, slave=slave)
        else:
            raise ValueError("function must be 1/2/3/4 for reads")
        return {
            "function": function, "slave": slave,
            "address": address, "values": list(data),
        }

    @mcp.tool()
    def modbus_write(
        address: int,
        slave: int,
        values: list[int],
        function: int = 6,
    ) -> dict:
        """Write Modbus data to a slave.

        Args:
            address: Register/coil address.
            slave: Slave/unit ID.
            values: Values to write. For coils (FC5/15) use 0/1 integers.
            function: 5=write single coil, 6=write single register (default),
                15=write multiple coils, 16=write multiple registers.
        """
        if function not in (5, 6, 15, 16):
            raise ValueError("function must be 5/6/15/16 for writes")
        if not values:
            raise ValueError("values must contain at least one item")
        c = _get_modbus()
        if function == 5:
            c.write_coil(address, bool(values[0]), slave=slave)
        elif function == 6:
            c.write_register(address, int(values[0]), slave=slave)
        elif function == 15:
            c.write_coils(address, [bool(v) for v in values], slave=slave)
        elif function == 16:
            c.write_registers(address, [int(v) for v in values], slave=slave)
        return {
            "function": function, "slave": slave,
            "address": address, "written": len(values),
        }

    @mcp.tool()
    def modbus_scan(
        start_addr: int = 1,
        end_addr: int = 247,
        probe_register: int = 0,
    ) -> dict:
        """Scan for responsive Modbus slave IDs via FC03 probe.

        Internally uses a short timeout (0.15s) and 0 retries, so a full
        1..247 sweep takes ~40s. A slave counts as present if it responds at
        all — including with a Modbus exception code (illegal address etc.).

        Requires an open session (modbus_open).
        """
        from mklink.modbus._scanner import scan_slaves
        c = _get_modbus()
        found = scan_slaves(
            c, start_addr=start_addr, end_addr=end_addr,
            probe_register=probe_register,
        )
        return {"found_slaves": found, "count": len(found)}


# ---- Generic serial port (independent session) ----
_serial_lock = threading.Lock()
_serial_holder: dict[str, Any] = {"port": None}


def _get_serial() -> Any:
    s = _serial_holder["port"]
    if s is None or not getattr(s, "is_open", False):
        raise RuntimeError("No serial session. Call serial_open first.")
    return s


def _register_serial_tools(mcp: Any) -> None:
    @mcp.tool()
    def serial_list() -> list[dict]:
        """List available serial ports EXCLUDING MKLink debug probes.

        Use for general UART targets (device console, USB-RS485, GNSS, etc.).
        MKLink probes are listed via ``discover_probes`` instead.
        """
        from mklink.serial._port import list_uart_ports
        return list_uart_ports()

    @mcp.tool()
    def serial_open(
        port: str,
        baudrate: int = 115200,
        databits: int = 8,
        stopbits: int = 1,
        parity: str = "N",
    ) -> dict:
        """Open a generic serial port session (independent of the MKLink probe).

        Holds a cross-process lock on this port. Default 115200 8N1.
        """
        from mklink.serial._port import SerialPort
        with _serial_lock:
            old = _serial_holder["port"]
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
            sp = SerialPort(
                port=port, baudrate=baudrate, databits=databits,
                stopbits=stopbits, parity=parity,
            )
            ok = sp.open()
            _serial_holder["port"] = sp if ok else None
        return {"open": ok, "port": port, "baudrate": baudrate}

    @mcp.tool()
    def serial_close() -> dict:
        """Close the serial session and release the port lock."""
        with _serial_lock:
            s = _serial_holder["port"]
            if s is None:
                return {"closed": True, "was_open": False}
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
            _serial_holder["port"] = None
        return {"closed": True, "was_open": True}

    @mcp.tool()
    def serial_send(data_hex: str) -> dict:
        """Write raw bytes to the open serial port.

        Args:
            data_hex: Hex string of bytes to send, e.g. "AABBCC" or "aa bb cc".
        """
        s = _get_serial()
        data = _from_hex(data_hex)
        s.write(data)
        return {"bytes_sent": len(data)}

    @mcp.tool()
    def serial_read(duration: float = 1.0) -> dict:
        """Read from the open serial port for ``duration`` seconds.

        Accumulates all bytes received in the window via a non-blocking poll
        loop. Use serial_list first, serial_open, then serial_send/serial_read.

        Args:
            duration: Seconds to capture (default 1.0).
        """
        import time
        duration = _bounded_duration(duration)
        s = _get_serial()
        deadline = time.time() + duration
        buf = bytearray()
        while time.time() < deadline:
            buf.extend(s.read_available())
            time.sleep(0.02)
        return {"hex": bytes(buf).hex(), "bytes_read": len(buf)}


# ==========================================================================
# Server factory
# ==========================================================================
def build_server() -> Any:
    """Construct and return the FastMCP server with all tools registered.

    Raises:
        ImportError: if ``fastmcp`` is not installed (hint: ``pip install
            -e ".[mcp]"``).
    """
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            'fastmcp not installed. Run: pip install -e ".[mcp]"'
        ) from exc

    mcp = FastMCP("mklink")

    _register_health_tools(mcp)
    _register_project_tools(mcp)
    _register_connection_tools(mcp)
    _register_flash_tools(mcp)
    _register_memory_tools(mcp)
    _register_variable_tools(mcp)
    _register_debug_tools(mcp)
    _register_symbol_tools(mcp)
    _register_rtt_tools(mcp)
    _register_systemview_tools(mcp)
    _register_hardfault_tools(mcp)
    _register_flush_tools(mcp)
    _register_modbus_tools(mcp)
    _register_serial_tools(mcp)

    # Phase 4: symbol search/typeinfo, SKILL.md methodology realignment,
    # and test_mcp_server.py unit tests.

    return mcp


# Module-level server instance for direct invocation.
mcp: Any = None


def run() -> None:
    """Entry point for the ``mklink mcp`` CLI subcommand.

    Uses stdio transport. MUST NOT print to stdout — that stream carries the
    JSON-RPC protocol. Diagnostic output goes to stderr via ``logging``.
    """
    global mcp
    if mcp is None:
        mcp = build_server()
    stop_mcp_stream_sidecar = None
    try:
        from mklink.mcp_stream_bridge import (
            start_mcp_stream_sidecar,
            stop_mcp_stream_sidecar,
        )

        start_mcp_stream_sidecar(wait_timeout=0.75)
    except Exception:
        # Observation is optional and must never prevent the MCP owner from
        # serving device tools. Avoid logging on the stdio protocol channel.
        pass
    with _isolate_stdio_protocol():
        try:
            mcp.run(transport="stdio")
        finally:
            try:
                _reset_device()
            finally:
                if stop_mcp_stream_sidecar is not None:
                    try:
                        stop_mcp_stream_sidecar(timeout=0.5)
                    except Exception:
                        pass
                try:
                    from mklink.observe_bridge import shutdown_process_observation

                    shutdown_process_observation(timeout=0.5)
                except Exception:
                    pass


__all__ = ["build_server", "run", "configure_device"]
