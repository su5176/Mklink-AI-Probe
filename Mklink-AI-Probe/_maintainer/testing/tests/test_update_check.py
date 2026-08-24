from mklink import mcp_server, update_check


def test_runtime_manifest_sources_prefer_github_and_fall_back_to_gitee(monkeypatch):
    github, gitee = update_check.DEFAULT_MANIFEST_URLS
    assert github == (
        "https://raw.githubusercontent.com/Aladdin-Wang/"
        "Mklink-AI-Probe/updates/latest.json"
    )
    assert gitee == (
        "https://gitee.com/Aladdin-Wang/Mklink-AI-Probe/raw/updates/latest.json"
    )

    calls = []

    def request(url, timeout):
        calls.append((url, timeout))
        if url == github:
            raise OSError("GitHub unavailable")
        return b'{"version":"0.1.6"}'

    monkeypatch.setattr(update_check, "_request_bytes", request)

    manifest, manifest_url = update_check.fetch_manifest(
        update_check.DEFAULT_MANIFEST_URLS, timeout=7.5,
    )

    assert manifest == {"version": "0.1.6"}
    assert manifest_url == gitee
    assert calls == [(github, 7.5), (gitee, 7.5)]


def test_runtime_update_check_uses_shared_cache(monkeypatch, tmp_path):
    cache = tmp_path / "update.json"
    manifest = {"version": "9.8.7", "notes": "Important fixes"}
    calls = []
    monkeypatch.setattr(update_check, "current_version", lambda: "1.2.3")
    monkeypatch.setattr(
        update_check,
        "fetch_manifest",
        lambda urls, timeout: calls.append((tuple(urls), timeout))
        or (manifest, "https://example.test/latest.json"),
    )

    first = update_check.check_for_update(cache_file=cache)
    second = update_check.check_for_update(cache_file=cache)

    assert first["update_available"] is True
    assert first["install_requires_user_approval"] is True
    assert second["cached"] is True
    assert len(calls) == 1


def test_runtime_update_check_does_not_block_use_when_network_is_unavailable(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(update_check, "current_version", lambda: "1.2.3")
    monkeypatch.setattr(
        update_check,
        "fetch_manifest",
        lambda _urls, timeout: (_ for _ in ()).throw(RuntimeError(f"offline after {timeout}")),
    )

    result = update_check.check_for_update(cache_file=tmp_path / "missing.json")

    assert result["status"] == "unavailable"
    assert result["update_available"] is False
    assert result["current_version"] == "1.2.3"


def test_mcp_ping_reports_update_state_for_ai_clients(monkeypatch):
    tools = {}

    class FakeMcp:
        def tool(self):
            def register(function):
                tools[function.__name__] = function
                return function
            return register

    expected = {
        "status": "ok",
        "current_version": "0.1.4",
        "latest_version": "0.1.5",
        "update_available": True,
        "install_requires_user_approval": True,
    }
    monkeypatch.setattr(update_check, "check_for_update", lambda: expected)
    monkeypatch.setattr("mklink.toolchain.status", lambda: {"elf_backend": "builtin"})
    mcp_server._register_health_tools(FakeMcp())

    result = tools["ping"]()

    assert result["ok"] is True
    assert result["update"] == expected
