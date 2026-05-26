"""
MKLink Serial Bridge — RTT 会话管理。

依赖: 无外部依赖
内部依赖: mklink.bridge, mklink._types
"""

from __future__ import annotations

import re

from mklink._types import DeviceState
from mklink.bridge import MKLinkSerialBridge


class RTTSession:
    """RTT 会话管理器。"""

    def __init__(self, bridge: MKLinkSerialBridge, channel: int = 0):
        self._bridge = bridge
        self._channel = channel
        self._running = False

    @staticmethod
    def _find_rtt_addr_from_config(project_root: str = ".") -> str | None:
        """从 .mklink/rtt_config.json 读取已保存的 RTT 地址."""
        import json
        from pathlib import Path

        config_path = Path(project_root) / ".mklink" / "rtt_config.json"
        if not config_path.exists():
            return None

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            addr = config.get("rtt_addr", "")
            if addr:
                return addr
        except (json.JSONDecodeError, IOError):
            pass
        return None

    def start(self, addr: str, search_size: int = 1024, project_root: str = ".") -> dict:
        """启动 RTT 并解析缓冲区配置。

        注意：先在 READY 状态下发送 RTTView.start 命令（send_command 要求 READY），
        成功后再切换到 RTT_STREAM 流模式。

        如果传入的 addr 为空或 None，会自动从 .mklink/rtt_config.json 读取已保存的地址。

        返回:
            dict: {
                "control_block_addr": str,
                "up_buffers": [{"channel": int, "size": int, "mode": int, "active": bool,
                                "name": str}],
                "down_buffers": [{"channel": int, "size": int, "mode": int, "active": bool,
                                  "name": str}],
            }
        """
        # 如果未指定地址，尝试从项目配置读取
        if not addr:
            addr = self._find_rtt_addr_from_config(project_root)
            if addr:
                print(f"[OK] 从配置读取 RTT 地址: {addr}")
            else:
                addr = "0x20000000"  # 默认搜索地址
                print(f"[WARN] 未找到 RTT 配置，使用默认搜索地址: {addr}")

        # 先在 READY 状态发送命令
        cmd = f"RTTView.start({addr},{search_size},{self._channel})"
        resp = self._bridge.send_command(cmd, timeout=10.0)

        result = self._parse_rtt_startup(resp)

        # 检查是否成功找到 RTT 控制块
        if not result.get("control_block_addr"):
            return result  # 失败时不切换到流模式

        # 命令成功，切换到 RTT_STREAM 流模式
        self._bridge._enter_stream(DeviceState.RTT_STREAM)
        self._running = True
        return result

    def read_output(self, duration: float = 10.0, callback=None) -> str:
        """读取 UpBuffer 输出 (MCU -> PC)。"""
        return self._bridge.read_stream(duration=duration)

    def send_input(self, data: bytes) -> bool:
        """通过 DownBuffer 发送数据到 MCU (PC -> MCU)。

        RTT 启动后直接通过 CDC 串口发送，不需要 PikaScript 命令。
        """
        self._bridge._write_raw(data)
        return True

    def stop(self) -> str:
        """停止 RTT 会话。"""
        # 先恢复 READY 状态以便 send_command 工作
        remaining = self._bridge._exit_stream()
        try:
            self._bridge.send_command("RTTView.stop()", timeout=5.0)
        except (ConnectionError, TimeoutError):
            pass  # 即使停止失败也继续恢复状态
        self._running = False
        return remaining

    @staticmethod
    def _parse_rtt_startup(output: str) -> dict:
        """解析 RTT 启动输出，提取控制块地址和缓冲区信息。"""
        result = {
            "control_block_addr": "",
            "up_buffers": [],
            "down_buffers": [],
        }

        # 提取控制块地址
        addr_match = re.search(
            r'Find SEGGER RTT addr\s+(0x[0-9a-fA-F]+)', output
        )
        if addr_match:
            result["control_block_addr"] = addr_match.group(1)

        # 支持 "Addr = 0x..., wSize = ..., Channel = ..." 前缀格式
        alt_match = re.search(r'Addr\s*=\s*(0x[0-9a-fA-F]+)', output)
        if alt_match and not result["control_block_addr"]:
            result["control_block_addr"] = alt_match.group(1)

        # 动态解析 UpBuffer/DownBuffer 通道（不硬编码数量）
        # 支持可选的 Name 字段: "... Mode: N Name: xxx"
        for m in re.finditer(
            r'UpBuffer\s+Channel\s+(\d+)\s+Size:\s+(\d+)\s+Mode:\s+(\d+)'
            r'(?:\s+Name:\s*(\S+))?',
            output,
        ):
            ch, size, mode = int(m.group(1)), int(m.group(2)), int(m.group(3))
            name = m.group(4) or ""
            result["up_buffers"].append({
                "channel": ch,
                "size": size,
                "mode": mode,
                "active": size > 0,
                "name": name,
            })

        for m in re.finditer(
            r'DownBuffer\s+Channel\s+(\d+)\s+Size:\s+(\d+)\s+Mode:\s+(\d+)'
            r'(?:\s+Name:\s*(\S+))?',
            output,
        ):
            ch, size, mode = int(m.group(1)), int(m.group(2)), int(m.group(3))
            name = m.group(4) or ""
            result["down_buffers"].append({
                "channel": ch,
                "size": size,
                "mode": mode,
                "active": size > 0,
                "name": name,
            })

        return result
