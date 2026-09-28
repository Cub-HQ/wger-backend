#!/usr/bin/env python3
"""Patch pinned server templates that bypass the shared Australian formats."""
import hashlib
from pathlib import Path
import sys


TEMPLATES = {
    "history-overview": {
        "sha256": "dcafe96253a5f4de6e906bb4d0f5df4763681125abb1d8795593303cf70f119e",
        "replacements": (
            ('{{ day.grouper|date:"l, j F Y" }}', '{{ day.grouper|date:"SHORT_DATE_FORMAT" }}'),
            ("event.stream.timestamp|date:'Y-m-d H:i:s'", "event.stream.timestamp|date:'SHORT_DATETIME_FORMAT'"),
        ),
    },
    "api-key": {
        "sha256": "0cd7ebae9a9de4af5926589c1afb41bf817cc797ec5f0eb00143dea80ac88386",
        "replacements": (
            ('session.long_lived.created|date:"Y-m-d H:i"', 'session.long_lived.created|date:"SHORT_DATETIME_FORMAT"'),
            ('session.expire_date|date:"Y-m-d H:i"', 'session.expire_date|date:"SHORT_DATETIME_FORMAT"'),
        ),
    },
}


def patch_text(name: str, text: str) -> str:
    try:
        replacements = TEMPLATES[name]["replacements"]
    except KeyError:
        raise SystemExit(f"Unknown pinned template: {name}") from None
    for old, new in replacements:
        if text.count(old) != 1:
            raise SystemExit(f"Expected one {name} date anchor: {old}")
        text = text.replace(old, new)
    return text


def patch_file(name: str, source: Path, target: Path) -> None:
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TEMPLATES[name]["sha256"]:
        raise SystemExit(f"Pinned wger {name} template changed; review Australian date patch before deployment")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch_text(name, raw.decode()))
    print(f"Pinned {name} dates use shared Australian numeric formats")


def self_test() -> None:
    history = patch_text("history-overview", '''
{{ day.grouper|date:"l, j F Y" }}
{{ event.stream.timestamp|date:'Y-m-d H:i:s' }}
''')
    account = patch_text("api-key", '''
{{ session.long_lived.created|date:"Y-m-d H:i" }}
{{ session.expire_date|date:"Y-m-d H:i" }}
''')
    assert "SHORT_DATE_FORMAT" in history
    assert history.count("SHORT_DATETIME_FORMAT") == 1
    assert account.count("SHORT_DATETIME_FORMAT") == 2
    print("Australian template date patch smoke passed")


def main() -> None:
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return
    if len(sys.argv) != 4:
        raise SystemExit(f"usage: {sys.argv[0]} TEMPLATE SOURCE TARGET")
    name = sys.argv[1]
    if name not in TEMPLATES:
        raise SystemExit(f"Unknown pinned template: {name}")
    patch_file(name, Path(sys.argv[2]), Path(sys.argv[3]))


if __name__ == "__main__":
    main()
