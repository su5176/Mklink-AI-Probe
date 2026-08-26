import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "release"
    / "prepare_release.py"
)


@pytest.fixture
def release_module():
    spec = importlib.util.spec_from_file_location("mklink_prepare_release", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_inputs(tmp_path):
    nsis = tmp_path / "input.exe"
    signature = tmp_path / "input.exe.sig"
    nsis.write_bytes(b"exe")
    signature.write_text("signature", encoding="ascii")
    skill = tmp_path / "skill.zip"
    with zipfile.ZipFile(skill, "w") as archive:
        root = "Mklink-AI-Probe-v0.1.0"
        archive.writestr(f"{root}/pyproject.toml", '[project]\nversion = "0.1.0"\n')
        archive.writestr(f"{root}/SKILL.md", "# Skill\n")
        archive.writestr(
            f"{root}/.claude-plugin/plugin.json",
            json.dumps({"version": "0.1.0"}),
        )
        archive.writestr(f"{root}/scripts/skill_update.py", "# updater\n")
        archive.writestr(f"{root}/scripts/win_usb_rename.ps1", "# rename\n")

    portable = tmp_path / "MKLink-Site-Agent-v0.1.0-windows-x86_64-portable.zip"
    portable.write_bytes(b"portable")
    portable_manifest = tmp_path / "portable.manifest.json"
    portable_manifest.write_text(
        json.dumps({
            "bundle": {"version": "0.1.0"},
            "artifact": {
                "name": portable.name,
                "size": portable.stat().st_size,
                "sha256": release_module_sha256(portable),
            },
        }),
        encoding="utf-8",
    )
    return nsis, signature, skill, portable, portable_manifest


def release_module_sha256(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepare_release_copies_named_assets_and_hashes_them(release_module, tmp_path):
    nsis, signature, skill, portable, portable_manifest = release_inputs(tmp_path)
    output = tmp_path / "release"

    result = release_module.prepare_release(
        version="0.1.0",
        source_commit="a" * 40,
        output_dir=output,
        nsis=nsis,
        updater_signature=signature,
        skill_archive=skill,
        site_agent_archive=portable,
        site_agent_manifest=portable_manifest,
    )

    assert {asset["name"] for asset in result["assets"]} == {
        "Mklink-AI-Probe-v0.1.0-x64-Setup.exe",
        "Mklink-AI-Probe-v0.1.0-x64-Setup.exe.sig",
        "Mklink-AI-Probe-v0.1.0-Skill.zip",
        "MKLink-Site-Agent-v0.1.0-windows-x86_64-portable.zip",
        "MKLink-Site-Agent-v0.1.0-windows-x86_64-portable.manifest.json",
    }
    assert all(len(asset["sha256"]) == 64 for asset in result["assets"])
    assert all(set(asset) == {"name", "size", "sha256"} for asset in result["assets"])
    assert (output / "release-manifest.json").is_file()
    assets_by_name = sorted(result["assets"], key=lambda asset: asset["name"].casefold())
    assert (output / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines() == [
        f'{asset["sha256"]}  {asset["name"]}' for asset in assets_by_name
    ]
    manifest_text = (output / "release-manifest.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in manifest_text
    assert json.loads(manifest_text) == result


def test_prepare_release_rejects_missing_inputs(release_module, tmp_path):
    _nsis, signature, skill, portable, portable_manifest = release_inputs(tmp_path)
    with pytest.raises(FileNotFoundError, match="release input does not exist"):
        release_module.prepare_release(
            version="0.1.0",
            source_commit="a" * 40,
            output_dir=tmp_path / "release",
            nsis=tmp_path / "missing.exe",
            updater_signature=signature,
            skill_archive=skill,
            site_agent_archive=portable,
            site_agent_manifest=portable_manifest,
        )


def test_prepare_release_rejects_nested_repository_skill_layout(
    release_module, tmp_path,
):
    nsis, signature, _skill, portable, portable_manifest = release_inputs(tmp_path)
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested, "w") as archive:
        root = "Mklink-AI-Probe-v0.1.0/Mklink-AI-Probe"
        archive.writestr(f"{root}/pyproject.toml", '[project]\nversion = "0.1.0"\n')
        archive.writestr(f"{root}/SKILL.md", "# Skill\n")
        archive.writestr(
            f"{root}/.claude-plugin/plugin.json",
            json.dumps({"version": "0.1.0"}),
        )
        archive.writestr(f"{root}/scripts/skill_update.py", "# updater\n")
        archive.writestr(f"{root}/scripts/win_usb_rename.ps1", "# rename\n")

    with pytest.raises(ValueError, match="directly contain"):
        release_module.prepare_release(
            version="0.1.0",
            source_commit="a" * 40,
            output_dir=tmp_path / "release",
            nsis=nsis,
            updater_signature=signature,
            skill_archive=nested,
            site_agent_archive=portable,
            site_agent_manifest=portable_manifest,
        )


def test_public_skill_archive_excludes_repository_maintenance(
    release_module, tmp_path,
):
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=release_module.REPO_ROOT,
        text=True,
    ).strip()
    archive_path = tmp_path / "public-skill.zip"

    release_module._build_skill_archive(
        version="0.1.8",
        source_commit=source_commit,
        output=archive_path,
    )

    with zipfile.ZipFile(archive_path) as archive:
        root = "Mklink-AI-Probe-v0.1.8/"
        files = {
            info.filename.removeprefix(root)
            for info in archive.infolist()
            if not info.is_dir()
        }
    assert {
        "SKILL.md",
        "agents/openai.yaml",
        "gui/dist/index.html",
        "scripts/skill_update.py",
        "scripts/win_usb_rename.ps1",
    } <= files
    assert not any(
        path == name or path.startswith(f"{name}/")
        for path in files
        for name in (
            "_maintainer",
            "commands",
            "docs",
            "native",
            "skills",
            "site-agent-gui",
        )
    )
    assert {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}.isdisjoint(files)
    assert not any(path.startswith("MK-Firmware/") for path in files)


def test_public_skill_allowlist_includes_only_bundled_runtime_scripts(release_module):
    assert release_module._is_public_skill_file(
        release_module.PurePosixPath("scripts/skill_update.py")
    )
    assert release_module._is_public_skill_file(
        release_module.PurePosixPath("scripts/win_usb_rename.ps1")
    )
    assert not release_module._is_public_skill_file(
        release_module.PurePosixPath("scripts/ai_memory.py")
    )


def test_prepare_release_rejects_maintenance_content(
    release_module, tmp_path,
):
    nsis, signature, skill, portable, portable_manifest = release_inputs(tmp_path)
    with zipfile.ZipFile(skill, "a") as archive:
        archive.writestr(
            "Mklink-AI-Probe-v0.1.0/skills/maintaining-mklink-ai-probe/SKILL.md",
            "maintainer only",
        )

    with pytest.raises(ValueError, match="non-user content"):
        release_module.prepare_release(
            version="0.1.0",
            source_commit="a" * 40,
            output_dir=tmp_path / "release",
            nsis=nsis,
            updater_signature=signature,
            skill_archive=skill,
            site_agent_archive=portable,
            site_agent_manifest=portable_manifest,
        )
