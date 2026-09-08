"""Stable errors exposed by the online flash API."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional


class FlashErrorCode(str, Enum):
    MKLINK_DAP_NOT_FOUND = "MKLINK_DAP_NOT_FOUND"
    PROBE_BUSY = "PROBE_BUSY"
    TARGET_NOT_SUPPORTED = "TARGET_NOT_SUPPORTED"
    PACK_INDEX_UNAVAILABLE = "PACK_INDEX_UNAVAILABLE"
    PACK_NOT_FOUND = "PACK_NOT_FOUND"
    PACK_DOWNLOAD_FAIL = "PACK_DOWNLOAD_FAIL"
    PACK_INTEGRITY_ERROR = "PACK_INTEGRITY_ERROR"
    CONNECT_FAIL = "CONNECT_FAIL"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_FORMAT_ERROR = "FILE_FORMAT_ERROR"
    BIN_ADDRESS_MISSING = "BIN_ADDRESS_MISSING"
    IMAGE_OUT_OF_RANGE = "IMAGE_OUT_OF_RANGE"
    TARGET_LOCKED = "TARGET_LOCKED"
    SECURITY_NOT_SUPPORTED = "SECURITY_NOT_SUPPORTED"
    UNLOCK_FAIL = "UNLOCK_FAIL"
    LOCK_FAIL = "LOCK_FAIL"
    ERASE_FAIL = "ERASE_FAIL"
    PROGRAM_FAIL = "PROGRAM_FAIL"
    VERIFY_FAIL = "VERIFY_FAIL"
    RESET_FAIL = "RESET_FAIL"
    USER_ABORT = "USER_ABORT"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


FLASH_ERROR_TITLES: Mapping[FlashErrorCode, str] = MappingProxyType({
    FlashErrorCode.MKLINK_DAP_NOT_FOUND: "未找到 MKLink DAP",
    FlashErrorCode.PROBE_BUSY: "探针忙",
    FlashErrorCode.TARGET_NOT_SUPPORTED: "目标芯片不受支持",
    FlashErrorCode.PACK_INDEX_UNAVAILABLE: "Pack 索引不可用",
    FlashErrorCode.PACK_NOT_FOUND: "未找到 Pack",
    FlashErrorCode.PACK_DOWNLOAD_FAIL: "Pack 下载失败",
    FlashErrorCode.PACK_INTEGRITY_ERROR: "Pack 完整性错误",
    FlashErrorCode.CONNECT_FAIL: "连接失败",
    FlashErrorCode.FILE_NOT_FOUND: "未找到固件文件",
    FlashErrorCode.FILE_FORMAT_ERROR: "固件格式错误",
    FlashErrorCode.BIN_ADDRESS_MISSING: "缺少 BIN 起始地址",
    FlashErrorCode.IMAGE_OUT_OF_RANGE: "镜像超出 Flash 范围",
    FlashErrorCode.TARGET_LOCKED: "目标芯片已锁定",
    FlashErrorCode.SECURITY_NOT_SUPPORTED: "不支持安全操作",
    FlashErrorCode.UNLOCK_FAIL: "解锁失败",
    FlashErrorCode.LOCK_FAIL: "加锁失败",
    FlashErrorCode.ERASE_FAIL: "擦除失败",
    FlashErrorCode.PROGRAM_FAIL: "烧录失败",
    FlashErrorCode.VERIFY_FAIL: "校验失败",
    FlashErrorCode.RESET_FAIL: "复位失败",
    FlashErrorCode.USER_ABORT: "用户已中止",
    FlashErrorCode.UNKNOWN_ERROR: "未知错误",
})


class FlashError(Exception):
    """An online flash failure with a stable machine-readable code."""

    def __init__(
        self,
        code: FlashErrorCode,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else None

    @property
    def title(self) -> str:
        return FLASH_ERROR_TITLES[self.code]

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "code": self.code.value,
            "title": self.title,
            "message": self.message,
        }
        if self.details is not None:
            result["details"] = self.details
        return result
