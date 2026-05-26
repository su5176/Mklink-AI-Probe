"""ResourceManager — Dashboard / AI 资源租约管理。

协调 MKLink Bridge、Serial Port、Modbus Port 三类资源的互斥访问。
优先级规则：user:* > ai:*，用户操作可强制抢占 AI 租约。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class ResourceGroup(Enum):
    MKLINK_BRIDGE = "mklink_bridge"
    SERIAL_PORT = "serial_port"
    MODBUS_PORT = "modbus_port"


@dataclass
class ResourceLease:
    owner: str
    resource: ResourceGroup
    acquired_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.monotonic() > self.expires_at

    @property
    def is_user(self) -> bool:
        return self.owner.startswith("user:")

    @property
    def is_ai(self) -> bool:
        return self.owner.startswith("ai:")


class ResourceError(Exception):
    def __init__(self, conflict_owner: str, resource: ResourceGroup):
        self.conflict_owner = conflict_owner
        self.resource = resource
        super().__init__(
            f"Resource {resource.value} is held by {conflict_owner}"
        )


class ResourceManager:
    def __init__(self):
        self._leases: dict[ResourceGroup, ResourceLease] = {}
        self._on_preempt: list[Callable[[ResourceLease, str], None]] = []

    def on_preempt(self, callback: Callable[[ResourceLease, str], None]):
        """注册抢占回调。当 AI 租约被用户抢占时触发。"""
        self._on_preempt.append(callback)

    def acquire(
        self,
        resource: ResourceGroup,
        owner: str,
        ttl: float | None = None,
        preempt: bool = False,
    ) -> ResourceLease:
        """获取资源租约。

        Args:
            resource: 资源类型
            owner: 所有者标识，格式 "user:dashboard:<type>" 或 "ai:session:<id>"
            ttl: 租约过期时间（秒），None 表示不过期
            preempt: 是否强制抢占低优先级持有者

        Returns:
            ResourceLease

        Raises:
            ResourceError: 资源被同等或更高优先级持有者占用
        """
        # 清理过期租约
        existing = self._leases.get(resource)
        if existing and existing.is_expired:
            del self._leases[resource]
            existing = None

        if existing is None:
            lease = self._make_lease(resource, owner, ttl)
            self._leases[resource] = lease
            return lease

        # 同一所有者刷新租约
        if existing.owner == owner:
            lease = self._make_lease(resource, owner, ttl)
            self._leases[resource] = lease
            return lease

        # 用户抢占 AI
        if preempt and owner.startswith("user:") and existing.is_ai:
            self._notify_preempt(existing, owner)
            lease = self._make_lease(resource, owner, ttl)
            self._leases[resource] = lease
            return lease

        raise ResourceError(existing.owner, resource)

    def release(self, owner: str) -> list[ResourceGroup]:
        """释放指定所有者的所有租约。返回被释放的资源列表。"""
        released = []
        for res, lease in list(self._leases.items()):
            if lease.owner == owner:
                del self._leases[res]
                released.append(res)
        return released

    def release_all(self) -> None:
        """释放所有租约。"""
        self._leases.clear()

    def get_active_lease(self, resource: ResourceGroup) -> ResourceLease | None:
        """获取指定资源的活跃租约。"""
        lease = self._leases.get(resource)
        if lease and lease.is_expired:
            del self._leases[resource]
            return None
        return lease

    def get_status(self) -> dict:
        """返回所有资源的当前状态。"""
        # 清理过期租约
        for res in list(self._leases):
            lease = self._leases.get(res)
            if lease and lease.is_expired:
                del self._leases[res]

        return {
            res.value: {
                "owner": lease.owner,
                "acquired_at": lease.acquired_at,
                "expires_at": lease.expires_at,
                "is_user": lease.is_user,
                "is_ai": lease.is_ai,
            }
            for res, lease in self._leases.items()
        }

    def _make_lease(
        self, resource: ResourceGroup, owner: str, ttl: float | None
    ) -> ResourceLease:
        return ResourceLease(
            owner=owner,
            resource=resource,
            acquired_at=time.monotonic(),
            expires_at=time.monotonic() + ttl if ttl else None,
        )

    def _notify_preempt(self, lease: ResourceLease, new_owner: str):
        for cb in self._on_preempt:
            try:
                cb(lease, new_owner)
            except Exception:
                pass
