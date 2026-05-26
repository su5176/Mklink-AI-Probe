"""寄存器值格式化与类型转换工具。"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class RegisterSpec:
    """轮询时的寄存器规格。"""

    addr: int
    type: str = "uint16"  # uint16, int16, uint32, int32, float
    name: str = ""
    register_type: str = "holding"  # holding 或 input

    @property
    def reg_count(self) -> int:
        """该类型占用的 16 位寄存器数量。"""
        return 2 if self.type in ("uint32", "int32", "float") else 1


def format_value(value: int | float, fmt: str = "dec", bit_width: int = 16) -> str:
    """格式化单个寄存器值。"""
    if fmt == "hex":
        if isinstance(value, float):
            return f"0x{struct.unpack('>H', struct.pack('>H', int(value) & 0xFFFF))[0]:04X}"
        return f"0x{value & ((1 << bit_width) - 1):0{bit_width // 4}X}"
    elif fmt == "bin":
        if isinstance(value, float):
            return "N/A"
        return f"{value & ((1 << bit_width) - 1):0{bit_width}b}"
    elif fmt == "float":
        if isinstance(value, float):
            return f"{value:.4f}"
        return f"{value}"
    else:  # dec
        return f"{value}"


def registers_to_values(registers: list[int], data_type: str) -> list[int | float]:
    """将原始 16 位寄存器列表转换为指定类型的值列表。

    32 位类型（uint32/int32/float）每两个寄存器合并为一个值（大端序）。
    """
    if data_type == "uint16":
        return list(registers)
    elif data_type == "int16":
        return [_to_int16(v) for v in registers]
    elif data_type == "uint32":
        return [_to_uint32(registers[i], registers[i + 1]) for i in range(0, len(registers) - 1, 2)]
    elif data_type == "int32":
        return [_to_int32(registers[i], registers[i + 1]) for i in range(0, len(registers) - 1, 2)]
    elif data_type == "float":
        return [_to_float(registers[i], registers[i + 1]) for i in range(0, len(registers) - 1, 2)]
    else:
        return list(registers)


def format_registers(
    registers: list[int],
    data_type: str = "uint16",
    fmt: str = "dec",
) -> list[str]:
    """格式化寄存器列表为显示字符串列表。"""
    values = registers_to_values(registers, data_type)
    bit_width = 32 if data_type in ("uint32", "int32") else 16
    return [format_value(v, fmt, bit_width) for v in values]


def parse_register_spec(spec_str: str) -> list[RegisterSpec]:
    """解析寄存器规格字符串为 RegisterSpec 列表。

    格式: "addr:type[:name]" 空格分隔
    示例: "0:uint16:Temp 1:uint16 2:float:Voltage"
    """
    specs = []
    for part in spec_str.strip().split():
        fields = part.split(":")
        addr = int(fields[0], 0)
        dtype = fields[1] if len(fields) > 1 else "uint16"
        name = fields[2] if len(fields) > 2 else ""
        specs.append(RegisterSpec(addr=addr, type=dtype, name=name))
    return specs


# ---- 内部转换函数 ----

def _to_int16(v: int) -> int:
    return v - 0x10000 if v >= 0x8000 else v


def _to_uint32(hi: int, lo: int) -> int:
    return (hi << 16) | lo


def _to_int32(hi: int, lo: int) -> int:
    v = _to_uint32(hi, lo)
    return v - 0x100000000 if v >= 0x80000000 else v


def _to_float(hi: int, lo: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
