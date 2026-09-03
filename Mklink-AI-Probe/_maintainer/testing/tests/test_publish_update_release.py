import importlib.util
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "release"
    / "publish_update_release.py"
)


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location("mklink_update_publisher", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_latest_document_matches_tauri_v2_schema(publisher):
    document = publisher.build_latest_document(
        version="0.1.0",
        notes="Stable release",
        published_at="2026-07-21T01:02:03Z",
        signature="signed-value",
        updater_url="https://gitee.example/Mklink-AI-Probe-v0.1.0-x64-Setup.exe",
        updater_sha256="a" * 64,
        updater_size=123,
        skill_url="https://gitee.example/Mklink-AI-Probe-v0.1.0-Skill.zip",
        skill_sha256="b" * 64,
        skill_size=456,
        source_commit="c" * 40,
    )

    assert document == {
        "version": "0.1.0",
        "notes": "Stable release",
        "pub_date": "2026-07-21T01:02:03Z",
        "platforms": {
            "windows-x86_64": {
                "signature": "signed-value",
                "url": "https://gitee.example/Mklink-AI-Probe-v0.1.0-x64-Setup.exe",
                "sha256": "a" * 64,
                "size": 123,
            }
        },
        "skill": {
            "version": "0.1.0",
            "url": "https://gitee.example/Mklink-AI-Probe-v0.1.0-Skill.zip",
            "sha256": "b" * 64,
            "size": 456,
            "source_commit": "c" * 40,
        },
    }


def test_publisher_defaults_to_official_repositories(publisher, tmp_path):
    args = publisher._parser().parse_args([
        "--version", "0.1.6",
        "--notes", "Release notes",
        "--release-dir", str(tmp_path),
        "--updater-installer", str(tmp_path / "setup.exe"),
        "--updater-signature", str(tmp_path / "setup.exe.sig"),
    ])

    assert args.github_repo == "Aladdin-Wang/Mklink-AI-Probe"
    assert args.gitee_repo == "Aladdin-Wang/Mklink-AI-Probe"


def test_gitee_request_uses_official_token_parameter_and_redacts_errors(
    publisher, monkeypatch,
):
    request = publisher.build_gitee_request(
        method="POST",
        path="/repos/owner/repo/releases",
        token="secret-token",
        payload={"tag_name": "v0.1.0"},
    )

    parsed = urllib.parse.urlsplit(request.full_url)
    assert parsed.path == "/api/v5/repos/owner/repo/releases"
    assert urllib.parse.parse_qs(parsed.query) == {"access_token": ["secret-token"]}
    assert b"secret-token" not in (request.data or b"")
    assert urllib.parse.parse_qs(request.data.decode()) == {"tag_name": ["v0.1.0"]}
    assert "secret-token" not in repr(request)

    def fail(_request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(publisher.urllib.request, "urlopen", fail)
    with pytest.raises(publisher.GiteeApiError) as captured:
        publisher.request_json(request)
    assert "secret-token" not in str(captured.value)


def test_resolve_gitee_token_uses_git_credential_without_echoing_secret(
    publisher, monkeypatch,
):
    calls = []

    class Result:
        returncode = 0
        stdout = "protocol=https\nhost=gitee.com\nusername=maintainer\npassword=credential-token\n"
        stderr = ""

    monkeypatch.delenv("GITEE_TOKEN", raising=False)
    monkeypatch.setattr(
        publisher.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Result(),
    )

    assert publisher.resolve_gitee_token() == "credential-token"
    assert calls[0][1]["input"] == "protocol=https\nhost=gitee.com\n\n"


def test_gitee_release_creation_is_idempotent(publisher, monkeypatch):
    calls = []
    existing = {"id": 42, "tag_name": "v0.1.0", "assets": []}

    def request_json(request, **_kwargs):
        calls.append((request.method, request.full_url))
        if request.method == "GET":
            return existing
        raise AssertionError("existing release must not be recreated")

    monkeypatch.setattr(publisher, "request_json", request_json)

    result = publisher.ensure_gitee_release(
        owner="owner",
        repo="repo",
        token="token",
        tag="v0.1.0",
        title="Mklink AI Probe v0.1.0",
        notes="Stable release",
    )

    assert result == existing
    assert calls == [
        (
            "GET",
            "https://gitee.com/api/v5/repos/owner/repo/releases/tags/v0.1.0?access_token=token",
        )
    ]


def test_gitee_release_creation_treats_null_tag_lookup_as_missing(
    publisher, monkeypatch,
):
    calls = []
    created = {"id": 43, "tag_name": "v0.1.1", "assets": []}

    def request_json(request, *, allow_null=False):
        calls.append((request.method, allow_null))
        if request.method == "GET":
            assert allow_null is True
            return None
        assert urllib.parse.parse_qs(request.data.decode())["target_commitish"] == ["master"]
        return created

    monkeypatch.setattr(publisher, "request_json", request_json)

    result = publisher.ensure_gitee_release(
        owner="owner",
        repo="repo",
        token="token",
        tag="v0.1.1",
        title="Mklink AI Probe v0.1.1",
        notes="Patch release",
    )

    assert result == created
    assert calls == [("GET", True), ("POST", False)]


def test_gitee_asset_upload_uses_a_large_file_timeout(publisher, monkeypatch, tmp_path):
    asset = tmp_path / "setup.exe"
    asset.write_bytes(b"installer")
    calls = []

    def request_json(request, **kwargs):
        calls.append((request, kwargs))
        return {"name": asset.name, "browser_download_url": "https://example.invalid/setup.exe"}

    monkeypatch.setattr(publisher, "request_json", request_json)

    result = publisher.upload_gitee_asset(
        owner="owner",
        repo="repo",
        token="token",
        release={"id": 7, "assets": []},
        path=asset,
    )

    assert result["name"] == asset.name
    assert calls[0][1]["timeout"] == 600


def release_fixture(publisher, tmp_path, *, version="0.1.0", head="a" * 40):
    repository = tmp_path / "repo"
    release_dir = tmp_path / "release"
    (repository / "gui" / "src-tauri").mkdir(parents=True)
    release_dir.mkdir()
    (repository / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n', encoding="utf-8"
    )
    (repository / "gui" / "src-tauri" / "Cargo.toml").write_text(
        f'[package]\nversion = "{version}"\n', encoding="utf-8"
    )
    (repository / "gui" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    names = [
        f"Mklink-AI-Probe-v{version}-x64-Setup.exe",
        f"Mklink-AI-Probe-v{version}-x64-Setup.exe.sig",
        f"Mklink-AI-Probe-v{version}-Skill.zip",
        f"MKLink-Site-Agent-v{version}-windows-x86_64-portable.zip",
    ]
    for name in names:
        path = release_dir / name
        path.write_bytes(name.encode())
    portable = release_dir / names[-1]
    portable_manifest = release_dir / (
        f"MKLink-Site-Agent-v{version}-windows-x86_64-portable.manifest.json"
    )
    portable_manifest.write_text(
        json.dumps({
            "bundle": {"version": version},
            "artifact": {
                "name": portable.name,
                "size": portable.stat().st_size,
                "sha256": publisher.sha256(portable),
            },
        }),
        encoding="utf-8",
    )
    names.append(portable_manifest.name)
    assets = []
    for name in names:
        path = release_dir / name
        assets.append({
            "name": name,
            "size": path.stat().st_size,
            "sha256": publisher.sha256(path),
        })
    manifest = {
        "release_version": version,
        "source_commit": head,
        "assets": assets,
    }
    (release_dir / "release-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (release_dir / "SHA256SUMS.txt").write_text(
        "".join(f'{asset["sha256"]}  {asset["name"]}\n' for asset in assets),
        encoding="ascii",
    )
    return repository, release_dir, release_dir / names[0], release_dir / names[1]


def test_release_preflight_rejects_dirty_branch_and_tampered_assets(
    publisher, monkeypatch, tmp_path,
):
    repository, release_dir, installer, signature = release_fixture(publisher, tmp_path)
    responses = {
        ("branch", "--show-current"): "feature/test",
        ("rev-parse", "HEAD"): "a" * 40,
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(
        publisher,
        "git_output",
        lambda repository, *args: responses[args],
    )

    with pytest.raises(RuntimeError, match="master"):
        publisher.validate_release_preflight(
            repository=repository,
            release_dir=release_dir,
            version="0.1.0",
            updater_installer=installer,
            updater_signature=signature,
        )

    responses[("branch", "--show-current")] = "master"
    responses[("status", "--porcelain")] = " M tracked-file"
    with pytest.raises(RuntimeError, match="clean"):
        publisher.validate_release_preflight(
            repository=repository,
            release_dir=release_dir,
            version="0.1.0",
            updater_installer=installer,
            updater_signature=signature,
        )

    responses[("status", "--porcelain")] = ""
    installer.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="hash"):
        publisher.validate_release_preflight(
            repository=repository,
            release_dir=release_dir,
            version="0.1.0",
            updater_installer=installer,
            updater_signature=signature,
        )


def test_github_existing_assets_must_match_digest(publisher, tmp_path):
    asset = tmp_path / "setup.exe"
    asset.write_bytes(b"setup")
    digest = f"sha256:{publisher.sha256(asset)}"

    assert publisher.select_github_uploads(
        [asset], [{"name": asset.name, "size": asset.stat().st_size, "digest": digest}]
    ) == []
    with pytest.raises(RuntimeError, match="conflicting GitHub release asset"):
        publisher.select_github_uploads(
            [asset], [{"name": asset.name, "size": asset.stat().st_size, "digest": "sha256:deadbeef"}]
        )


def test_gitee_git_push_uses_token_only_in_askpass_environment(
    publisher, monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    publisher._gitee_push(
        repository=tmp_path,
        repo="owner/repo",
        refspec="master:master",
        token="secret-token",
    )

    command, kwargs = calls[0]
    assert "secret-token" not in " ".join(command)
    assert kwargs["env"]["MKLINK_GIT_PASSWORD"] == "secret-token"
    assert command[-1] == "master:master"


def test_updates_branch_is_published_only_after_both_releases_and_verification(
    publisher, monkeypatch, tmp_path,
):
    events = []
    installer = tmp_path / "Mklink-AI-Probe-v0.1.0-x64-Setup.exe"
    signature = tmp_path / "Mklink-AI-Probe-v0.1.0-x64-Setup.exe.sig"
    skill = tmp_path / "Mklink-AI-Probe-v0.1.0-Skill.zip"
    installer.write_bytes(b"installer")
    signature.write_text("signature", encoding="ascii")
    skill.write_bytes(b"skill")

    monkeypatch.setattr(
        publisher,
        "validate_release_preflight",
        lambda **_kwargs: events.append("preflight") or [installer, signature],
    )
    monkeypatch.setattr(publisher, "push_version_tag", lambda **_kwargs: events.append("tag"))
    monkeypatch.setattr(publisher, "publish_github_release", lambda **_kwargs: events.append("github"))
    monkeypatch.setattr(
        publisher,
        "publish_gitee_release",
        lambda **_kwargs: events.append("gitee") or {
            "asset_urls": {
                installer.name: "https://gitee.example/Mklink-AI-Probe-v0.1.0-x64-Setup.exe",
                "Mklink-AI-Probe-v0.1.0-Skill.zip": "https://gitee.example/Mklink-AI-Probe-v0.1.0-Skill.zip",
            }
        },
    )
    monkeypatch.setattr(
        publisher,
        "verify_public_asset",
        lambda **_kwargs: events.append("verify"),
    )
    monkeypatch.setattr(
        publisher,
        "publish_updates_branch",
        lambda **_kwargs: events.append("updates"),
    )
    monkeypatch.setattr(publisher, "git_output", lambda *_args: "c" * 40)

    publisher.publish_update_release(
        version="0.1.0",
        notes="Stable release",
        published_at="2026-07-21T01:02:03Z",
        release_dir=tmp_path,
        updater_installer=installer,
        updater_signature=signature,
        github_repo="Aladdin-Wang/Mklink-AI-Probe",
        gitee_repo="Aladdin-Wang/Mklink-AI-Probe",
        gitee_token="token",
        repository=tmp_path,
    )

    assert events == [
        "preflight", "tag", "github", "gitee", "verify", "verify", "updates"
    ]
