from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def queue_selected_translations(conn: sqlite3.Connection, project_root: Path) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "DELETE FROM translation_queue WHERE record_id NOT IN "
        "(SELECT record_id FROM sample_memberships)"
    )
    rows = conn.execute(
        "SELECT DISTINCT a.record_id,a.story_id,a.language,a.body_original,a.body_file "
        "FROM articles a JOIN sample_memberships s ON s.record_id=a.record_id"
    ).fetchall()
    queued = 0
    for row in rows:
        text = row["body_original"] or ""
        if not text and row["body_file"]:
            path = project_root / row["body_file"]
            if path.exists():
                text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
        conn.execute(
            "INSERT INTO translation_queue "
            "(record_id,story_id,source_language,body_sha256,body_file,translation_status,queued_at) "
            "VALUES (?,?,?,?,?,'pending_provider_decision',?) "
            "ON CONFLICT(record_id) DO UPDATE SET body_sha256=excluded.body_sha256,"
            "body_file=excluded.body_file,translation_status='pending_provider_decision',queued_at=excluded.queued_at",
            (row["record_id"], row["story_id"], row["language"], digest, row["body_file"], now),
        )
        queued += 1
    conn.commit()
    return queued
