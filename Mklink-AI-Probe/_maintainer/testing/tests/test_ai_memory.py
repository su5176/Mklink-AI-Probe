import json

import pytest

from scripts import ai_memory


def test_current_project_memory_stays_within_context_budget():
    data = ai_memory.load_memory()

    assert len(ai_memory.render(data)) <= ai_memory.MAX_HANDOFF_CHARS


def test_project_memory_rejects_oversized_sections(tmp_path, monkeypatch):
    data = json.loads(ai_memory.MEMORY_PATH.read_text(encoding="utf-8"))
    data["verification"] = [
        {"area": f"area-{index}", "result": "result"}
        for index in range(ai_memory.MAX_SECTION_ITEMS["verification"] + 1)
    ]
    memory_path = tmp_path / "project-memory.json"
    memory_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(ai_memory, "MEMORY_PATH", memory_path)

    with pytest.raises(ValueError, match="verification exceeds"):
        ai_memory.load_memory()
