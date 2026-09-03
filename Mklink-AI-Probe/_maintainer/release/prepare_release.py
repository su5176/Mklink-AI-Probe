"""Prepare sanitized, checksummed MKLink release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SKILL_DIRECTORIES = (
    PurePosixPath(".claude-plugin"),
    PurePosixPath("agents"),
    PurePosixPath("gui/dist"),
    PurePosixPath("mklink"),
    PurePosixPath("references"),
)
PUBLIC_SKILL_FILES = {
    PurePosixPath(".mcp.json"),
    PurePosixPath("pyproject.toml"),
    PurePosixPath("README.md"),
    PurePosixPath("scripts/skill_update.py"),
    PurePosixPath("scripts/win_usb_rename.ps1"),
    PurePosixPath("SKILL.md"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_versions() -> dict[str, str]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.9/3.10 fallback
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

    python_package = "unknown"
    if tomllib is not None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as stream:
            python_package = str(tomllib.load(stream)["project"]["version"])
    tauri_config = json.loads(
        (REPO_ROOT / "gui" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "python_package": python_package,
        "tauri": str(tauri_config["version"]),
        "build_python": platform.python_version(),
    }


def _require_file(value: Path | str) -> Path:
    path = Path(value).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"release input does not exist: {path.name}")
    return path


def _is_public_skill_file(relative: PurePosixPath) -> bool:
    return relative in PUBLIC_SKILL_FILES or any(
        directory in relative.parents for directory in PUBLIC_SKILL_DIRECTORIES
    )


def _validate_skill_archive(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        files = {
            PurePosixPath(info.filename)
            for info in archive.infolist()
            if not info.is_dir()
        }
        if not files or any(
            member.is_absolute() or ".." in member.parts for member in files
        ):
            raise ValueError("Skill archive contains an unsafe or empty layout")
        roots = {member.parts[0] for member in files if member.parts}
        if len(roots) != 1:
            raise ValueError("Skill archive must contain exactly one root directory")
        root = next(iter(roots))
        required = {
            PurePosixPath(root, "pyproject.toml"),
            PurePosixPath(root, "SKILL.md"),
            PurePosixPath(root, ".claude-plugin", "plugin.json"),
            PurePosixPath(root, "scripts", "skill_update.py"),
            PurePosixPath(root, "scripts", "win_usb_rename.ps1"),
        }
        if not required <= files:
            raise ValueError(
                "Skill archive root must directly contain the installable project"
            )
        root_path = PurePosixPath(root)
        unexpected = sorted(
            str(member.relative_to(root_path))
            for member in files
            if not _is_public_skill_file(member.relative_to(root_path))
        )
        if unexpected:
            raise ValueError(
                f"Skill archive contains non-user content: {unexpected[0]}"
            )
        plugin_path = PurePosixPath(root, ".claude-plugin", "plugin.json")
        plugin = json.loads(archive.read(str(plugin_path)))
        if str(plugin.get("version")) != version:
            raise ValueError("Skill archive plugin version does not match the release")


def _build_skill_archive(
    *, version: str, source_commit: str, output: Path,
) -> Path:
    git_root = Path(subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--show-toplevel"],
        text=True,
    ).strip()).resolve()
    source_relative = REPO_ROOT.relative_to(git_root)
    treeish = source_commit
    if source_relative.parts:
        treeish = f"{source_commit}:{source_relative.as_posix()}"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "git",
        "-C",
        str(git_root),
        "archive",
        "--format=zip",
        f"--prefix=Mklink-AI-Probe-v{version}/",
        f"--output={output}",
        treeish,
        "--",
        *(str(path) for path in PUBLIC_SKILL_DIRECTORIES),
        *(str(path) for path in sorted(PUBLIC_SKILL_FILES)),
    ]
    try:
        subprocess.run(command, check=True)
        _validate_skill_archive(output, version)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def _validate_site_agent_portable(
    archive: Path,
    manifest_path: Path,
    version: str,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_name = f"MKLink-Site-Agent-v{version}-windows-x86_64-portable.zip"
    if archive.name != expected_name:
        raise ValueError("Site Agent portable archive name does not match the release")
    bundle = manifest.get("bundle")
    artifact = manifest.get("artifact")
    if not isinstance(bundle, dict) or str(bundle.get("version")) != version:
        raise ValueError("Site Agent portable version does not match the release")
    if not isinstance(artifact, dict) or artifact.get("name") != archive.name:
        raise ValueError("Site Agent portable manifest has an unexpected artifact")
    if artifact.get("size") != archive.stat().st_size or artifact.get("sha256") != _sha256(archive):
        raise ValueError("Site Agent portable manifest hash or size mismatch")


def prepare_release(
    *,
    version: str,
    source_commit: str,
    output_dir: Path | str,
    nsis: Path | str,
    updater_signature: Path | str,
    site_agent_archive: Path | str,
    site_agent_manifest: Path | str,
    skill_archive: Path | str | None = None,
) -> dict[str, object]:
    if not version or any(separator in version for separator in ("/", "\\")):
        raise ValueError("release version must be a path-safe value")
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdefABCDEF" for character in source_commit
    ):
        raise ValueError("source commit must be a 40-character hexadecimal SHA")

    site_agent_source = _require_file(site_agent_archive)
    site_agent_manifest_source = _require_file(site_agent_manifest)
    _validate_site_agent_portable(
        site_agent_source,
        site_agent_manifest_source,
        version,
    )
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    skill_name = f"Mklink-AI-Probe-v{version}-Skill.zip"
    if skill_archive is None:
        skill_source = _build_skill_archive(
            version=version,
            source_commit=source_commit,
            output=output / skill_name,
        )
    else:
        skill_source = _require_file(skill_archive)
        _validate_skill_archive(skill_source, version)
    sources = [
        (_require_file(nsis), f"Mklink-AI-Probe-v{version}-x64-Setup.exe"),
        (
            _require_file(updater_signature),
            f"Mklink-AI-Probe-v{version}-x64-Setup.exe.sig",
        ),
        (
            skill_source,
            skill_name,
        ),
        (
            site_agent_source,
            f"MKLink-Site-Agent-v{version}-windows-x86_64-portable.zip",
        ),
        (
            site_agent_manifest_source,
            f"MKLink-Site-Agent-v{version}-windows-x86_64-portable.manifest.json",
        ),
    ]

    names: set[str] = set()
    for _source, name in sources:
        folded = name.casefold()
        if folded in names:
            raise ValueError(f"duplicate release asset name: {name}")
        names.add(folded)

    assets = []
    for source, name in sorted(sources, key=lambda item: item[1].casefold()):
        destination = output / name
        if source.resolve() != destination:
            shutil.copy2(source, destination)
        assets.append(
            {
                "name": name,
                "size": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )

    manifest: dict[str, object] = {
        "schema_version": 1,
        "release_version": version,
        "source_commit": source_commit.lower(),
        "build_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "versions": _source_versions(),
        "assets": assets,
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    checksum_lines = [
        f'{asset["sha256"]}  {asset["name"]}'
        for asset in sorted(assets, key=lambda item: str(item["name"]).casefold())
    ]
    (output / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n", encoding="ascii"
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--nsis", required=True, type=Path)
    parser.add_argument("--updater-signature", required=True, type=Path)
    parser.add_argument(
        "--skill-archive",
        type=Path,
        help="prebuilt public Skill archive; omitted to build the sanitized archive from source-commit",
    )
    parser.add_argument("--site-agent-archive", required=True, type=Path)
    parser.add_argument("--site-agent-manifest", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = prepare_release(
        version=args.version,
        source_commit=args.source_commit,
        output_dir=args.output,
        nsis=args.nsis,
        updater_signature=args.updater_signature,
        skill_archive=args.skill_archive,
        site_agent_archive=args.site_agent_archive,
        site_agent_manifest=args.site_agent_manifest,
    )
    print(json.dumps({
        "release_version": manifest["release_version"],
        "asset_count": len(manifest["assets"]),
        "output": str(args.output.resolve()),
    }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
