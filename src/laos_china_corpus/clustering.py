from __future__ import annotations

import re
import sqlite3


def assign_story_ids(conn: sqlite3.Connection) -> int:
    """Assign deterministic story IDs without deleting language versions.

    KPL uses the numeric source article ID as the strongest bilingual linkage.
    Other sources initially get one story per article; later manual/entity
    clustering can update story_id without changing record_id.
    """
    rows = conn.execute(
        "SELECT record_id, source_code, source_article_id FROM articles ORDER BY record_id"
    ).fetchall()
    updated = 0
    for row in rows:
        source = row["source_code"].upper()
        source_id = row["source_article_id"]
        if source.startswith("KPL") and source_id:
            numeric = re.sub(r"\D", "", source_id).lstrip("0") or "0"
            story_id = f"KPL-STORY-{int(numeric):06d}"
            method = "shared_source_article_id"
            confidence = 1.0
        else:
            story_id = f"{source}-STORY-{row['record_id']}"
            method = "single_record"
            confidence = 1.0
        conn.execute("UPDATE articles SET story_id=? WHERE record_id=?", (story_id, row["record_id"]))
        conn.execute(
            "INSERT OR IGNORE INTO story_clusters "
            "(story_id, source_code, representative_record_id, cluster_method, confidence) "
            "VALUES (?,?,?,?,?)",
            (story_id, source, row["record_id"], method, confidence),
        )
        updated += 1
    conn.commit()
    return updated

