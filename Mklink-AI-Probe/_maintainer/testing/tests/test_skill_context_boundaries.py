import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import pytest

from _maintainer.release.prepare_release import (
    PUBLIC_SKILL_DIRECTORIES,
    _is_public_skill_file,
)


ROOT = Path(__file__).resolve().parents[3]
MAINTAINER_SKILLS = (
    ROOT / "skills" / "maintaining-mklink-ai-probe",
    ROOT / "skills" / "tauri-gui-builder",
)


def _frontmatter(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    _empty, header, _body = text.split("---", 2)
    return {line.strip() for line in header.splitlines() if line.strip()}


def test_maintainer_skills_are_explicit_only_in_codex():
    for skill in MAINTAINER_SKILLS:
        frontmatter = _frontmatter(skill / "SKILL.md")
        openai = (skill / "agents" / "openai.yaml").read_text(encoding="utf-8")

        assert any(line.startswith("description: Maintainer-only") for line in frontmatter)
        assert "allow_implicit_invocation: false" in openai


def test_end_user_skill_remains_implicitly_available():
    openai = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert "allow_implicit_invocation: true" in openai
    assert "$mklink-ai-probe" in openai
    assert "maintain" not in openai.casefold()


def test_user_entry_keeps_a_small_context_budget():
    # Character budgets guard progressive disclosure, not model token estimates.
    _empty, header, body = (ROOT / "SKILL.md").read_text(encoding="utf-8").split("---", 2)
    assert len(header) <= 350
    assert len(body) <= 4500


def test_user_skill_publishes_probe_safety_boundaries():
    entry = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    memory = (ROOT / "references" / "commands-memory.md").read_text(encoding="utf-8")
    flush = (ROOT / "references" / "flush-memory.md").read_text(encoding="utf-8")

    assert "同一下载器、命令口或目标串口同一时刻只" in entry
    assert "最多 **15 个**离散地址" in entry
    assert "快速连续 float VOFA 最多 **16 路**" in entry
    assert "**511 UTF-8 字节**" in entry
    assert "单批总数据最多 **12 KiB**、最多 **8 个地址项**" in entry
    assert "V4 通道为 **0~2**，搜索窗口为 **0~65536 字节**" in entry
    assert "不得拼接 Pika 表达式" in entry
    assert "MCP `rtt_write` 单次最多 **256" in entry
    assert "超限不得自动拆分" in entry
    assert "文件或日志走 YMODEM/串口专用传输" in entry
    assert "禁止拆分" in entry
    assert "`pattern` 是 1~256 UTF-8 字节的字面子串" in entry
    assert "只调用一次 `device_status`" in entry
    assert "`disconnect` → `connect`" in entry
    assert "禁止自动重试" in entry

    assert "精确模式最多 **15 个**" in memory
    assert "安全上限为 **511B**" in memory
    assert "**1~16 路**" in memory
    assert "单批最多 **12 KiB / 8 个地址项**" in memory
    assert "每批总数据量 ≤ 12 KiB" in flush
    assert "地址项数量 ≤ 8" in flush


def test_user_skill_keeps_generated_files_off_the_system_drive():
    entry = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    work_files = (ROOT / "references" / "work-files.md").read_text(encoding="utf-8")

    assert "用户指定的非系统盘" in entry
    assert "目标项目 `.mklink/`" in entry
    assert "不得默认落在 C 盘/系统盘" in work_files


USER_DOCUMENTS = (ROOT / "SKILL.md", ROOT / "README.md", *sorted((ROOT / "references").glob("*.md")))


@pytest.mark.parametrize("document", USER_DOCUMENTS, ids=lambda path: path.name)
def test_user_document_links_stay_in_the_public_package(document):
    # Check every reference, including those reached only via another reference.
    for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (document.parent / unquote(parsed.path)).resolve()
        relative = PurePosixPath(target.relative_to(ROOT.resolve()).as_posix())
        assert target.exists(), f"{document.name}: missing {link}"
        assert _is_public_skill_file(relative) or relative in PUBLIC_SKILL_DIRECTORIES, (
            f"{document.name}: user documentation links to non-user content: {link}"
        )


@pytest.mark.parametrize("document", USER_DOCUMENTS, ids=lambda path: path.name)
def test_user_documents_do_not_invoke_repository_maintenance(document):
    text = document.read_text(encoding="utf-8")
    # Target firmware builds are legitimate user tasks; MKLink product maintenance is not.
    forbidden = re.compile(
        r"\bpytest\b|\bnpx\s+tauri\s+(?:dev|build)\b|\bpyinstaller\b"
        r"|\bnpm\s+(?:ci|install|run\s+(?:build|dev|test))\b"
        r"|(?:scripts[/\\])?(?:ai_memory\.py|build_workspace\.ps1)"
        r"|_maintainer[/\\]|docs/ai/|skills/(?:maintaining-mklink|tauri-gui-builder)",
        re.IGNORECASE,
    )
    assert not forbidden.search(text), f"{document.name}: maintenance instruction in user context"
