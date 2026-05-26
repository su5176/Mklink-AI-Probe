"""寄存器轮询 — ANSI 实时表格显示。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from mklink.modbus._client import ModbusClient, ModbusError
from mklink.modbus._format import RegisterSpec, registers_to_values, format_value


def _group_consecutive(specs: list[RegisterSpec]) -> list[list[RegisterSpec]]:
    """将连续地址的同类型寄存器分为一组，减少请求次数。"""
    if not specs:
        return []
    groups: list[list[RegisterSpec]] = [[specs[0]]]
    for spec in specs[1:]:
        prev = groups[-1][-1]
        prev_end = prev.addr + prev.reg_count
        if (
            spec.addr == prev_end
            and spec.register_type == prev.register_type
        ):
            groups[-1].append(spec)
        else:
            groups.append([spec])
    return groups


def _move_up(n: int) -> str:
    """ANSI 转义：光标上移 n 行。"""
    return f"\033[{n}A"


def _clear_line() -> str:
    """ANSI 转义：清除当前行。"""
    return "\033[2K"


def poll_registers(
    client: ModbusClient,
    slave: int,
    specs: list[RegisterSpec],
    interval: float = 1.0,
    fmt: str = "dec",
    count: int | None = None,
) -> None:
    """轮询寄存器并显示实时 ANSI 表格。

    Args:
        client: 已连接的 ModbusClient
        slave: 从站地址
        specs: 寄存器规格列表
        interval: 轮询间隔（秒）
        fmt: 显示格式 (dec/hex/bin/float)
        count: 轮询次数，None 为无限
    """
    groups = _group_consecutive(specs)

    # 计算每个 spec 的显示宽度
    name_col = max((len(s.name or f"REG_{s.addr}") for s in specs), default=10)
    type_col = max((len(s.type) for s in specs), default=6)

    # 打印表头
    header = (
        f"{'Address':>8}  "
        f"{'Type':<{type_col}}  "
        f"{'Name':<{name_col}}  "
        f"{'Value':<16}"
    )
    separator = "-" * len(header)

    print(f"从站: {slave}  |  间隔: {interval}s  |  按 Ctrl+C 停止")
    print(separator)
    print(header)
    print(separator)

    poll_count = 0
    error_count = 0
    table_lines = len(specs) + 2  # separator + header + separator
    first = True

    try:
        while count is None or poll_count < count:
            values_map: dict[int, tuple[int | float, RegisterSpec]] = {}

            for group in groups:
                start_addr = group[0].addr
                total_regs = sum(s.reg_count for s in group)
                try:
                    raw = client.read_holding_registers(start_addr, total_regs, slave)
                except ModbusError:
                    error_count += 1
                    raw = [0] * total_regs

                # 将原始寄存器值映射到各个 spec
                offset = 0
                for spec in group:
                    raw_slice = raw[offset : offset + spec.reg_count]
                    converted = registers_to_values(raw_slice, spec.type)
                    values_map[id(spec)] = (converted[0] if converted else 0, spec)
                    offset += spec.reg_count

            # 清除旧表格行
            if not first:
                sys.stdout.write(_move_up(table_lines + 1))
            first = False

            print(separator)
            for spec in specs:
                val, _ = values_map.get(id(spec), (0, spec))
                bit_width = 32 if spec.type in ("uint32", "int32") else 16
                display = format_value(val, fmt, bit_width)
                label = spec.name or f"REG_{spec.addr}"
                print(
                    f"{spec.addr:>8}  "
                    f"{spec.type:<{type_col}}  "
                    f"{label:<{name_col}}  "
                    f"{display:<16}"
                )
            print(f"{separator}  轮询: {poll_count + 1}  错误: {error_count}")

            sys.stdout.flush()
            poll_count += 1

            if count is not None and poll_count >= count:
                break

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n[*] 轮询已停止，共 {poll_count} 次，错误 {error_count} 次")
