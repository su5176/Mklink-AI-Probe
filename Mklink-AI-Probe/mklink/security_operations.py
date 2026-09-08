"""Shared one-shot reversible security operations for CLI, MCP, and WebGUI."""

from __future__ import annotations

from typing import Optional


_VOLTAGES_MV = frozenset({1800, 3300, 5000})


def run_security_operation(
    action: str,
    target_part: str,
    *,
    voltage_mv: int,
    confirm_user: bool,
    confirm_data_loss: bool = False,
    firmware: Optional[str] = None,
    base_address: Optional[int] = None,
    probe_id: Optional[str] = None,
    frequency: int = 1_000_000,
    timeout: float = 240.0,
) -> dict[str, object]:
    """Run one validated RDP1 lock/unlock through the online-flash backend.

    This is the common non-UI entry point used by the CLI and MCP. The WebGUI
    uses the same capability resolver, job configuration, backend, and reset
    path through ``_start_job_with_configuration``.
    """

    normalized_action = str(action or "").strip().casefold()
    if normalized_action not in {"lock", "unlock"}:
        raise ValueError("action must be lock or unlock")
    part = str(target_part or "").strip()
    if not part:
        raise ValueError("target_part is required")
    if isinstance(voltage_mv, bool) or voltage_mv not in _VOLTAGES_MV:
        raise ValueError("voltage_mv must be 1800, 3300, or 5000")
    if confirm_user is not True:
        raise ValueError(
            "security operation requires explicit confirmation for the exact restore voltage"
        )
    if normalized_action == "unlock" and confirm_data_loss is not True:
        raise ValueError(
            "unlock requires explicit confirmation that protected nonvolatile data will be erased"
        )
    if normalized_action == "lock" and not str(firmware or "").strip():
        raise ValueError("lock requires a firmware image to verify immediately before protection")
    if (
        isinstance(frequency, bool)
        or not isinstance(frequency, int)
        or frequency < 1
        or frequency > 10_000_000
    ):
        raise ValueError("frequency must be between 1 and 10000000 Hz")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")

    from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
    from mklink.cmsis_dap.models import JobState
    from mklink.remote.online_flash_api import (
        JobBody,
        _enumerate_probes,
        _resolved_target,
        _start_job_with_configuration,
        _target_flash_configuration,
        create_default_online_flash_services,
        shutdown_online_flash_services,
    )
    from mklink.remote.resource_manager import ResourceManager

    services = create_default_online_flash_services(ResourceManager())
    try:
        target = _resolved_target(services.catalog, part)
        from mklink.cmsis_dap.security import require_security_capability

        security = require_security_capability(target.part_number)
        probes = _enumerate_probes(services.probe_provider)
        if probe_id:
            selected = [probe for probe in probes if probe.unique_id == probe_id]
        else:
            selected = probes
        if len(selected) != 1:
            message = (
                "the requested MKLink probe was not found"
                if probe_id
                else "exactly one MKLink probe must be connected"
            )
            raise FlashError(FlashErrorCode.MKLINK_DAP_NOT_FOUND, message)

        inspection = None
        if normalized_action == "lock":
            regions, fingerprint, _configured_paths = _target_flash_configuration(
                services, target.part_number
            )
            inspection = services.image_inspector.inspect(
                str(firmware), regions, base_address=base_address
            )
            services.image_targets[inspection.image_id] = (
                target.part_number.casefold(), fingerprint
            )

        body = JobBody(
            actions=(
                ["connect", "verify", "lock", "reset", "disconnect"]
                if normalized_action == "lock"
                else ["connect", "unlock", "reset", "disconnect"]
            ),
            image_id=inspection.image_id if inspection is not None else None,
            base_address=base_address,
            preempt_ai=True,
            probe_id=selected[0].unique_id,
            target_part=target.part_number,
            frequency=frequency,
            connect_mode=(
                "under-reset"
                if normalized_action == "unlock"
                and security.family not in {"py32f030x8-rdp1", "gd32f303xe-spc"}
                else "halt"
            ),
            reset_mode="power-cycle",
            reset_voltage_mv=voltage_mv,
        )
        job_id, _snapshot = _start_job_with_configuration(services, body, target)
        snapshot = services.job_manager.wait(job_id, timeout=float(timeout))
        messages = tuple(
            str(event.message)
            for event in services.job_manager.events(job_id)
            if event.event == "log" and event.message
        )
        if snapshot.state is not JobState.SUCCEEDED:
            try:
                code = FlashErrorCode(snapshot.error_code or "UNKNOWN_ERROR")
            except ValueError:
                code = FlashErrorCode.UNKNOWN_ERROR
            raise FlashError(code, snapshot.error_message or "security operation failed")
        return {
            "status": "succeeded",
            "action": normalized_action,
            "target_part": target.part_number,
            "voltage_mv": voltage_mv,
            "connect_mode": body.connect_mode,
            "reset_mode": body.reset_mode,
            "messages": messages,
            "verified_sha256": (
                inspection.sha256 if inspection is not None else None
            ),
        }
    finally:
        shutdown_online_flash_services(services)
