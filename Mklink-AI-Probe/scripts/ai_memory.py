"""Validate and render the repository's cross-model project memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MEMORY_PATH = ROOT / "docs" / "ai" / "project-memory.json"
HANDOFF_PATH = ROOT / "docs" / "ai" / "CURRENT_HANDOFF.md"
REQUIRED = {
    "schema_version",
    "updated_at",
    "repository",
    "mission",
    "current_session",
    "milestones",
    "verification",
    "decisions",
    "known_limits",
    "next_actions",
}
MAX_SECTION_ITEMS = {
    "milestones": 6,
    "verification": 8,
    "decisions": 10,
    "known_limits": 8,
    "next_actions": 5,
    "continuation_protocol": 5,
}
MAX_ENTRY_CHARS = 1200
MAX_HANDOFF_CHARS = 10000


def load_memory() -> dict[str, Any]:
    data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    missing = sorted(REQUIRED - data.keys())
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    if data["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    if not data["next_actions"]:
        raise ValueError("next_actions must not be empty")
    for section, limit in MAX_SECTION_ITEMS.items():
        entries = data.get(section, [])
        if not isinstance(entries, list):
            raise ValueError(f"{section} must be a list")
        if len(entries) > limit:
            raise ValueError(f"{section} exceeds the {limit}-item context budget")
        for entry in entries:
            serialized = json.dumps(entry, ensure_ascii=False)
            if len(serialized) > MAX_ENTRY_CHARS:
                raise ValueError(
                    f"{section} entry exceeds the {MAX_ENTRY_CHARS}-character context budget"
                )
    if len(render(data)) > MAX_HANDOFF_CHARS:
        raise ValueError(
            f"rendered handoff exceeds the {MAX_HANDOFF_CHARS}-character context budget"
        )
    return data


def render(data: dict[str, Any]) -> str:
    repo = data["repository"]
    session = data["current_session"]
    lines = [
        "# 当前 AI 交接",
        "",
        "> 本文件由 `python scripts/ai_memory.py render` 根据 `project-memory.json` 生成。",
        "",
        "## 当前断点",
        "",
        f"- 更新时间：`{data['updated_at']}`",
        f"- 分支：`{repo.get('branch', '')}`",
        f"- HEAD：`{repo.get('head', '')}`",
        f"- 远端 HEAD：`{repo.get('remote_head', '')}`",
        f"- 工作树：{repo.get('working_tree', '')}",
        f"- 当前任务：{session.get('current_task', '')}",
        f"- 状态：`{session.get('status', '')}`",
        "",
        "## 里程碑",
        "",
    ]
    for item in data["milestones"]:
        detail = item.get("notes") or item.get("tests") or item.get("evidence", "")
        lines.append(f"- **{item['name']}** — `{item['status']}`。{detail}")
    lines.extend(["", "## 验证证据", ""])
    for item in data["verification"]:
        lines.append(f"- **{item['area']}**：{item['result']}")
    lines.extend(["", "## 架构决策", ""])
    for item in data["decisions"]:
        lines.append(f"- {item}")
    hardware = data.get("hardware", {})
    lines.extend(["", "## 真机环境", ""])
    for key, value in hardware.items():
        lines.append(f"- **{key}**：{value}")
    lines.extend(["", "## 下一动作", ""])
    for index, action in enumerate(data["next_actions"], 1):
        lines.append(f"{index}. {action}")
    lines.extend(["", "## 已知限制", ""])
    for item in data["known_limits"]:
        lines.append(f"- {item}")
    lines.extend(["", "## 延续协议", ""])
    for item in data.get("continuation_protocol", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "render"))
    args = parser.parse_args()
    data = load_memory()
    if args.command == "render":
        with HANDOFF_PATH.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(render(data))
        print(f"rendered {HANDOFF_PATH.relative_to(ROOT)}")
    else:
        print(f"valid project memory v{data['schema_version']}: {data['updated_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
