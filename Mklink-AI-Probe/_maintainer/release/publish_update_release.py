"""Publish a signed MKLink update to GitHub, Gitee, and the updates branch."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib


GITEE_API = "https://gitee.com/api/v5"


class GiteeApiError(RuntimeError):
    def __init__(self, status: int, path: str):
        super().__init__(f"Gitee API returned HTTP {status} for {path}")
        self.status = status


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(repository: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repository).stdout.strip()


def _release_names(version: str) -> dict[str, str]:
    prefix = f"Mklink-AI-Probe-v{version}-x64"
    return {
        "setup": f"{prefix}-Setup.exe",
        "updater_signature": f"{prefix}-Setup.exe.sig",
        "skill": f"Mklink-AI-Probe-v{version}-Skill.zip",
        "site_agent": f"MKLink-Site-Agent-v{version}-windows-x86_64-portable.zip",
        "site_agent_manifest": f"MKLink-Site-Agent-v{version}-windows-x86_64-portable.manifest.json",
        "checksums": "SHA256SUMS.txt",
        "manifest": "release-manifest.json",
    }


def validate_release_preflight(
    *, repository: Path, release_dir: Path, version: str,
    updater_installer: Path, updater_signature: Path,
) -> list[Path]:
    if git_output(repository, "branch", "--show-current") != "master":
        raise RuntimeError("release publication must run from the master branch")
    if git_output(repository, "status", "--porcelain"):
        raise RuntimeError("release publication requires a clean working tree")
    head = git_output(repository, "rev-parse", "HEAD")

    with (repository / "pyproject.toml").open("rb") as stream:
        python_version = str(tomllib.load(stream)["project"]["version"])
    with (repository / "gui" / "src-tauri" / "Cargo.toml").open("rb") as stream:
        cargo_version = str(tomllib.load(stream)["package"]["version"])
    tauri_version = str(json.loads(
        (repository / "gui" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )["version"])
    if {python_version, cargo_version, tauri_version} != {version}:
        raise RuntimeError("release version does not match project metadata")

    names = _release_names(version)
    expected_files = {release_dir / name for name in names.values()}
    actual_files = {path for path in release_dir.iterdir() if path.is_file()}
    if actual_files != expected_files:
        raise RuntimeError("release directory has an unexpected public asset set")
    if updater_installer.resolve() != (release_dir / names["setup"]).resolve():
        raise RuntimeError("updater installer filename does not match the release version")
    if updater_signature.resolve() != (release_dir / names["updater_signature"]).resolve():
        raise RuntimeError("updater signature filename does not match the release version")

    manifest = json.loads((release_dir / names["manifest"]).read_text(encoding="utf-8"))
    if manifest.get("release_version") != version or manifest.get("source_commit") != head:
        raise RuntimeError("release manifest version or source commit does not match HEAD")
    expected_payload_names = {
        names["setup"], names["updater_signature"], names["skill"],
        names["site_agent"], names["site_agent_manifest"],
    }
    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) != 5 or not all(
        isinstance(value, dict) for value in assets
    ) or {
        value.get("name") for value in assets if isinstance(value, dict)
    } != expected_payload_names:
        raise RuntimeError("release manifest has an unexpected asset set")
    checksum_lines = []
    for asset in sorted(assets, key=lambda value: str(value["name"]).casefold()):
        path = release_dir / str(asset["name"])
        if path.stat().st_size != asset.get("size") or sha256(path) != asset.get("sha256"):
            raise RuntimeError(f"release asset hash or size mismatch: {path.name}")
        checksum_lines.append(f'{asset["sha256"]}  {asset["name"]}')
    actual_checksums = (release_dir / names["checksums"]).read_text(
        encoding="ascii"
    ).splitlines()
    if actual_checksums != checksum_lines:
        raise RuntimeError("SHA256SUMS.txt does not match the release manifest")
    portable_manifest = json.loads(
        (release_dir / names["site_agent_manifest"]).read_text(encoding="utf-8")
    )
    portable_artifact = portable_manifest.get("artifact")
    portable = release_dir / names["site_agent"]
    if not isinstance(portable_artifact, dict) or portable_artifact != {
        "name": portable.name,
        "sha256": sha256(portable),
        "size": portable.stat().st_size,
    }:
        raise RuntimeError("Site Agent portable manifest does not match its archive")
    return [release_dir / names[key] for key in (
        "setup", "updater_signature", "skill", "site_agent",
        "site_agent_manifest", "checksums", "manifest"
    )]


def build_latest_document(
    *, version: str, notes: str, published_at: str, signature: str,
    updater_url: str, updater_sha256: str, updater_size: int,
    skill_url: str, skill_sha256: str, skill_size: int, source_commit: str,
) -> dict[str, object]:
    return {
        "version": version,
        "notes": notes,
        "pub_date": published_at,
        "platforms": {
            "windows-x86_64": {
                "signature": signature,
                "url": updater_url,
                "sha256": updater_sha256,
                "size": updater_size,
            }
        },
        "skill": {
            "version": version,
            "url": skill_url,
            "sha256": skill_sha256,
            "size": skill_size,
            "source_commit": source_commit,
        },
    }


def build_gitee_request(
    *,
    method: str,
    path: str,
    token: str,
    payload: Mapping[str, object] | None = None,
    data: bytes | None = None,
    content_type: str = "application/x-www-form-urlencoded",
) -> urllib.request.Request:
    if payload is not None and data is not None:
        raise ValueError("payload and data are mutually exclusive")
    body = data
    if payload is not None:
        normalized = {
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in payload.items()
        }
        body = urllib.parse.urlencode(normalized).encode("utf-8")
    query = urllib.parse.urlencode({"access_token": token})
    request = urllib.request.Request(
        f"{GITEE_API}{path}?{query}",
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": content_type,
            "User-Agent": "Mklink-AI-Probe-release-publisher",
        },
    )
    return request


def request_json(
    request: urllib.request.Request,
    *,
    allow_null: bool = False,
    timeout: float = 60,
) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        parsed = urllib.parse.urlsplit(request.full_url)
        redacted = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", "")
        )
        raise GiteeApiError(error.code, redacted) from None
    result = json.loads(payload.decode("utf-8"))
    if result is None and allow_null:
        return None
    if not isinstance(result, dict):
        raise RuntimeError("Gitee API returned a non-object response")
    return result


def resolve_gitee_token() -> str:
    token = os.environ.get("GITEE_TOKEN", "").strip()
    if token:
        return token
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=gitee.com\n\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("unable to read the configured Gitee credential")
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    token = values.get("password", "").strip()
    if not token:
        raise RuntimeError("GITEE_TOKEN or a Git credential for gitee.com is required")
    return token


def ensure_gitee_release(
    *, owner: str, repo: str, token: str, tag: str, title: str, notes: str
) -> dict[str, object]:
    path = f"/repos/{owner}/{repo}/releases/tags/{tag}"
    try:
        release = request_json(
            build_gitee_request(method="GET", path=path, token=token),
            allow_null=True,
        )
    except GiteeApiError as error:
        if error.status != 404:
            raise
        release = None
    if release is not None:
        if release.get("tag_name") != tag:
            raise RuntimeError("Gitee returned a conflicting release tag")
        if release.get("name") not in (None, title) or release.get("body") not in (None, notes):
            raise RuntimeError("Gitee release metadata conflicts with the requested release")
        return release

    created = request_json(
        build_gitee_request(
            method="POST",
            path=f"/repos/{owner}/{repo}/releases",
            token=token,
            payload={
                "tag_name": tag,
                "target_commitish": "master",
                "name": title,
                "body": notes,
                "prerelease": False,
            },
        )
    )
    if created is None:
        raise RuntimeError("Gitee release creation returned an empty response")
    return created


def _multipart_file(path: Path) -> tuple[bytes, str]:
    boundary = f"mklink-{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    body = header + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


def verify_public_asset(
    *, url: str, expected_sha256: str, expected_size: int
) -> None:
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mklink-AI-Probe-release-verifier"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise RuntimeError("published Gitee asset does not match the local file")


def _asset_url(asset: Mapping[str, object]) -> str:
    return str(
        asset.get("browser_download_url")
        or asset.get("download_url")
        or asset.get("url")
        or ""
    )


def upload_gitee_asset(
    *, owner: str, repo: str, token: str, release: Mapping[str, object], path: Path
) -> dict[str, object]:
    for value in release.get("assets", []):
        if not isinstance(value, dict) or value.get("name") != path.name:
            continue
        url = _asset_url(value)
        if not url:
            raise RuntimeError(f"existing Gitee asset has no download URL: {path.name}")
        verify_public_asset(
            url=url,
            expected_sha256=sha256(path),
            expected_size=path.stat().st_size,
        )
        return value

    release_id = release.get("id")
    if release_id is None:
        raise RuntimeError("Gitee release response has no id")
    data, content_type = _multipart_file(path)
    return request_json(
        build_gitee_request(
            method="POST",
            path=f"/repos/{owner}/{repo}/releases/{release_id}/attach_files",
            token=token,
            data=data,
            content_type=content_type,
        ),
        timeout=600,
    )


def _run(
    command: Sequence[str], *, cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command), cwd=cwd, env=env, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{command[0]} failed: {detail}")
    return result


def _gitee_push(
    *, repository: Path, repo: str, refspec: str, token: str, force: bool = False
) -> None:
    owner = repo.split("/", 1)[0]
    with tempfile.TemporaryDirectory(prefix="mklink-askpass-") as directory:
        askpass = Path(directory) / ("askpass.cmd" if os.name == "nt" else "askpass.sh")
        if os.name == "nt":
            askpass.write_text("@echo off\necho %MKLINK_GIT_PASSWORD%\n", encoding="ascii")
        else:  # pragma: no cover
            askpass.write_text("#!/bin/sh\nprintf '%s\\n' \"$MKLINK_GIT_PASSWORD\"\n", encoding="ascii")
            askpass.chmod(0o700)
        env = os.environ.copy()
        env.update({
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "MKLINK_GIT_PASSWORD": token,
        })
        command = ["git", "-c", "credential.helper=", "push"]
        if force:
            command.append("--force")
        command.extend([f"https://{owner}@gitee.com/{repo}.git", refspec])
        _run(command, cwd=repository, env=env)


def push_version_tag(
    *, repository: Path, tag: str, github_repo: str, gitee_repo: str,
    gitee_token: str,
) -> None:
    current = _run(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()
    existing = subprocess.run(
        ["git", "rev-list", "-n", "1", tag],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if existing.returncode == 0 and existing.stdout.strip() != current:
        raise RuntimeError(f"tag {tag} points to a different commit")
    if existing.returncode != 0:
        _run(["git", "tag", "-a", tag, "-m", f"Mklink AI Probe {tag}"], cwd=repository)
    _run(["git", "push", "origin", "master"], cwd=repository)
    _gitee_push(
        repository=repository, repo=gitee_repo, refspec="master:master",
        token=gitee_token,
    )
    _run(["git", "push", "origin", tag], cwd=repository)
    _gitee_push(
        repository=repository, repo=gitee_repo, refspec=tag, token=gitee_token
    )


def select_github_uploads(
    assets: Sequence[Path], existing_assets: Sequence[Mapping[str, object]]
) -> list[Path]:
    existing = {str(asset.get("name")): asset for asset in existing_assets}
    uploads = []
    for path in assets:
        remote = existing.get(path.name)
        if remote is None:
            uploads.append(path)
            continue
        expected_digest = f"sha256:{sha256(path)}"
        if remote.get("size") != path.stat().st_size or remote.get("digest") != expected_digest:
            raise RuntimeError(f"conflicting GitHub release asset: {path.name}")
    extra = set(existing) - {path.name for path in assets}
    if extra:
        raise RuntimeError("GitHub release contains unexpected assets")
    return uploads


def publish_github_release(
    *, repo: str, tag: str, title: str, notes: str, assets: Sequence[Path]
) -> None:
    view = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "tagName,name,body,assets"],
        text=True,
        capture_output=True,
        check=False,
    )
    if view.returncode == 0:
        existing = json.loads(view.stdout)
        if existing.get("tagName") != tag or existing.get("name") != title or existing.get("body") != notes:
            raise RuntimeError("GitHub release metadata conflicts with the requested release")
        uploads = select_github_uploads(assets, existing.get("assets", []))
    else:
        _run(["gh", "release", "create", tag, "--repo", repo, "--title", title, "--notes", notes, "--verify-tag"])
        uploads = list(assets)
    if uploads:
        _run(["gh", "release", "upload", tag, "--repo", repo, *map(str, uploads)])


def publish_gitee_release(
    *, repo: str, tag: str, title: str, notes: str, token: str,
    assets: Sequence[Path], required_urls: Sequence[str],
) -> dict[str, object]:
    owner, name = repo.split("/", 1)
    release = ensure_gitee_release(
        owner=owner, repo=name, token=token, tag=tag, title=title, notes=notes
    )
    asset_urls: dict[str, str] = {}
    for path in assets:
        asset = upload_gitee_asset(
            owner=owner, repo=name, token=token, release=release, path=path
        )
        url = _asset_url(asset)
        if url:
            asset_urls[path.name] = url
    missing = set(required_urls) - set(asset_urls)
    if missing:
        raise RuntimeError(
            "Gitee release asset URLs were not returned: " + ", ".join(sorted(missing))
        )
    return {"release": release, "asset_urls": asset_urls}


def publish_updates_branch(
    *, document: Mapping[str, object], github_repo: str, gitee_repo: str,
    gitee_token: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="mklink-updates-") as directory:
        checkout = Path(directory)
        _run(["git", "init", "--initial-branch=updates"], cwd=checkout)
        _run(["git", "config", "user.name", "Mklink Release Bot"], cwd=checkout)
        _run(["git", "config", "user.email", "release@mklink.local"], cwd=checkout)
        (checkout / "latest.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        _run(["git", "add", "latest.json"], cwd=checkout)
        _run(["git", "commit", "-m", f"release: publish v{document['version']} update"], cwd=checkout)
        _run(["git", "push", "--force", f"https://github.com/{github_repo}.git", "updates:updates"], cwd=checkout)
        _gitee_push(
            repository=checkout, repo=gitee_repo, refspec="updates:updates",
            token=gitee_token, force=True,
        )


def publish_update_release(
    *, version: str, notes: str, published_at: str, release_dir: Path,
    updater_installer: Path, updater_signature: Path, github_repo: str,
    gitee_repo: str, gitee_token: str, repository: Path,
) -> dict[str, object]:
    tag = f"v{version}"
    title = f"Mklink AI Probe {tag}"
    required = validate_release_preflight(
        repository=repository,
        release_dir=release_dir,
        version=version,
        updater_installer=updater_installer,
        updater_signature=updater_signature,
    )
    signature = updater_signature.read_text(encoding="ascii").strip()
    if not signature:
        raise ValueError("updater signature is empty")

    push_version_tag(
        repository=repository, tag=tag, github_repo=github_repo,
        gitee_repo=gitee_repo, gitee_token=gitee_token,
    )
    publish_github_release(
        repo=github_repo, tag=tag, title=title, notes=notes, assets=required
    )
    gitee = publish_gitee_release(
        repo=gitee_repo, tag=tag, title=title, notes=notes, token=gitee_token,
        assets=required,
        required_urls=(
            updater_installer.name,
            f"Mklink-AI-Probe-v{version}-Skill.zip",
        ),
    )
    asset_urls = gitee["asset_urls"]
    if not isinstance(asset_urls, dict):
        raise RuntimeError("Gitee release asset URL map is invalid")
    skill_archive = release_dir / f"Mklink-AI-Probe-v{version}-Skill.zip"
    updater_url = str(asset_urls[updater_installer.name])
    skill_url = str(asset_urls[skill_archive.name])
    verify_public_asset(
        url=updater_url,
        expected_sha256=sha256(updater_installer),
        expected_size=updater_installer.stat().st_size,
    )
    verify_public_asset(
        url=skill_url,
        expected_sha256=sha256(skill_archive),
        expected_size=skill_archive.stat().st_size,
    )
    source_commit = git_output(repository, "rev-parse", "HEAD")
    document = build_latest_document(
        version=version,
        notes=notes,
        published_at=published_at,
        signature=signature,
        updater_url=updater_url,
        updater_sha256=sha256(updater_installer),
        updater_size=updater_installer.stat().st_size,
        skill_url=skill_url,
        skill_sha256=sha256(skill_archive),
        skill_size=skill_archive.stat().st_size,
        source_commit=source_commit,
    )
    publish_updates_branch(
        document=document, github_repo=github_repo, gitee_repo=gitee_repo,
        gitee_token=gitee_token,
    )
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--updater-installer", required=True, type=Path)
    parser.add_argument("--updater-signature", required=True, type=Path)
    parser.add_argument("--github-repo", default="Aladdin-Wang/Mklink-AI-Probe")
    parser.add_argument("--gitee-repo", default="Aladdin-Wang/Mklink-AI-Probe")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    document = publish_update_release(
        version=args.version,
        notes=args.notes,
        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        release_dir=args.release_dir.resolve(),
        updater_installer=args.updater_installer.resolve(),
        updater_signature=args.updater_signature.resolve(),
        github_repo=args.github_repo,
        gitee_repo=args.gitee_repo,
        gitee_token=resolve_gitee_token(),
        repository=args.repository.resolve(),
    )
    print(json.dumps({"version": document["version"], "published": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
