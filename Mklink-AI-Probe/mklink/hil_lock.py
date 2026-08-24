"""HIL-Infra lockd 兼容跨进程文件锁（协议级原生对齐，零第三方依赖）。

与 hil_core.lockd 完全互操作：同锁根目录（%LOCALAPPDATA%\\hil-core\\locks，
可用 HIL_CORE_LOCK_ROOT 覆盖）、同 JSON 字段（owner_id/pid/hostname/
acquired_at/lease_s/expires_at）、同 O_CREAT|O_EXCL 排他创建与租约过期
回收语义、同文件名清洗规则。插件在本模块上取的锁，底座编排器
（hil_core.lockd）可见并可判定冲突；反之亦然。

锁名约定与 bench.yaml 的 transport 声明一致：
    usb-serial + COM5  -> transport_usb-serial_COM5
    usb + 0x0080       -> transport_usb_0x0080
    usb + <dev_index>  -> transport_usb_<dev_index>
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_ENV_ROOT = os.environ.get("HIL_CORE_LOCK_ROOT", "")
LOCK_ROOT = (Path(_ENV_ROOT) if _ENV_ROOT else
             Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hil-core" / "locks")

DEFAULT_LEASE_S = 3600.0  # 会话级长租约：过期即崩溃回收视界


class HilLockHeld(RuntimeError):
    """锁被他人持有；holder 含 pid/hostname/owner_id/expires_at。"""

    def __init__(self, name: str, holder: dict):
        self.name = name
        self.holder = holder
        super().__init__(
            f"HIL lock '{name}' held by pid={holder.get('pid')} "
            f"host={holder.get('hostname')} owner={holder.get('owner_id')} "
            f"expires_at={holder.get('expires_at')}"
        )


def transport_lock_name(kind: str, locator: str) -> str:
    return f"transport_{kind}_{locator}"


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


@contextmanager
def _exclusive_guard(path: Path, timeout_s: float = 10.0):
    """Serialize lock-file ownership checks and metadata updates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised by Linux CI
                    import fcntl

                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                acquired = True
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"wait HIL lock guard timeout: {path}")
                time.sleep(0.01)
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - exercised by Linux CI
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _local_holder_is_dead(holder: dict) -> bool:
    """Return true only when a same-host lock owner is confirmed dead."""
    if holder.get("hostname") != socket.gethostname():
        return False
    try:
        pid = int(holder.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    # Reuse the conservative platform-specific check used by local serial
    # cleanup. Inspection failures count as alive, so an active lock is never
    # reclaimed merely because its process cannot be queried.
    from mklink.local_resources import _pid_exists

    return not _pid_exists(pid)


class HilFileLock:
    """一次性获取/释放的租约文件锁；支持 with 语法与幂等 release。"""

    def __init__(self, name: str, *, root=None, lease_s: float = DEFAULT_LEASE_S,
                 owner_id: str | None = None, purpose: str = ""):
        self.name = name
        self.root = Path(root) if root is not None else LOCK_ROOT
        self.path = self.root / (_sanitize(name) + ".lock")
        self.guard_path = self.path.with_name(self.path.name + ".guard")
        self.lease_s = float(lease_s)
        prefix = f"{purpose}-" if purpose else ""
        self.owner_id = owner_id or f"{prefix}{uuid.uuid4().hex[:12]}"
        self._held = False

    # --- 内部 ---
    def _payload(self) -> dict:
        now = time.time()
        return {
            "owner_id": self.owner_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": now,
            "lease_s": self.lease_s,
            "expires_at": now + self.lease_s,
        }

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    # --- 公开 ---
    def acquire(self) -> "HilFileLock":
        self.root.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self._payload()).encode("utf-8")
        with _exclusive_guard(self.guard_path):
            for _ in range(4):  # guard 内回收并排他创建
                try:
                    fd = os.open(
                        self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    )
                except FileExistsError:
                    holder = self._read()
                    if holder is None:
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        continue
                    expired = (
                        float(holder.get("expires_at", 0) or 0) < time.time()
                    )
                    if expired or _local_holder_is_dead(holder):
                        try:
                            self.path.unlink()
                            continue
                        except OSError:
                            pass
                    raise HilLockHeld(self.name, holder)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                self._held = True
                return self
        raise HilLockHeld(self.name, self._read() or {})

    def release(self) -> bool:
        if not self._held:
            return False
        with _exclusive_guard(self.guard_path):
            holder = self._read()
            if holder is not None and holder.get("owner_id") == self.owner_id:
                try:
                    self.path.unlink()
                except OSError:
                    pass
        self._held = False
        return True

    def renew(self, lease_s: float | None = None) -> bool:
        """续租：先验所有权再原子替换；失败（含所有权丢失）返回 False。

        长会话按 lease_s/3 周期调用，防止租约过期后被他人回收。
        """
        if not self._held:
            return False
        if lease_s is not None:
            self.lease_s = float(lease_s)
        tmp = self.path.with_name(self.path.name + f".renew-{self.owner_id[:8]}")
        with _exclusive_guard(self.guard_path):
            tmp.write_text(json.dumps(self._payload()), encoding="utf-8")
            for attempt in range(5):  # Windows 杀毒/索引器可能短暂持锁
                try:
                    current = self._read()
                    if current is None or current.get("owner_id") != self.owner_id:
                        tmp.unlink()
                        return False  # 所有权已丢失（过期被回收）
                    os.replace(tmp, self.path)
                    return True
                except OSError:
                    if attempt == 4:
                        tmp.unlink()
                        return False
                    time.sleep(0.02)
        return False

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> "HilFileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
