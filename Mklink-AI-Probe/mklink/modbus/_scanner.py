"""Modbus 从站地址扫描器。"""

from __future__ import annotations

from typing import Callable

from mklink.modbus._client import ModbusClient, ModbusError, ModbusSlaveError


def scan_slaves(
    client: ModbusClient,
    start_addr: int = 1,
    end_addr: int = 247,
    probe_register: int = 0,
    on_progress: Callable[[int, int, str | None], None] | None = None,
) -> list[int]:
    """顺序扫描 Modbus 从站地址。

    使用 FC03 读取 1 个保持寄存器来探测。即使从站返回异常（如地址非法），
    也说明从站存在。只有完全无响应（超时）才判定为不存在。

    扫描期间自动将底层客户端超时压缩到 0.15 秒、重试设为 0，
    以避免无响应地址长时间阻塞（247 地址全扫约 40 秒）。
    扫描结束后恢复原始参数。

    Args:
        client: 已连接的 ModbusClient
        start_addr: 扫描起始地址
        end_addr: 扫描结束地址
        probe_register: 探测用的寄存器地址
        on_progress: 进度回调 (current_addr, total, result_msg)

    Returns:
        响应的从站地址列表
    """
    # 保存原始参数，切换到快速扫描模式
    raw = client.raw_client
    orig_timeout = raw.comm_params.timeout_connect
    orig_retries = raw.retries
    raw.comm_params.timeout_connect = 0.15
    raw.retries = 0

    found: list[int] = []
    total = end_addr - start_addr + 1

    try:
        for i, addr in enumerate(range(start_addr, end_addr + 1)):
            try:
                client.read_holding_registers(probe_register, 1, slave=addr)
                found.append(addr)
                msg = f"[OK] 从站 {addr} 响应"
            except ModbusSlaveError:
                # 从站返回异常响应（如非法地址），说明从站存在
                found.append(addr)
                msg = f"[OK] 从站 {addr} 存在（返回异常码）"
            except ModbusError:
                msg = None
            except Exception:
                msg = None

            if on_progress:
                on_progress(i + 1, total, msg)
    finally:
        # 恢复原始参数
        raw.comm_params.timeout_connect = orig_timeout
        raw.retries = orig_retries

    return found
