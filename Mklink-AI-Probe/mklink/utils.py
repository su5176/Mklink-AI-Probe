"""
MKLink Serial Bridge — 纯工具函数合集。

包含进度解析、hex dump、输出格式化。
零外部依赖，零内部依赖。
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# 进度解析
# ---------------------------------------------------------------------------
def parse_download_progress(output: str) -> list[dict]:
    """解析烧录进度输出（无换行拼接格式）。

    示例输入:
        Download:   5% ,used 234 msDownload:  11% ,used 508 ms...

    返回:
        [{"percent": 5, "used_ms": 234}, {"percent": 11, "used_ms": 508}, ...]
    """
    results = []
    for m in re.finditer(r'Download:\s*(\d+)%\s*,\s*used\s*(\d+)\s*ms', output):
        results.append({
            "percent": int(m.group(1)),
            "used_ms": int(m.group(2)),
        })
    return results


def parse_load_result(output: str) -> dict:
    """解析烧录结果（支持单文件和多文件）。

    返回:
        单文件: {"success": bool, "filename": str, "progress": [...]}
        多文件: {"success": bool, "results": [{"filename": str, "success": bool}], "progress": [...]}
    """
    progress = parse_download_progress(output)

    results = []
    for m in re.finditer(r'/(\S+)\s+loaded\s+(success|failed)', output):
        results.append({
            "filename": m.group(1),
            "success": m.group(2) == "success",
        })

    # Fallback: 如果没有找到 loaded 模式，检查是否有 100% 进度且无错误
    if not results:
        if progress and progress[-1]["percent"] == 100:
            # 100% 进度存在，检查是否有错误标志
            if "error" not in output.lower() and "fail" not in output.lower():
                return {"success": True, "progress": progress}
        # 也检查返回值是否为 "0"（设备端成功标志）
        for line in output.strip().split("\n"):
            if line.strip() == "0":
                return {"success": True, "progress": progress}
        return {"success": False, "progress": progress}

    if len(results) == 1:
        return {
            "success": results[0]["success"],
            "filename": results[0]["filename"],
            "progress": progress,
        }

    return {
        "success": all(r["success"] for r in results),
        "results": results,
        "progress": progress,
    }


# ---------------------------------------------------------------------------
# Hex dump 工具
# ---------------------------------------------------------------------------
def hex_dump(data: bytes, base_addr: int = 0) -> str:
    """生成标准 hex dump 表格（地址 + 16 字节/行 + ASCII）。"""
    lines = []
    header = "         00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F"
    lines.append(header)

    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        addr_str = f"{base_addr + offset:08X}"
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        hex_part = hex_part.ljust(47)
        ascii_part = "".join(
            chr(b) if 0x20 <= b < 0x7F else "." for b in chunk
        )
        lines.append(f"{addr_str} {hex_part} {ascii_part}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 格式化输出
# ---------------------------------------------------------------------------
def format_progress_bar(percent: int, width: int = 20) -> str:
    """生成进度条。"""
    filled = int(width * percent / 100)
    bar = "=" * filled + ">" + " " * (width - filled)
    return f"[{bar}] {percent}%"


def format_rtt_info(info: dict) -> str:
    """格式化 RTT 启动信息。"""
    lines = ["[OK] RTT 已启动"]
    if info.get("control_block_addr"):
        lines.append(f"  控制块地址: {info['control_block_addr']}")

    for buf in info.get("up_buffers", []):
        active = "  ✓ 活跃" if buf["active"] else ""
        lines.append(
            f"  UpBuffer Ch{buf['channel']}: {buf['size']} 字节 (MCU → PC){active}"
        )

    for buf in info.get("down_buffers", []):
        active = "  ✓ 活跃" if buf["active"] else ""
        lines.append(
            f"  DownBuffer Ch{buf['channel']}: {buf['size']} 字节 (PC → MCU){active}"
        )

    return "\n".join(lines)


def format_idcode(idcode: int, mcu_name: str | None = None) -> str:
    """格式化 IDCODE 输出。"""
    base = f"IDCODE: 0x{idcode:08X}"
    if mcu_name:
        return f"{base} → {mcu_name} (已自动匹配)"
    return base
