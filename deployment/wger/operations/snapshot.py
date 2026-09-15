"""Pure snapshot manifest and recovery-plan checks for wger operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


REQUIRED = {"database.dump", "media.tar", "deployment.tar"}
RESTORE_ARTIFACTS = ("database.dump", "media.tar")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_manifest(root: Path, stamp: str) -> dict:
    files = {path.name: sha256(path) for path in root.iterdir() if path.is_file() and path.name not in {"manifest.json", "INCOMPLETE"}}
    missing = REQUIRED - set(files)
    if missing:
        raise ValueError("snapshot missing required recovery files: " + ", ".join(sorted(missing)))
    manifest = {"format": 2, "created_at": stamp, "database_format": "pg_dump custom full database",
                "media_stable_across_database_dump": True, "includes_powersync_storage": True,
                "powersync_storage_source": "database.dump", "contains_secrets": True,
                "storage_policy": "private local backup; never publish or commit",
                "excludes": ["rebuildable static files", "Redis cache and queued tasks", "Celery schedule cache"],
                "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_snapshot(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    if (root / "INCOMPLETE").exists() or manifest.get("format") != 2 or not manifest.get("includes_powersync_storage") or manifest.get("powersync_storage_source") != "database.dump":
        raise ValueError("complete full-database recovery snapshot with PowerSync state required")
    if REQUIRED - set(manifest.get("files", {})):
        raise ValueError("snapshot omits required recovery state")
    for name, expected in manifest["files"].items():
        path = root / name
        if path.parent != root or not path.is_file() or sha256(path) != expected:
            raise ValueError("snapshot checksum mismatch")
    return manifest
