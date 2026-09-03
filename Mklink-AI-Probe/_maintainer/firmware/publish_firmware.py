"""Publish UF2 files to the independent GitHub/Gitee firmware channel.

The default input is the repository's MK-Firmware directory. Firmware assets
live in the long-lived ``firmware-assets`` Release; the ``firmware`` branch is
updated last and contains only ``latest.json``.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence
from urllib.parse import quote


REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from mklink.firmware_check import (  # noqa: E402
    FIRMWARE_MANIFEST_SCHEMA,
    Version,
    parse_firmware_filename,
    validate_uf2,
)
from _maintainer.release.publish_update_release import (  # noqa: E402
    _asset_url,
    _gitee_push,
    _run,
    ensure_gitee_release,
    resolve_gitee_token,
    sha256,
    upload_gitee_asset,
    verify_public_asset,
)


FIRMWARE_RELEASE_TAG = "firmware-assets"
FIRMWARE_RELEASE_TITLE = "MKLink Firmware"
FIRMWARE_RELEASE_NOTES = (
    "Independent public UF2 channel for MicroLink and HPMLink probes. "
    "Applications discover the current files through the firmware branch."
)


def _version(value: str) -> Version:
    info = parse_firmware_filename(f"MicroLink_{value}.uf2")
    if info is None:
        raise ValueError(f"invalid firmware version: {value}")
    return info.version


def scan_firmware_directory(root: Path) -> dict[tuple[str, str], dict[str, object]]:
    """Validate all UF2 files and select the newest family/model entries."""
    if not root.is_dir():
        raise FileNotFoundError(f"firmware directory not found: {root}")
    selected: dict[tuple[str, str], dict[str, object]] = {}
    matched = 0
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.casefold() != ".uf2":
            continue
        info = parse_firmware_filename(path.name)
        if info is None:
            raise ValueError(f"unsupported firmware filename: {path.name}")
        blocks = validate_uf2(path)
        matched += 1
        entry: dict[str, object] = {
            "family": info.family,
            "model": info.model,
            "version": info.version_str,
            "name": info.name,
            "size": path.stat().st_size,
            "sha256": sha256(path),
            "uf2_blocks": blocks,
            "path": path,
        }
        key = (info.family, info.model)
        previous = selected.get(key)
        if previous is None or info.version > _version(str(previous["version"])):
            selected[key] = entry
    if matched == 0:
        raise ValueError(f"no supported UF2 files found in {root}")
    return selected


def _public_entry(entry: Mapping[str, object], urls: Mapping[str, str]) -> dict[str, object]:
    return {
        "family": entry["family"],
        "model": entry["model"],
        "version": entry["version"],
        "name": entry["name"],
        "size": entry["size"],
        "sha256": entry["sha256"],
        "uf2_blocks": entry["uf2_blocks"],
        "urls": dict(urls),
    }


def merge_manifest(
    existing: Mapping[str, object] | None,
    local: Mapping[tuple[str, str], Mapping[str, object]],
    urls: Mapping[str, Mapping[str, str]],
    *,
    published_at: str,
) -> tuple[dict[str, object], bool]:
    """Merge local newest files, rejecting rollback and mutable versions."""
    old_entries: dict[tuple[str, str], dict[str, object]] = {}
    if existing is not None:
        if existing.get("schema") != FIRMWARE_MANIFEST_SCHEMA:
            raise ValueError("existing firmware manifest has an unsupported schema")
        values = existing.get("firmwares")
        if not isinstance(values, list):
            raise ValueError("existing firmware manifest has no firmware list")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("existing firmware manifest contains an invalid entry")
            key = (str(value.get("family")), str(value.get("model")))
            old_entries[key] = dict(value)

    merged = dict(old_entries)
    for key, entry in local.items():
        old = old_entries.get(key)
        if old is not None:
            old_version = _version(str(old.get("version")))
            new_version = _version(str(entry["version"]))
            if new_version < old_version:
                raise ValueError(
                    f"firmware rollback rejected for {key[0]}/{key[1]}: "
                    f"{new_version} < {old_version}"
                )
            if new_version == old_version and (
                old.get("sha256") != entry["sha256"]
                or old.get("size") != entry["size"]
            ):
                raise ValueError(
                    f"same firmware version has different content: {entry['name']}"
                )
        merged[key] = _public_entry(entry, urls[str(entry["name"])])

    ordered = [merged[key] for key in sorted(merged)]
    changed = existing is None or existing.get("firmwares") != ordered
    if not changed and existing is not None:
        return dict(existing), False
    return {
        "schema": FIRMWARE_MANIFEST_SCHEMA,
        "published_at": published_at,
        "firmwares": ordered,
    }, True


def load_github_manifest(repo: str) -> dict[str, object] | None:
    """Load the authoritative current manifest; 404 means first publication."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/latest.json?ref=firmware"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).casefold()
        if "404" in detail or "not found" in detail:
            return None
        raise RuntimeError(f"unable to read current firmware manifest: {result.stderr.strip()}")
    envelope = json.loads(result.stdout)
    content = base64.b64decode(str(envelope["content"])).decode("utf-8")
    document = json.loads(content)
    if not isinstance(document, dict):
        raise ValueError("current firmware manifest is not an object")
    return document


def ensure_github_assets(repo: str, files: Sequence[Path]) -> dict[str, str]:
    view = subprocess.run(
        [
            "gh", "release", "view", FIRMWARE_RELEASE_TAG, "--repo", repo,
            "--json", "tagName,name,body,assets",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if view.returncode == 0:
        release = json.loads(view.stdout)
        if release.get("tagName") != FIRMWARE_RELEASE_TAG:
            raise RuntimeError("GitHub returned a conflicting firmware Release")
        existing = {str(item.get("name")): item for item in release.get("assets", [])}
    else:
        _run([
            "gh", "release", "create", FIRMWARE_RELEASE_TAG, "--repo", repo,
            "--title", FIRMWARE_RELEASE_TITLE, "--notes", FIRMWARE_RELEASE_NOTES,
            "--target", "master", "--latest=false",
        ])
        existing = {}

    uploads: list[Path] = []
    urls: dict[str, str] = {}
    for path in files:
        asset = existing.get(path.name)
        url = (
            f"https://github.com/{repo}/releases/download/{FIRMWARE_RELEASE_TAG}/"
            f"{quote(path.name)}"
        )
        if asset is None:
            uploads.append(path)
        else:
            digest = asset.get("digest")
            if asset.get("size") != path.stat().st_size:
                raise RuntimeError(f"conflicting GitHub firmware asset: {path.name}")
            if digest is not None and digest != f"sha256:{sha256(path)}":
                raise RuntimeError(f"conflicting GitHub firmware asset: {path.name}")
            verify_public_asset(
                url=url, expected_sha256=sha256(path), expected_size=path.stat().st_size
            )
        urls[path.name] = url
    if uploads:
        _run([
            "gh", "release", "upload", FIRMWARE_RELEASE_TAG, "--repo", repo,
            *map(str, uploads),
        ])
    for path in uploads:
        verify_public_asset(
            url=urls[path.name],
            expected_sha256=sha256(path),
            expected_size=path.stat().st_size,
        )
    return urls


def ensure_gitee_assets(
    repo: str, token: str, files: Sequence[Path],
) -> dict[str, str]:
    owner, name = repo.split("/", 1)
    release = ensure_gitee_release(
        owner=owner,
        repo=name,
        token=token,
        tag=FIRMWARE_RELEASE_TAG,
        title=FIRMWARE_RELEASE_TITLE,
        notes=FIRMWARE_RELEASE_NOTES,
    )
    urls: dict[str, str] = {}
    for path in files:
        asset = upload_gitee_asset(
            owner=owner, repo=name, token=token, release=release, path=path
        )
        url = _asset_url(asset)
        if not url:
            raise RuntimeError(f"Gitee did not return a URL for {path.name}")
        verify_public_asset(
            url=url, expected_sha256=sha256(path), expected_size=path.stat().st_size
        )
        urls[path.name] = url
    return urls


def publish_manifest_branch(
    document: Mapping[str, object], *, github_repo: str, gitee_repo: str,
    gitee_token: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="mklink-firmware-index-") as directory:
        checkout = Path(directory)
        _run(["git", "init", "--initial-branch=firmware"], cwd=checkout)
        _run(["git", "config", "user.name", "Mklink Firmware Bot"], cwd=checkout)
        _run(["git", "config", "user.email", "firmware@mklink.local"], cwd=checkout)
        (checkout / "latest.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _run(["git", "add", "latest.json"], cwd=checkout)
        _run([
            "git", "commit", "-m",
            f"firmware: publish {len(document['firmwares'])} current images",
        ], cwd=checkout)
        _run([
            "git", "push", "--force", f"https://github.com/{github_repo}.git",
            "firmware:firmware",
        ], cwd=checkout)
        _gitee_push(
            repository=checkout,
            repo=gitee_repo,
            refspec="firmware:firmware",
            token=gitee_token,
            force=True,
        )


def publish_firmware(
    *, root: Path, github_repo: str, gitee_repo: str, token: str,
    dry_run: bool = False,
) -> dict[str, object]:
    local = scan_firmware_directory(root)
    files = [Path(entry["path"]) for entry in local.values()]
    existing = load_github_manifest(github_repo)
    if dry_run:
        existing_urls = {
            str(entry.get("name")): entry.get("urls")
            for entry in (existing or {}).get("firmwares", [])
            if isinstance(entry, dict) and isinstance(entry.get("urls"), dict)
        }
        placeholder_urls = {}
        for path in files:
            old_urls = existing_urls.get(path.name, {})
            placeholder_urls[path.name] = {
                "github": (
                    f"https://github.com/{github_repo}/releases/download/"
                    f"{FIRMWARE_RELEASE_TAG}/{quote(path.name)}"
                ),
                "gitee": str(
                    old_urls.get("gitee")
                    or f"https://gitee.example/{path.name}"
                ),
            }
        document, changed = merge_manifest(
            existing, local, placeholder_urls,
            published_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        return {"manifest": document, "changed": changed, "published": False}

    # Validate rollback and same-version immutability before creating or
    # uploading any remote asset.  Asset publication is intentionally
    # append-only, so a rejected manifest must not leave orphaned files in the
    # long-lived firmware Release.
    preflight_urls = {
        path.name: {
            "github": (
                f"https://github.com/{github_repo}/releases/download/"
                f"{FIRMWARE_RELEASE_TAG}/{quote(path.name)}"
            ),
            "gitee": (
                f"https://gitee.com/{gitee_repo}/releases/download/"
                f"{FIRMWARE_RELEASE_TAG}/{quote(path.name)}"
            ),
        }
        for path in files
    }
    merge_manifest(
        existing,
        local,
        preflight_urls,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )

    github_urls = ensure_github_assets(github_repo, files)
    gitee_urls = ensure_gitee_assets(gitee_repo, token, files)
    urls = {
        path.name: {"github": github_urls[path.name], "gitee": gitee_urls[path.name]}
        for path in files
    }
    document, changed = merge_manifest(
        existing,
        local,
        urls,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    if changed:
        publish_manifest_branch(
            document,
            github_repo=github_repo,
            gitee_repo=gitee_repo,
            gitee_token=token,
        )
    return {"manifest": document, "changed": changed, "published": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-dir", type=Path, default=REPOSITORY / "MK-Firmware")
    parser.add_argument("--github-repo", default="Aladdin-Wang/Mklink-AI-Probe")
    parser.add_argument("--gitee-repo", default="Aladdin-Wang/Mklink-AI-Probe")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = "" if args.dry_run else resolve_gitee_token()
    result = publish_firmware(
        root=args.firmware_dir.resolve(),
        github_repo=args.github_repo,
        gitee_repo=args.gitee_repo,
        token=token,
        dry_run=args.dry_run,
    )
    manifest = result["manifest"]
    print(json.dumps({
        "published": result["published"],
        "changed": result["changed"],
        "firmwares": len(manifest["firmwares"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
