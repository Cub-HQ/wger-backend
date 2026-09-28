#!/usr/bin/env python3
"""Patch the pinned PDF footer to Australian numeric date order."""
import hashlib
from pathlib import Path
import sys

PDF_SHA256 = "feee0507c07db45160024eb7a60d90487fcbaef7f2700acf345fd290403774f5"
OLD_DATE = "date = datetime.date.today().strftime('%d.%m.%Y')"
NEW_DATE = "date = datetime.date.today().strftime('%d/%m/%Y')"


def patch_text(text: str) -> str:
    if text.count(OLD_DATE) != 1:
        raise SystemExit(f"Expected one pinned PDF footer date anchor, found {text.count(OLD_DATE)}")
    return text.replace(OLD_DATE, NEW_DATE)


def patch_file(source: Path, target: Path) -> None:
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != PDF_SHA256:
        raise SystemExit(f"Pinned wger PDF utility changed ({digest}); review the date patch before deployment")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch_text(raw.decode()))
    print("Wger PDF footer now uses Australian DD/MM/YYYY dates")


def self_test() -> None:
    patched = patch_text("date = datetime.date.today().strftime('%d.%m.%Y')")
    assert "strftime('%d/%m/%Y')" in patched
    print("Australian PDF date patch smoke passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        patch_file(Path(sys.argv[1]), Path(sys.argv[2]))
