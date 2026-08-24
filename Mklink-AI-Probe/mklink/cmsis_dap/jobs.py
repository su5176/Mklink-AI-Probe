"""Single-probe online flash job execution and event replay."""

from __future__ import annotations

import inspect
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterator, List, Optional, Sequence

from .errors import FlashError, FlashErrorCode
from .daemon_executor import DaemonSingleExecutor
from .models import (
    ImageInspection,
    JobEvent,
    JobRequest,
    JobSnapshot,
    JobState,
    assert_transition,
)
from ..remote.resource_manager import ResourceError, ResourceGroup


# Keep the generic host-side read request aligned with the common 4 KiB
# Flash sector. Callers that have reliable FLM geometry may provide a smaller
# or larger exact sector sequence explicitly.
READ_MEMORY_CHUNK_SIZE = 4 * 1024
MAX_READ_MEMORY_SIZE = 64 * 1024 * 1024


_ACTION_STATES = {
    "connect": JobState.CONNECTING,
    "erase": JobState.ERASING,
    "program": JobState.PROGRAMMING,
    "verify": JobState.VERIFYING,
    "reset": JobState.RESETTING,
    "disconnect": JobState.DISCONNECTING,
}
_ACTION_ERRORS = {
    "connect": FlashErrorCode.CONNECT_FAIL,
    "erase": FlashErrorCode.ERASE_FAIL,
    "program": FlashErrorCode.PROGRAM_FAIL,
    "verify": FlashErrorCode.VERIFY_FAIL,
    "reset": FlashErrorCode.RESET_FAIL,
    "disconnect": FlashErrorCode.UNKNOWN_ERROR,
}
_TERMINAL = frozenset({JobState.STOPPED, JobState.SUCCEEDED, JobState.FAILED})


@dataclass
class _Job:
    job_id: str
    request: JobRequest
    created_at: float
    updated_at: float
    state: JobState = JobState.QUEUED
    current_action: Optional[str] = None
    stage_progress: float = 0.0
    total_progress: float = 0.0
    image: Optional[ImageInspection] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    cancel_requested: bool = False
    completed_actions: int = 0
    sequence: int = 0
    events: Deque[JobEvent] = field(default_factory=deque)
    future: Optional[Future] = None


class OnlineFlashJobManager:
    """Run at most one flash job and retain immutable public history."""

    def __init__(
        self,
        backend_factory: Callable[[], object],
        resource_manager: object,
        image_provider: Optional[Callable[[str], ImageInspection]] = None,
        *,
        prepare_connect: Optional[Callable[[JobRequest], None]] = None,
        max_completed: int = 20,
        max_events: int = 5000,
    ) -> None:
        for name, value in (
            ("max_completed", max_completed),
            ("max_events", max_events),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._backend_factory = backend_factory
        self._resource_manager = resource_manager
        self._image_provider = image_provider
        self._prepare_connect = prepare_connect
        self._max_completed = max_completed
        self._max_events = max_events
        self._jobs: Dict[str, _Job] = {}
        self._completed: Deque[str] = deque()
        self._active_id: Optional[str] = None
        self._condition = threading.Condition(threading.RLock())
        self._executor = DaemonSingleExecutor()
        self._shutdown = False

    def start(self, request: JobRequest) -> str:
        with self._condition:
            if self._shutdown:
                raise RuntimeError("online flash job manager is shut down")
            self._validate_request(request)
            if self._active_id is not None:
                raise FlashError(
                    FlashErrorCode.PROBE_BUSY,
                    "another online flash job is active",
                    {"job_id": self._active_id},
                )
            now = time.monotonic()
            job_id = str(uuid.uuid4())
            job = _Job(job_id, request, now, now)
            job.events = deque(maxlen=self._max_events)
            self._jobs[job_id] = job
            self._active_id = job_id
            try:
                self._emit_locked(job, "state", state=JobState.QUEUED)
                job.future = self._executor.submit(self._run, job)
                job.future.add_done_callback(
                    lambda future, captured=job: self._future_done(
                        captured, future
                    )
                )
            except Exception as exc:
                self._jobs.pop(job_id, None)
                if self._active_id == job_id:
                    self._active_id = None
                self._condition.notify_all()
                raise RuntimeError(
                    f"failed to submit online flash job: {exc}"
                ) from exc
            return job_id

    def read_memory(
        self,
        request: JobRequest,
        address: int,
        size: int,
        *,
        chunk_size: int = READ_MEMORY_CHUNK_SIZE,
    ) -> bytes:
        """Read target memory through a short-lived, resource-protected session."""
        if type(size) is not int or size <= 0 or size > MAX_READ_MEMORY_SIZE:
            raise ValueError(
                f"size must be between 1 and {MAX_READ_MEMORY_SIZE} bytes"
            )
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        chunk_size = min(chunk_size, READ_MEMORY_CHUNK_SIZE)
        chunk_sizes = tuple(
            min(chunk_size, size - offset)
            for offset in range(0, size, chunk_size)
        )
        return b"".join(self.iter_memory(request, address, chunk_sizes))

    def iter_memory(
        self,
        request: JobRequest,
        address: int,
        chunk_sizes: Sequence[int],
    ) -> Iterator[bytes]:
        """Yield target-memory chunks from one resource-protected connection."""
        from mklink.hpm_config import is_hpm_target

        hpm_target = is_hpm_target(request.target_part)
        if type(address) is not int or address < 0:
            raise ValueError("address must be a non-negative integer")
        sizes = tuple(chunk_sizes)
        if not sizes or any(type(value) is not int or value <= 0 for value in sizes):
            raise ValueError("chunk_sizes must contain positive integers")
        size = sum(sizes)
        if size > MAX_READ_MEMORY_SIZE:
            raise ValueError(
                f"size must be between 1 and {MAX_READ_MEMORY_SIZE} bytes"
            )
        owner = f"user:online-read:{uuid.uuid4()}"
        backend = None
        acquired = False
        try:
            acquired = True
            resources = [ResourceGroup.TARGET_DEBUG]
            if hpm_target:
                resources.append(ResourceGroup.MKLINK_BRIDGE)
            self._resource_manager.acquire_many(
                resources,
                owner,
                preempt=request.preempt_ai,
                preempt_user_dashboard=True,
            )
            if self._prepare_connect is not None:
                self._prepare_connect(request)
            backend = self._backend_factory()
            connect_options = dict(
                probe=request.probe_id,
                target=request.target_part,
                frequency=request.frequency,
                pack=request.pack_path,
                custom_flm_paths=request.custom_flm_paths,
                custom_flm_digests=request.custom_flm_digests,
                custom_flm_regions=request.custom_flm_regions,
                pack_flm_regions=request.pack_flm_regions,
                custom_flm_ram_start=request.custom_flm_ram_start,
                custom_flm_ram_size=request.custom_flm_ram_size,
                connect_mode=request.connect_mode,
                reset_mode=request.reset_mode,
            )
            if hpm_target:
                connect_options.update(
                    board=request.board,
                    hpm_flash_cfg=request.hpm_flash_cfg,
                )
            backend.connect(**connect_options)
            regions = tuple(backend.memory_regions())
            end = address + size
            if end < address or not any(
                address >= region.start and end <= region.end
                for region in regions
                if region.is_flash or region.is_ram
            ):
                raise FlashError(
                    FlashErrorCode.IMAGE_OUT_OF_RANGE,
                    "requested memory range is outside the target memory map",
                )
            offset = 0
            for chunk_size in sizes:
                yield backend.read_memory(address + offset, chunk_size)
                offset += chunk_size
        finally:
            if backend is not None:
                try:
                    backend.disconnect()
                except Exception:
                    pass
            if acquired:
                try:
                    self._resource_manager.release(owner)
                except Exception:
                    pass

    def get(self, job_id: str) -> JobSnapshot:
        with self._condition:
            return self._snapshot(self._require_job(job_id))

    def list(self) -> List[JobSnapshot]:
        with self._condition:
            identifiers = list(self._completed)
            if (
                self._active_id is not None
                and self._active_id not in identifiers
            ):
                identifiers.append(self._active_id)
            return [self._snapshot(self._jobs[job_id]) for job_id in identifiers]

    def wait(self, job_id: str, timeout: Optional[float] = None) -> JobSnapshot:
        with self._condition:
            job = self._require_job(job_id)
            if not self._condition.wait_for(lambda: job.state in _TERMINAL, timeout):
                raise TimeoutError(f"job {job_id} did not finish")
            return self._snapshot(job)

    def stop(self, job_id: str) -> JobSnapshot:
        with self._condition:
            job = self._require_job(job_id)
            if job.state in _TERMINAL or job.state is JobState.STOPPING:
                return self._snapshot(job)
            if job.cancel_requested and job.state is JobState.DISCONNECTING:
                return self._snapshot(job)
            job.cancel_requested = True
            if job.state is JobState.QUEUED:
                self._transition_locked(job, JobState.STOPPED)
                self._record_completed_locked(job)
            elif job.state is JobState.DISCONNECTING:
                self._emit_locked(
                    job, "log", message="stop requested while disconnecting"
                )
            else:
                self._transition_locked(job, JobState.STOPPING)
            return self._snapshot(job)

    def events(self, job_id: str, after: int = 0) -> List[JobEvent]:
        with self._condition:
            job = self._require_job(job_id)
            return [event for event in job.events if event.sequence > after]

    def wait_for_events(
        self, job_id: str, after: int = 0, timeout: Optional[float] = None
    ) -> List[JobEvent]:
        with self._condition:
            job = self._require_job(job_id)
            self._condition.wait_for(
                lambda: any(event.sequence > after for event in job.events)
                or job.state in _TERMINAL,
                timeout,
            )
            return [event for event in job.events if event.sequence > after]

    def shutdown(
        self,
        wait: bool = True,
        timeout: Optional[float] = None,
    ) -> bool:
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or timeout < 0
        ):
            raise ValueError("shutdown timeout must be nonnegative")
        with self._condition:
            self._shutdown = True
            active_id = self._active_id
        if active_id is not None:
            self.stop(active_id)
        if not wait:
            self._executor.shutdown(wait=False)
            return active_id is None
        if timeout is None:
            self._executor.shutdown(wait=True)
            return True
        with self._condition:
            completed = self._condition.wait_for(
                lambda: self._active_id is None,
                timeout=float(timeout),
            )
        self._executor.shutdown(wait=completed)
        return completed

    def _run(self, job: _Job) -> None:
        owner = f"user:online-flash:{job.job_id}"
        backend = None
        connection_may_be_open = False
        acquire_attempted = False
        deferred_erase = False
        primary_error: Optional[FlashError] = None
        with self._condition:
            if job.state is not JobState.QUEUED or job.cancel_requested:
                self._drain_active_locked(job)
                return
            try:
                self._transition_locked(job, JobState.CONNECTING, "connect")
            except Exception as exc:
                primary_error = FlashError(FlashErrorCode.UNKNOWN_ERROR, str(exc))
        try:
            try:
                if primary_error is not None:
                    raise primary_error

                acquire_attempted = True
                from mklink.hpm_config import is_hpm_target

                resources = [ResourceGroup.TARGET_DEBUG]
                if is_hpm_target(job.request.target_part):
                    resources.append(ResourceGroup.MKLINK_BRIDGE)
                    self._resource_manager.acquire_many(
                        resources,
                        owner,
                        preempt=job.request.preempt_ai,
                        preempt_user_dashboard=True,
                    )
                else:
                    self._resource_manager.acquire(
                        ResourceGroup.TARGET_DEBUG,
                        owner,
                        preempt=job.request.preempt_ai,
                        preempt_user_dashboard=True,
                    )
                with self._condition:
                    cancelled_after_acquire = job.cancel_requested

                if not cancelled_after_acquire:
                    if self._prepare_connect is not None:
                        try:
                            self._prepare_connect(job.request)
                        except FlashError:
                            raise
                        except Exception as exc:
                            raise FlashError(
                                FlashErrorCode.CONNECT_FAIL,
                                f"online flash connection preparation failed: {exc}",
                            ) from exc
                    backend = self._backend_factory()
                    for index, action in enumerate(job.request.actions[:-1]):
                        with self._condition:
                            if job.cancel_requested:
                                break
                            if index > 0:
                                self._transition_locked(
                                    job, _ACTION_STATES[action], action
                                )
                        try:
                            if action == "connect":
                                connection_may_be_open = True
                                connect_options = dict(
                                    probe=job.request.probe_id,
                                    target=job.request.target_part,
                                    frequency=job.request.frequency,
                                    pack=job.request.pack_path,
                                    custom_flm_paths=job.request.custom_flm_paths,
                                    custom_flm_digests=job.request.custom_flm_digests,
                                    custom_flm_regions=job.request.custom_flm_regions,
                                    pack_flm_regions=job.request.pack_flm_regions,
                                    custom_flm_ram_start=job.request.custom_flm_ram_start,
                                    custom_flm_ram_size=job.request.custom_flm_ram_size,
                                    connect_mode=job.request.connect_mode,
                                    reset_mode=job.request.reset_mode,
                                )
                                if is_hpm_target(job.request.target_part):
                                    connect_options.update(
                                        board=job.request.board,
                                        hpm_flash_cfg=job.request.hpm_flash_cfg,
                                    )
                                backend.connect(**connect_options)
                            elif action == "erase":
                                deferred_erase = bool(
                                    getattr(backend, "erase_deferred", False)
                                    and "program" in job.request.actions
                                )
                                if deferred_erase:
                                    with self._condition:
                                        self._emit_locked(
                                            job,
                                            "log",
                                            message="erase in progress (completed by HPM ROM during program)",
                                        )
                                if job.request.sector_addresses:
                                    backend.erase_sectors(job.request.sector_addresses)
                                else:
                                    backend.erase_chip()
                            elif action == "program":
                                self._refresh_image(job)
                                with self._condition:
                                    if job.cancel_requested:
                                        break
                                last_reported_percent = -1

                                def report_program_progress(value: float) -> None:
                                    nonlocal deferred_erase, last_reported_percent
                                    try:
                                        fraction = float(value)
                                    except (TypeError, ValueError):
                                        return
                                    if fraction > 1:
                                        fraction /= 100.0
                                    fraction = min(1.0, max(0.0, fraction))
                                    if deferred_erase and fraction <= 0:
                                        return
                                    percent = int(fraction * 100)
                                    if percent <= last_reported_percent and fraction < 1:
                                        return
                                    last_reported_percent = percent
                                    if deferred_erase and fraction > 0:
                                        with self._condition:
                                            self._complete_deferred_stage_locked(job, "erase")
                                        deferred_erase = False
                                    self._program_progress(job, fraction)

                                if not deferred_erase:
                                    report_program_progress(0)
                                backend.program(
                                    job.image,
                                    progress_callback=report_program_progress,
                                )
                                if deferred_erase:
                                    with self._condition:
                                        self._complete_deferred_stage_locked(job, "erase")
                                    deferred_erase = False
                                report_program_progress(1)
                            elif action == "verify":
                                self._refresh_image(job)
                                with self._condition:
                                    if job.cancel_requested:
                                        break
                                last_reported_percent = -1

                                def report_verify_progress(value: float) -> None:
                                    nonlocal last_reported_percent
                                    try:
                                        fraction = float(value)
                                    except (TypeError, ValueError):
                                        return
                                    if fraction > 1:
                                        fraction /= 100.0
                                    fraction = min(1.0, max(0.0, fraction))
                                    percent = int(fraction * 100)
                                    if percent <= last_reported_percent and fraction < 1:
                                        return
                                    last_reported_percent = percent
                                    self._verify_progress(job, fraction)

                                report_verify_progress(0)
                                verify = backend.verify
                                try:
                                    verify_parameters = inspect.signature(verify).parameters
                                    accepts_progress = (
                                        "progress_callback" in verify_parameters
                                        or any(
                                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                                            for parameter in verify_parameters.values()
                                        )
                                    )
                                except (TypeError, ValueError):
                                    accepts_progress = False
                                if accepts_progress:
                                    verify(
                                        job.image,
                                        progress_callback=report_verify_progress,
                                    )
                                else:
                                    verify(job.image)
                                report_verify_progress(1)
                            elif action == "reset":
                                backend.reset_run(job.request.reset_mode)
                        except FlashError:
                            raise
                        except Exception as exc:
                            raise FlashError(_ACTION_ERRORS[action], str(exc)) from exc
                        with self._condition:
                            if not (action == "erase" and deferred_erase):
                                self._stage_complete_locked(job, action)
            except ResourceError as exc:
                primary_error = FlashError(
                    FlashErrorCode.PROBE_BUSY,
                    str(exc),
                    {
                        "conflict_owner": exc.conflict_owner,
                        "resource": exc.resource.value,
                    },
                )
            except FlashError as exc:
                primary_error = exc
            except Exception as exc:
                primary_error = FlashError(FlashErrorCode.UNKNOWN_ERROR, str(exc))
        finally:
            try:
                if backend is not None and connection_may_be_open:
                    try:
                        with self._condition:
                            if (
                                job.state not in _TERMINAL
                                and job.state is not JobState.DISCONNECTING
                            ):
                                self._transition_locked(
                                    job, JobState.DISCONNECTING, "disconnect"
                                )
                        backend.disconnect()
                        with self._condition:
                            self._stage_complete_locked(job, "disconnect")
                    except Exception as exc:
                        primary_error = self._record_cleanup_error(
                            job,
                            primary_error,
                            "disconnect",
                            exc,
                        )

                if acquire_attempted:
                    try:
                        self._resource_manager.release(owner)
                    except Exception as exc:
                        primary_error = self._record_cleanup_error(
                            job,
                            primary_error,
                            "release",
                            exc,
                        )
            except Exception as exc:
                if primary_error is None:
                    primary_error = FlashError(
                        FlashErrorCode.UNKNOWN_ERROR,
                        f"cleanup failed: {exc}",
                    )
            finally:
                try:
                    with self._condition:
                        try:
                            if job.state not in _TERMINAL:
                                if primary_error is not None:
                                    self._fail_locked(job, primary_error)
                                elif job.cancel_requested:
                                    self._transition_locked(job, JobState.STOPPED)
                                else:
                                    self._transition_locked(job, JobState.SUCCEEDED)
                        except Exception as exc:
                            job.state = JobState.FAILED
                            job.updated_at = time.monotonic()
                            if primary_error is None:
                                job.error_code = FlashErrorCode.UNKNOWN_ERROR.value
                                job.error_message = f"terminalization failed: {exc}"
                        finally:
                            self._finish_locked(job)
                except Exception:
                    with self._condition:
                        job.state = JobState.FAILED
                        job.updated_at = time.monotonic()
                        if job.error_code is None:
                            job.error_code = FlashErrorCode.UNKNOWN_ERROR.value
                            job.error_message = "terminalization failed"
                        if self._active_id == job.job_id:
                            self._active_id = None
                        self._condition.notify_all()

    def _record_cleanup_error(
        self,
        job: _Job,
        primary_error: Optional[FlashError],
        operation: str,
        error: Exception,
    ) -> FlashError:
        if primary_error is None:
            return FlashError(
                FlashErrorCode.UNKNOWN_ERROR,
                f"{operation} failed: {error}",
            )
        try:
            with self._condition:
                self._emit_locked(
                    job,
                    "log",
                    message=f"{operation} also failed: {error}",
                )
        except Exception:
            pass
        return primary_error

    def _refresh_image(self, job: _Job) -> None:
        if self._image_provider is None or job.request.image_id is None:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND,
                "image provider is unavailable",
            )
        inspected = self._image_provider(job.request.image_id)
        if inspected.image_id != job.request.image_id:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND,
                "image provider returned a different image",
            )
        with self._condition:
            job.image = inspected
            job.updated_at = time.monotonic()

    def _transition_locked(
        self, job: _Job, state: JobState, action: Optional[str] = None
    ) -> None:
        assert_transition(job.state, state)
        job.state = state
        job.updated_at = time.monotonic()
        if action is not None:
            job.current_action = action
            job.stage_progress = 0.0
        self._emit_locked(job, "state", state=state)

    def _stage_complete_locked(self, job: _Job, action: str) -> None:
        job.current_action = action
        job.stage_progress = 1.0
        job.completed_actions += 1
        job.total_progress = job.completed_actions / len(job.request.actions)
        job.updated_at = time.monotonic()
        self._emit_locked(job, "progress", progress=job.total_progress)
        self._emit_locked(job, "log", message=f"{action} complete")

    def _complete_deferred_stage_locked(self, job: _Job, action: str) -> None:
        """Complete a prior stage without overwriting the active stage."""
        job.completed_actions += 1
        job.total_progress = max(
            job.total_progress,
            job.completed_actions / len(job.request.actions),
        )
        job.updated_at = time.monotonic()
        self._emit_locked(job, "progress", progress=job.total_progress)
        self._emit_locked(job, "log", message=f"{action} complete")

    def _program_progress(self, job: _Job, fraction: float) -> None:
        with self._condition:
            if job.current_action != "program" or job.image is None:
                return
            if fraction < job.stage_progress:
                return
            job.stage_progress = fraction
            job.total_progress = max(
                job.total_progress,
                (job.completed_actions + fraction) / len(job.request.actions),
            )
            job.updated_at = time.monotonic()
            completed = min(job.image.size, round(job.image.size * fraction))
            self._emit_locked(
                job,
                "progress",
                message=(
                    f"[PROGRAM] {completed} / {job.image.size} Bytes "
                    f"({round(fraction * 100)}%)"
                ),
                progress=job.total_progress,
            )

    def _verify_progress(self, job: _Job, fraction: float) -> None:
        with self._condition:
            if job.current_action != "verify" or job.image is None:
                return
            if fraction < job.stage_progress:
                return
            job.stage_progress = fraction
            job.total_progress = max(
                job.total_progress,
                (job.completed_actions + fraction) / len(job.request.actions),
            )
            job.updated_at = time.monotonic()
            verified = min(job.image.size, round(job.image.size * fraction))
            self._emit_locked(
                job,
                "progress",
                message=(
                    f"[VERIFY] {verified} / {job.image.size} Bytes "
                    f"({round(fraction * 100)}%)"
                ),
                progress=job.total_progress,
            )

    def _fail_locked(self, job: _Job, error: FlashError) -> None:
        job.error_code = error.code.value
        job.error_message = error.message
        self._emit_locked(job, "error", message=error.message, state=JobState.FAILED)
        self._transition_locked(job, JobState.FAILED)

    def _emit_locked(
        self,
        job: _Job,
        event: str,
        *,
        message: str = "",
        state: Optional[JobState] = None,
        progress: Optional[float] = None,
    ) -> None:
        job.sequence += 1
        job.events.append(
            JobEvent(
                job_id=job.job_id,
                sequence=job.sequence,
                timestamp=time.time(),
                event=event,
                message=message,
                state=state,
                progress=progress,
            )
        )
        self._condition.notify_all()

    def _finish_locked(self, job: _Job) -> None:
        self._record_completed_locked(job)
        self._drain_active_locked(job)

    def _record_completed_locked(self, job: _Job) -> None:
        if job.job_id not in self._completed:
            self._completed.append(job.job_id)
        self._trim_completed_locked()
        self._condition.notify_all()

    def _drain_active_locked(self, job: _Job) -> None:
        if self._active_id == job.job_id:
            self._active_id = None
        self._trim_completed_locked()
        self._condition.notify_all()

    def _future_done(self, job: _Job, future: Future) -> None:
        with self._condition:
            if future.cancelled():
                job.cancel_requested = True
                if job.state is JobState.QUEUED:
                    self._transition_locked(job, JobState.STOPPED)
                    self._record_completed_locked(job)
                self._drain_active_locked(job)
            self._trim_completed_locked()
            self._condition.notify_all()

    def _trim_completed_locked(self) -> None:
        while len(self._completed) > self._max_completed:
            candidate_id = self._completed[0]
            candidate = self._jobs.get(candidate_id)
            if (
                candidate is not None
                and candidate.future is not None
                and not candidate.future.done()
            ):
                break
            self._completed.popleft()
            self._jobs.pop(candidate_id, None)

    def _snapshot(self, job: _Job) -> JobSnapshot:
        inspected = job.image
        elapsed_end = job.updated_at if job.state in _TERMINAL else time.monotonic()
        return JobSnapshot(
            job_id=job.job_id,
            state=job.state,
            actions=job.request.actions,
            image_id=job.request.image_id,
            created_at=job.created_at,
            updated_at=job.updated_at,
            probe_id=job.request.probe_id,
            target_part=job.request.target_part,
            frequency=job.request.frequency,
            connect_mode=job.request.connect_mode,
            reset_mode=job.request.reset_mode,
            file_path=inspected.file_path if inspected else None,
            image_format=inspected.format if inspected else None,
            image_start=inspected.start if inspected else None,
            image_end=inspected.end if inspected else None,
            image_size=inspected.size if inspected else None,
            image_sha256=inspected.sha256 if inspected else None,
            current_action=job.current_action,
            stage_progress=job.stage_progress,
            total_progress=job.total_progress,
            elapsed_seconds=max(0.0, elapsed_end - job.created_at),
            error_code=job.error_code,
            error_message=job.error_message,
        )

    def _require_job(self, job_id: str) -> _Job:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"unknown online flash job: {job_id}") from exc

    @staticmethod
    def _validate_request(request: JobRequest) -> None:
        actions = request.actions
        if not actions or actions[0] != "connect" or actions[-1] != "disconnect":
            raise ValueError("actions must start with connect and end with disconnect")
        if len(set(actions)) != len(actions):
            raise ValueError("duplicate actions are not allowed")
        unknown = set(actions) - set(_ACTION_STATES)
        if unknown:
            raise ValueError(f"unknown actions: {sorted(unknown)}")
        if any(action in actions for action in ("program", "verify")) and not request.image_id:
            raise ValueError("program and verify require an image")
        if request.sector_addresses and "erase" not in actions:
            raise ValueError("sector addresses require erase")
        state = JobState.QUEUED
        for action in actions:
            next_state = _ACTION_STATES[action]
            assert_transition(state, next_state)
            state = next_state
