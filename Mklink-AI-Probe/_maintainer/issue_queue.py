"""Read-only GitHub intake plus a local, revision-aware processing ledger.

No report contents are executed, downloaded, or sent to another service here.
The maintainer agent performs triage and records outcomes explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

REPOSITORY = "MicroKeen/Mklink-AI-Probe"
OUTCOMES = ("needs-info", "needs-hil", "pr-open", "duplicate", "blocked", "done")
HOLD_LABELS = {"ai:skip", "ai:needs-hil", "ai:pr-open", "ai:duplicate"}
MAX_ATTEMPTS = 2


def github(endpoint: str):
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate", "--slurp"],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    pages = json.loads(result.stdout)
    return [item for page in pages for item in page]


def revision(issue: dict, comments: list[dict], maintainer: str) -> str:
    # Bot/maintenance comments must not retrigger their own processing loop.
    evidence = [
        [c.get("id"), c.get("body", "")]
        for c in comments
        if c.get("user", {}).get("type") != "Bot"
        and c.get("user", {}).get("login") != maintainer
    ]
    value = [issue["number"], issue.get("title", ""), issue.get("body", ""), evidence]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"repository": REPOSITORY, "issues": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("repository") != REPOSITORY or not isinstance(state.get("issues"), dict):
        raise ValueError("Unrecognized issue ledger; refusing to reset processing history")
    return state


def eligible(issue: dict, fingerprint: str, state: dict) -> bool:
    if issue.get("state") != "open" or "pull_request" in issue:
        return False
    labels = {label["name"] for label in issue.get("labels", [])}
    if labels & HOLD_LABELS:
        return False
    saved = state["issues"].get(str(issue["number"]), {})
    # Revision updates do not replenish the automatic attempt budget.
    if saved.get("attempts", 0) >= MAX_ATTEMPTS:
        return False
    if saved.get("revision") == fingerprint:
        return False
    return True


def record(path: Path, number: int, fingerprint: str, outcome: str) -> None:
    if number <= 0 or outcome not in OUTCOMES:
        raise ValueError("Invalid outcome or issue number")
    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise ValueError("Expected SHA-256 revision from scan")
    state = load_state(path)
    key = str(number)
    previous = state["issues"].get(key, {})
    attempts = previous.get("attempts", 0) + (previous.get("revision") != fingerprint)
    state["issues"][key] = {
        "revision": fingerprint, "outcome": outcome, "attempts": attempts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def scan(state: dict, maintainer: str, limit: int = 3) -> list[dict]:
    issues = github(f"repos/{REPOSITORY}/issues?state=open&per_page=100&sort=created&direction=asc")
    result = []
    for issue in issues:
        if "pull_request" in issue or issue.get("state") != "open":
            continue
        labels = {label["name"] for label in issue.get("labels", [])}
        if labels & HOLD_LABELS:
            continue
        comments = github(f"repos/{REPOSITORY}/issues/{issue['number']}/comments?per_page=100") if issue.get("comments") else []
        fingerprint = revision(issue, comments, maintainer)
        if eligible(issue, fingerprint, state):
            result.append({
                "number": issue["number"], "title": issue["title"],
                "url": issue["html_url"], "revision": fingerprint,
                "labels": sorted(labels),
            })
        if len(result) >= limit:
            break
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Ledger in ignored .build")
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("scan")
    read.add_argument("--maintainer", required=True, help="gh api user --jq .login")
    read.add_argument("--limit", type=int, choices=range(1, 4), default=3)
    write = sub.add_parser("record")
    write.add_argument("number", type=int)
    write.add_argument("revision")
    write.add_argument("outcome", choices=OUTCOMES)
    args = parser.parse_args()
    if args.command == "scan":
        print(json.dumps(scan(load_state(args.state), args.maintainer, args.limit), ensure_ascii=False, indent=2))
    else:
        record(args.state, args.number, args.revision, args.outcome)


if __name__ == "__main__":
    main()
