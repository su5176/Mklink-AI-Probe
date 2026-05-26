"""
MKLink Serial Bridge — RTT 自动启动配置生成。

零外部依赖（仅 stdlib），零内部依赖。
"""

from __future__ import annotations

import re


def generate_autostart_config(
    addr: str,
    size: int,
    channel: int,
    existing_content: str = "",
) -> str:
    """生成 default_config.py 内容（保留已有配置）。

    非破坏性：如果已有 RTTView.start 行则替换，否则追加。
    """
    rtt_line = f"RTTView.start({addr}, {size}, {channel})\n"

    if existing_content and "RTTView.start" in existing_content:
        return re.sub(r"RTTView\.start\([^)]+\)", rtt_line.strip(), existing_content)

    if existing_content:
        return existing_content.rstrip("\n") + "\n" + rtt_line

    return f"""import time
import cmd

# 等待目标板连接（超时 10 秒）
elapsed = 0
while elapsed < 10000:
    idcode = cmd.get_idcode()
    if idcode not in (0, 0xFFFFFFFF):
        break
    time.sleep_ms(500)
    elapsed += 500

if idcode not in (0, 0xFFFFFFFF):
    {rtt_line.strip()}
"""
