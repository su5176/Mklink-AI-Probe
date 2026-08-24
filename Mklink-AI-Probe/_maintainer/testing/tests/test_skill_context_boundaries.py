from pathlib import Path


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
    frontmatter = _frontmatter(ROOT / "SKILL.md")
    openai = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert "allow_implicit_invocation: true" in openai
