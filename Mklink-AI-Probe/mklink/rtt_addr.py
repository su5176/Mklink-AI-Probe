"""
MKLink Serial Bridge — RTT 地址查找（从 .map / .elf / .out 文件）。

零外部依赖（仅 stdlib），零内部依赖。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RTTFindResult:
    """RTT 地址查找结果。"""

    addr: str | None = None
    source: str = ""
    details: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    path_checked: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return bool(self.addr)


def find_rtt_addr_from_map(map_file_path: str) -> str | None:
    """向后兼容接口：返回 RTT 地址或 None。"""
    return diagnose_rtt_addr(map_file_path).addr


def diagnose_rtt_addr(map_file_path: str) -> RTTFindResult:
    """诊断 RTT 地址查找过程，返回地址和失败原因。"""
    path = Path(map_file_path)
    result = RTTFindResult()
    result.path_checked.append(str(path))

    if not path.exists():
        result.details.append(f"文件不存在: {map_file_path}")
        return result

    # 优先按传入文件类型解析。
    if path.suffix.lower() in (".elf", ".out", ".axf"):
        addr = _find_rtt_in_binary(path, result)
        if addr:
            result.addr = addr
            result.source = f"binary:{path.name}"
            return result
    else:
        addr = _find_rtt_in_map(path, result)
        if addr:
            result.addr = addr
            result.source = f"map:{path.name}"
            return result

    # 对 map 文件自动回退到同目录/兄弟目录的 ELF/OUT。
    if path.suffix.lower() == ".map":
        for candidate in _candidate_binary_paths(path):
            result.path_checked.append(str(candidate))
            if not candidate.exists():
                continue
            addr = _find_rtt_in_binary(candidate, result)
            if addr:
                result.addr = addr
                result.source = f"binary:{candidate.name}"
                return result

    # 如果还没找到，补充 map 诊断。
    if path.suffix.lower() == ".map":
        _diagnose_map_failure(path, result)

    if not result.details:
        result.details.append("未找到 _SEGGER_RTT 地址")
    return result


def _candidate_binary_paths(map_path: Path) -> list[Path]:
    """根据 .map 路径推断同构建目录中的 .out/.elf/.axf。"""
    candidates: list[Path] = []
    stem = map_path.stem

    for suffix in (".out", ".elf", ".axf"):
        candidates.append(map_path.with_suffix(suffix))

    if map_path.parent.name.lower() == "list":
        sibling_dir = map_path.parent.parent / "Exe"
        for suffix in (".out", ".elf", ".axf"):
            candidates.append(sibling_dir / f"{stem}{suffix}")

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _find_rtt_in_binary(path: Path, result: RTTFindResult) -> str | None:
    """通过符号工具查找 ELF/OUT 中的 RTT 符号。"""
    tools = [
        ["arm-none-eabi-nm", str(path)],
        ["D:\\IAR\\arm\\bin\\ielfdumparm.exe", str(path)],
        ["D:\\IAR\\arm\\bin\\ielftool.exe", "--symbols", str(path)],
    ]

    saw_missing_tool = False
    for cmd in tools:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
            )
        except FileNotFoundError:
            saw_missing_tool = True
            continue
        except subprocess.TimeoutExpired:
            result.warnings.append(f"符号工具超时: {cmd[0]}")
            continue

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        addr = _extract_rtt_addr_from_text(output)
        if addr:
            return addr

    if saw_missing_tool:
        result.warnings.append("未找到可用的符号工具（arm-none-eabi-nm / ielfdumparm / ielftool）")
    return None


def _find_rtt_in_map(path: Path, result: RTTFindResult | None = None) -> str | None:
    """从 .map 文件中正则匹配 RTT 地址。"""
    patterns = [
        r"^\s*(0x[0-9a-fA-F]{8})\s+_SEGGER_RTT(?:\s|$)",
        r"^\s*(0x[0-9a-fA-F]{8})\s+\S+\s+_SEGGER_RTT(?:\s|$)",
        r"^\s*([0-9a-fA-F]{8})\s+[TtBbDd]\s+_SEGGER_RTT(?:\s|$)",
        r"^\s*_SEGGER_RTT\s+(0x[0-9a-fA-F]{8})(?:\s|$)",
        r"^\s*_SEGGER_RTT\s+(0x[0-9a-fA-F]{8})\s+[0-9a-fA-Fx]+\s+[A-Za-z]+(?:\s|$)",
    ]

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "_SEGGER_RTT" not in line:
                    continue
                for pattern in patterns:
                    match = re.search(pattern, line)
                    if not match:
                        continue
                    addr = match.group(1)
                    if not addr.startswith("0x"):
                        addr = "0x" + addr
                    addr_int = int(addr, 16)
                    if _is_valid_ram_addr(addr_int):
                        return addr
                    if result is not None:
                        result.warnings.append(f"找到 _SEGGER_RTT 但地址不在 RAM 范围: {addr}")
    except OSError as e:
        if result is not None:
            result.details.append(f"读取文件失败: {e}")

    return None


def _extract_rtt_addr_from_text(text: str) -> str | None:
    """从符号工具输出中提取 _SEGGER_RTT 地址。"""
    patterns = [
        r"\b([0-9A-Fa-f]{8})\b\s+[A-Za-z]\s+_SEGGER_RTT(?:\s|$)",
        r"\b([0-9A-Fa-f]{8})\b.*\b_SEGGER_RTT\b",
        r"_SEGGER_RTT\b.*\b(0x[0-9A-Fa-f]{8})\b",
    ]
    for line in text.splitlines():
        if "_SEGGER_RTT" not in line or "SEGGER_RTT_CB" in line:
            continue
        for pattern in patterns:
            match = re.search(pattern, line)
            if not match:
                continue
            addr = match.group(1)
            if not addr.startswith("0x"):
                addr = "0x" + addr
            if _is_valid_ram_addr(int(addr, 16)):
                return addr
    return None


def _diagnose_map_failure(path: Path, result: RTTFindResult) -> None:
    """对 map 文件失败场景给出更可执行的诊断。"""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        result.details.append(f"读取 MAP 文件失败: {e}")
        return

    has_segger_obj = "SEGGER_RTT.o" in content or "SEGGER_RTT_printf.o" in content
    has_rtt_init = "SEGGER_RTT_Init" in content
    has_rtt_symbol = "_SEGGER_RTT" in content

    if has_rtt_symbol:
        result.details.append("MAP 文件包含 _SEGGER_RTT 符号，但当前解析器未能提取地址")
        return

    if has_segger_obj or has_rtt_init:
        result.details.append("已发现 SEGGER RTT 相关对象或函数，但未发现 _SEGGER_RTT 符号")
        result.warnings.append("可能是链接裁剪、MAP 输出格式变化，或应改从 .out/.elf 符号表读取")
        return

    result.details.append("未发现 SEGGER RTT 相关对象或符号")
    result.warnings.append("请确认已运行 rtt-integrate，并重新完整编译生成最新 MAP/OUT 后再试")


def _is_valid_ram_addr(addr: int) -> bool:
    """检查地址是否在常见 RAM 区域范围内。"""
    return (
        0x20000000 <= addr <= 0x3FFFFFFF  # 主 SRAM
        or 0x10000000 <= addr <= 0x1FFFFFFF  # CCM / Backup SRAM
        or 0x30000000 <= addr <= 0x3FFFFFFF  # SRAM4 等
    )
