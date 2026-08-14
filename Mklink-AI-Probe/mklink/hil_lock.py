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


class HilFileLock:
    """一次性获取/释放的租约文件锁；支持 with 语法与幂等 release。"""

    def __init__(self, name: str, *, root=None, lease_s: float = DEFAULT_LEASE_S,
                 owner_id: str | None = None, purpose: str = ""):
        self.name = name
        self.root = Path(root) if root is not None else LOCK_ROOT
        self.path = self.root / (_sanitize(name) + ".lock")
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
        for _ in range(4):  # 回收竞争重试上限：创建成功者唯一
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read()
                if holder is None:
                    # 损坏/残缺文件按崩溃处理：清除后重试
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if float(holder.get("expires_at", 0) or 0) < time.time():
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
        holder = self._read()
        if holder is not None and holder.get("owner_id") == self.owner_id:
            try:
                self.path.unlink()
            except OSError:
                pass
        self._held = False
        return True

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> "HilFileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
