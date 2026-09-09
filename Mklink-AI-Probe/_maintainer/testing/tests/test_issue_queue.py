import json

import pytest

from _maintainer import issue_queue as queue


def issue(number=1, **updates):
    return {"number": number, "state": "open", "title": "A failure", "body": "steps",
            "html_url": f"https://github.com/{queue.REPOSITORY}/issues/{number}",
            "labels": [], "comments": 0, **updates}


def test_new_issue_and_pull_request_are_distinguished(tmp_path):
    state = queue.load_state(tmp_path / "state.json")
    assert queue.eligible(issue(), "a" * 64, state)
    assert not queue.eligible(issue(pull_request={}), "a" * 64, state)
    assert not queue.eligible(issue(state="closed"), "a" * 64, state)


@pytest.mark.parametrize("label", sorted(queue.HOLD_LABELS))
def test_explicit_hold_prevents_retry_even_with_new_evidence(tmp_path, label):
    assert not queue.eligible(issue(labels=[{"name": label}]), "a" * 64, queue.load_state(tmp_path / "state.json"))


def test_revision_tracks_reporter_evidence_not_bot_replies():
    base = queue.revision(issue(), [], "maintainer")
    bot = {"id": 1, "body": "processed", "user": {"type": "Bot", "login": "bot"}}
    own = {"id": 2, "body": "need logs", "user": {"type": "User", "login": "maintainer"}}
    user = {"id": 3, "body": "new reproduction", "user": {"type": "User", "login": "reporter"}}
    assert queue.revision(issue(), [bot, own], "maintainer") == base
    assert queue.revision(issue(), [user], "maintainer") != base
    assert queue.revision(issue(body="changed"), [], "maintainer") != base


def test_identical_record_is_idempotent_and_new_evidence_is_bounded(tmp_path):
    path = tmp_path / "state.json"
    queue.record(path, 1, "a" * 64, "needs-info")
    queue.record(path, 1, "a" * 64, "needs-info")
    state = queue.load_state(path)
    assert state["issues"]["1"]["attempts"] == 1
    assert not queue.eligible(issue(), "a" * 64, state)
    assert queue.eligible(issue(), "b" * 64, state)
    queue.record(path, 1, "b" * 64, "blocked")
    assert not queue.eligible(issue(), "c" * 64, queue.load_state(path))


def test_corrupt_or_foreign_ledger_does_not_silently_reprocess(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        queue.load_state(path)
    path.write_text('{"repository":"another/repo","issues":{}}', encoding="utf-8")
    with pytest.raises(ValueError):
        queue.load_state(path)


def test_scan_limits_work_and_treats_shell_text_as_data(tmp_path, monkeypatch):
    data = [issue(1, pull_request={}), issue(2, labels=[{"name": "ai:skip"}]),
            issue(3, title="$(upload-secrets)"), issue(4)]
    calls = []

    def read(endpoint):
        calls.append(endpoint)
        return data

    monkeypatch.setattr(queue, "github", read)
    result = queue.scan(queue.load_state(tmp_path / "state.json"), "maintainer", 1)
    assert [row["number"] for row in result] == [3]
    assert result[0]["title"] == "$(upload-secrets)"
    assert len(calls) == 1


def test_github_reads_all_pages_without_shell(monkeypatch):
    def run(argv, **kwargs):
        assert argv[-2:] == ["--paginate", "--slurp"]
        assert not kwargs.get("shell")
        return type("Result", (), {"stdout": '[[{"number":1}],[{"number":2}]]'})()

    monkeypatch.setattr(queue.subprocess, "run", run)
    assert queue.github("repos/owner/repo/issues") == [{"number": 1}, {"number": 2}]


def test_bad_record_cannot_replace_ledger(tmp_path):
    path = tmp_path / "state.json"
    with pytest.raises(ValueError):
        queue.record(path, 1, "not-a-revision", "done")
    assert not path.exists()
