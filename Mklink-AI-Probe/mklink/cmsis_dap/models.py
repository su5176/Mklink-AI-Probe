"""Immutable, backend-independent contracts for online flash operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import FrozenSet, Mapping, Optional, Tuple


class JobState(str, Enum):
    QUEUED = "queued"
    CONNECTING = "connecting"
    ERASING = "erasing"
    PROGRAMMING = "programming"
    VERIFYING = "verifying"
    RESETTING = "resetting"
    DISCONNECTING = "disconnecting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ALLOWED_TRANSITIONS: Mapping[JobState, FrozenSet[JobState]] = MappingProxyType({
    JobState.QUEUED: frozenset({JobState.CONNECTING, JobState.FAILED, JobState.STOPPED}),
    JobState.CONNECTING: frozenset({
        JobState.ERASING,
        JobState.PROGRAMMING,
        JobState.VERIFYING,
        JobState.RESETTING,
        JobState.DISCONNECTING,
        JobState.FAILED,
        JobState.STOPPING,
    }),
    JobState.ERASING: frozenset({
        JobState.PROGRAMMING,
        JobState.RESETTING,
        JobState.DISCONNECTING,
        JobState.FAILED,
        JobState.STOPPING,
    }),
    JobState.PROGRAMMING: frozenset({
        JobState.VERIFYING,
        JobState.RESETTING,
        JobState.DISCONNECTING,
        JobState.FAILED,
        JobState.STOPPING,
    }),
    JobState.VERIFYING: frozenset({
        JobState.RESETTING,
        JobState.DISCONNECTING,
        JobState.FAILED,
        JobState.STOPPING,
    }),
    JobState.RESETTING: frozenset({
        JobState.DISCONNECTING,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.STOPPING,
    }),
    JobState.DISCONNECTING: frozenset({
        JobState.SUCCEEDED,
        JobState.STOPPED,
        JobState.FAILED,
    }),
    JobState.STOPPING: frozenset({
        JobState.DISCONNECTING,
        JobState.STOPPED,
        JobState.FAILED,
    }),
    JobState.STOPPED: frozenset(),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
})


def assert_transition(current: JobState, next_state: JobState) -> None:
    if next_state not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid job state transition: {current.value} -> {next_state.value}")


@dataclass(frozen=True)
class ProbeRecord:
    unique_id: str
    vendor_name: str = ""
    product_name: str = ""
    description: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: Optional[str] = None


@dataclass(frozen=True)
class TargetRecord:
    part_number: str
    vendor: str
    pack_id: Optional[str] = None
    pack_version: Optional[str] = None
    pack_path: Optional[str] = None
    installed: bool = False
    source: str = "index"
    family: str = ""
    series: str = ""


@dataclass(frozen=True)
class PackRecord:
    pack_id: str
    version: str
    vendor: str = ""
    name: str = ""
    path: Optional[str] = None
    installed: bool = False
    source: str = "index"


@dataclass(frozen=True)
class MemoryRegion:
    name: str
    start: int
    length: int
    is_flash: bool
    writable: bool = True
    sector_size: Optional[int] = None

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(frozen=True)
class ImageSegment:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class ImageInspection:
    image_id: str
    file_name: str = ""
    file_path: str = ""
    format: str = ""
    size: int = 0
    sha256: str = ""
    start: int = 0
    end: int = 0
    segments: Tuple[ImageSegment, ...] = ()
    base_address: Optional[int] = None


@dataclass(frozen=True)
class JobRequest:
    actions: Tuple[str, ...]
    image_id: Optional[str] = None
    preempt_ai: bool = True
    probe_id: Optional[str] = None
    target_part: Optional[str] = None
    pack_path: Optional[str] = None
    custom_flm_paths: Tuple[str, ...] = ()
    custom_flm_digests: Tuple[str, ...] = ()
    custom_flm_regions: Tuple[Tuple[int, int], ...] = ()
    pack_flm_regions: Tuple[Tuple[int, int], ...] = ()
    custom_flm_ram_start: Optional[int] = None
    custom_flm_ram_size: Optional[int] = None
    frequency: int = 1_000_000
    connect_mode: str = "halt"
    reset_mode: str = "default"
    base_address: Optional[int] = None
    sector_addresses: Tuple[int, ...] = ()
    board: Optional[str] = None
    hpm_flash_cfg: Optional[Tuple[str, str, str, str]] = None

    @classmethod
    def full_sequence(
        cls,
        image: ImageInspection,
        *,
        preempt_ai: bool = True,
    ) -> "JobRequest":
        return cls(
            actions=("connect", "erase", "program", "verify", "reset", "disconnect"),
            image_id=image.image_id,
            preempt_ai=preempt_ai,
            base_address=image.base_address,
        )

    @classmethod
    def program_only(
        cls,
        image: ImageInspection,
        *,
        preempt_ai: bool = True,
    ) -> "JobRequest":
        return cls(
            actions=("connect", "program", "disconnect"),
            image_id=image.image_id,
            preempt_ai=preempt_ai,
            base_address=image.base_address,
        )


@dataclass(frozen=True)
class JobEvent:
    job_id: str
    sequence: int
    timestamp: float
    event: str
    message: str = ""
    state: Optional[JobState] = None
    progress: Optional[float] = None


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    state: JobState
    actions: Tuple[str, ...]
    image_id: Optional[str]
    created_at: float
    updated_at: float
    probe_id: Optional[str] = None
    target_part: Optional[str] = None
    frequency: int = 1_000_000
    connect_mode: str = "halt"
    reset_mode: str = "default"
    file_path: Optional[str] = None
    image_format: Optional[str] = None
    image_start: Optional[int] = None
    image_end: Optional[int] = None
    image_size: Optional[int] = None
    image_sha256: Optional[str] = None
    current_action: Optional[str] = None
    stage_progress: float = 0.0
    total_progress: float = 0.0
    speed_bytes_per_second: float = 0.0
    elapsed_seconds: float = 0.0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
