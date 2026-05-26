"""串口自动应答引擎。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AutoReplyRule:
    match_hex: str | None = None
    match_regex: str | None = None
    match_contains: str | None = None
    reply_hex: str | None = None
    reply_ascii: str | None = None
    delay: float = 0.0
    description: str = ""


def _bytes_to_ascii_safe(data: bytes) -> str:
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data)


def _process_escape_sequences(s: str) -> bytes:
    result = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "n":
                result.append(0x0A)
                i += 2
            elif nxt == "r":
                result.append(0x0D)
                i += 2
            elif nxt == "t":
                result.append(0x09)
                i += 2
            elif nxt == "x" and i + 3 < len(s):
                result.append(int(s[i + 2 : i + 4], 16))
                i += 4
            elif nxt == "\\":
                result.append(0x5C)
                i += 2
            else:
                result.append(ord(s[i]))
                i += 1
        else:
            result.append(ord(s[i]))
            i += 1
    return bytes(result)


def _build_reply(rule: AutoReplyRule) -> bytes | None:
    if rule.reply_hex is not None:
        return bytes.fromhex(rule.reply_hex)
    if rule.reply_ascii is not None:
        return _process_escape_sequences(rule.reply_ascii)
    return None


def _matches(rule: AutoReplyRule, data: bytes) -> bool:
    if rule.match_hex is not None:
        hex_str = data.hex().upper()
        if rule.match_hex.upper() in hex_str:
            return True

    ascii_repr = _bytes_to_ascii_safe(data)

    if rule.match_regex is not None:
        if re.search(rule.match_regex, ascii_repr):
            return True

    if rule.match_contains is not None:
        if rule.match_contains in ascii_repr:
            return True

    return False


class AutoReplyEngine:
    def __init__(self, rules: list[AutoReplyRule] | None = None) -> None:
        self._rules: list[AutoReplyRule] = list(rules) if rules else []

    def add_rule(self, rule: AutoReplyRule) -> None:
        self._rules.append(rule)

    def remove_rule(self, index: int) -> None:
        del self._rules[index]

    def load_rules(self, rules_data: list[dict]) -> None:
        for item in rules_data:
            self._rules.append(AutoReplyRule(**{
                k: v for k, v in item.items() if k in AutoReplyRule.__dataclass_fields__
            }))

    def check(self, data: bytes) -> list[tuple[bytes, float]]:
        results: list[tuple[bytes, float]] = []
        for rule in self._rules:
            if _matches(rule, data):
                reply = _build_reply(rule)
                if reply is not None:
                    results.append((reply, rule.delay))
        return results

    @property
    def rules(self) -> list[AutoReplyRule]:
        return list(self._rules)


def load_rules_from_file(path: str) -> list[AutoReplyRule]:
    """Load rules from a JSON file. The file should contain a list of rule dicts."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rules: list[AutoReplyRule] = []
    for item in data:
        rules.append(AutoReplyRule(**{
            k: v for k, v in item.items() if k in AutoReplyRule.__dataclass_fields__
        }))
    return rules
