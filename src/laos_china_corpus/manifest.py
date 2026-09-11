from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import EN_DIRECT_QUERIES, ENTITY_QUERIES, LAO_DIRECT_QUERIES, SNAPSHOT_END, SNAPSHOT_START, ProjectPaths


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(conn: sqlite3.Connection, paths: ProjectPaths, *, command: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    preview = paths.root.parent / "preview.md"
    design = paths.root.parent / "retrieval_system_design.md"
    payload = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": now,
        "command": command,
        "snapshot": {"start": SNAPSHOT_START.isoformat(), "end": SNAPSHOT_END.isoformat()},
        "environment": {"python": sys.version, "platform": platform.platform()},
        "queries": {
            "lao_direct": list(LAO_DIRECT_QUERIES),
            "english_direct": list(EN_DIRECT_QUERIES),
            "entity": list(ENTITY_QUERIES),
        },
        "inputs": {
            "preview.md": file_sha256(preview),
            "retrieval_system_design.md": file_sha256(design),
        },
        "row_counts": {
            "articles": conn.execute("SELECT count(*) FROM articles").fetchone()[0],
            "stories": conn.execute("SELECT count(*) FROM story_clusters").fetchone()[0],
            "sample_memberships": conn.execute("SELECT count(*) FROM sample_memberships").fetchone()[0],
            "translation_queue": conn.execute("SELECT count(*) FROM translation_queue").fetchone()[0],
            "evidence_objects": conn.execute("SELECT count(*) FROM evidence_objects").fetchone()[0],
            "crawl_partitions": conn.execute("SELECT count(*) FROM crawl_partitions").fetchone()[0],
            "failed_partitions": conn.execute(
                "SELECT count(*) FROM crawl_partitions WHERE status='failed'"
            ).fetchone()[0],
        },
    }
    paths.audit.mkdir(parents=True, exist_ok=True)
    (paths.audit / "run_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload
