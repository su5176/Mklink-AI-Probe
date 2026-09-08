from dataclasses import is_dataclass

import pytest

from mklink.cmsis_dap.errors import FLASH_ERROR_TITLES, FlashError, FlashErrorCode
from mklink.cmsis_dap.models import (
    ALLOWED_TRANSITIONS,
    ImageInspection,
    ImageSegment,
    JobEvent,
    JobRequest,
    JobSnapshot,
    JobState,
    MemoryRegion,
    PackRecord,
    ProbeRecord,
    TargetRecord,
    assert_transition,
)


def test_flash_error_serializes_stable_code():
    error = FlashError(FlashErrorCode.PACK_DOWNLOAD_FAIL, "network timeout")
    assert error.to_dict() == {
        "code": "PACK_DOWNLOAD_FAIL",
        "title": "Pack 下载失败",
        "message": "network timeout",
    }


def test_all_stable_flash_error_codes_exist():
    assert {code.value for code in FlashErrorCode} == {
        "MKLINK_DAP_NOT_FOUND",
        "PROBE_BUSY",
        "TARGET_NOT_SUPPORTED",
        "PACK_INDEX_UNAVAILABLE",
        "PACK_NOT_FOUND",
        "PACK_DOWNLOAD_FAIL",
        "PACK_INTEGRITY_ERROR",
        "CONNECT_FAIL",
        "FILE_NOT_FOUND",
        "FILE_FORMAT_ERROR",
        "BIN_ADDRESS_MISSING",
        "IMAGE_OUT_OF_RANGE",
        "TARGET_LOCKED",
        "SECURITY_NOT_SUPPORTED",
        "UNLOCK_FAIL",
        "LOCK_FAIL",
        "ERASE_FAIL",
        "PROGRAM_FAIL",
        "VERIFY_FAIL",
        "RESET_FAIL",
        "USER_ABORT",
        "UNKNOWN_ERROR",
    }


def test_all_flash_error_codes_have_nonempty_titles():
    assert all(FLASH_ERROR_TITLES[code].strip() for code in FlashErrorCode)


def test_flash_error_titles_are_read_only():
    with pytest.raises(TypeError):
        FLASH_ERROR_TITLES[FlashErrorCode.UNKNOWN_ERROR] = "changed"


def test_domain_records_are_frozen_dataclasses():
    records = (
        ProbeRecord,
        TargetRecord,
        PackRecord,
        MemoryRegion,
        ImageSegment,
        ImageInspection,
        JobRequest,
        JobEvent,
        JobSnapshot,
    )
    assert all(is_dataclass(record) for record in records)
    assert all(record.__dataclass_params__.frozen for record in records)


def test_full_sequence_has_stable_action_order():
    image = ImageInspection(image_id="image-1")
    request = JobRequest.full_sequence(image)
    assert request.image_id == "image-1"
    assert request.actions == (
        "connect",
        "erase",
        "program",
        "verify",
        "reset",
        "disconnect",
    )


def test_full_sequence_enables_ai_preemption_by_default():
    image = ImageInspection(image_id="image-1")
    request = JobRequest.full_sequence(image)
    assert request.preempt_ai is True


def test_full_sequence_can_disable_ai_preemption():
    image = ImageInspection(image_id="image-1")
    request = JobRequest.full_sequence(image, preempt_ai=False)
    assert request.preempt_ai is False


@pytest.mark.parametrize(
    "factory",
    (JobRequest.full_sequence, JobRequest.program_only),
)
def test_job_request_factory_propagates_bin_base_address(factory):
    image = ImageInspection(
        image_id="bin-1",
        format="bin",
        base_address=0x08000000,
    )
    request = factory(image)
    assert request.base_address == 0x08000000


def test_program_only_keeps_connection_boundaries():
    image = ImageInspection(image_id="image-1")
    request = JobRequest.program_only(image)
    assert request.image_id == "image-1"
    assert request.actions == ("connect", "program", "disconnect")
    assert request.preempt_ai is True


def test_program_only_can_disable_ai_preemption():
    image = ImageInspection(image_id="image-1")
    request = JobRequest.program_only(image, preempt_ai=False)
    assert request.preempt_ai is False


def test_job_state_allows_forward_transition():
    assert_transition(JobState.CONNECTING, JobState.PROGRAMMING)


def test_allowed_transitions_are_read_only():
    assert all(isinstance(next_states, frozenset) for next_states in ALLOWED_TRANSITIONS.values())
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[JobState.QUEUED] = frozenset()


def test_transition_matrix_matches_allowed_transitions():
    for current in JobState:
        for next_state in JobState:
            if next_state in ALLOWED_TRANSITIONS[current]:
                assert_transition(current, next_state)
            else:
                with pytest.raises(ValueError):
                    assert_transition(current, next_state)


def test_job_state_rejects_backward_transition():
    with pytest.raises(ValueError, match="programming -> connecting"):
        assert_transition(JobState.PROGRAMMING, JobState.CONNECTING)


def test_job_snapshot_has_recoverable_primitive_defaults():
    snapshot = JobSnapshot(
        job_id="job-1",
        state=JobState.QUEUED,
        actions=("connect", "disconnect"),
        image_id="image-1",
        created_at=1.0,
        updated_at=1.0,
    )
    assert {
        "probe_id": snapshot.probe_id,
        "target_part": snapshot.target_part,
        "frequency": snapshot.frequency,
        "connect_mode": snapshot.connect_mode,
        "reset_mode": snapshot.reset_mode,
        "reset_voltage_mv": snapshot.reset_voltage_mv,
        "file_path": snapshot.file_path,
        "image_format": snapshot.image_format,
        "image_start": snapshot.image_start,
        "image_end": snapshot.image_end,
        "image_size": snapshot.image_size,
        "image_sha256": snapshot.image_sha256,
        "current_action": snapshot.current_action,
        "stage_progress": snapshot.stage_progress,
        "total_progress": snapshot.total_progress,
        "speed_bytes_per_second": snapshot.speed_bytes_per_second,
        "elapsed_seconds": snapshot.elapsed_seconds,
        "error_code": snapshot.error_code,
        "error_message": snapshot.error_message,
    } == {
        "probe_id": None,
        "target_part": None,
        "frequency": 1_000_000,
        "connect_mode": "halt",
        "reset_mode": "default",
        "reset_voltage_mv": None,
        "file_path": None,
        "image_format": None,
        "image_start": None,
        "image_end": None,
        "image_size": None,
        "image_sha256": None,
        "current_action": None,
        "stage_progress": 0.0,
        "total_progress": 0.0,
        "speed_bytes_per_second": 0.0,
        "elapsed_seconds": 0.0,
        "error_code": None,
        "error_message": None,
    }
    assert not hasattr(snapshot, "progress")
