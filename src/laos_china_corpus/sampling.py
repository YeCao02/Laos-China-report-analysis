from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from .config import DEFAULT_TARGET_PER_SOURCE_MONTH

FORMAL_GRADES = {"A1", "A2", "B1", "B2"}
GRADE_SCORE = {"A1": 35, "B1": 32, "A2": 25, "B2": 22, "C1": 8, "C2": -100}


def _score(row: sqlite3.Row) -> tuple[float, bool]:
    metadata = json.loads(row["metadata_json"] or "{}")
    labels = set(json.loads(row["topic_labels_json"] or "[]"))
    scope = str(metadata.get("scope", ""))
    bilateral = "中老" in scope or bool(labels & {"bilateral", "china_in_laos"})
    syndicated = row["content_origin"] in {"xinhua", "cri", "syndicated", "reprint"}
    score = float(GRADE_SCORE.get(row["evidence_grade"], -50))
    score += 45 if bilateral else 0
    score += 20 if row["body_original"] or row["body_file"] else 0
    score += 12 if row["content_origin"] in {"local_original", "local_adapted"} else 0
    score -= 8 if syndicated else 0
    score += min(len(row["title_original"] or ""), 80) / 1000
    return score, syndicated


def rank_hydration_candidates(conn: sqlite3.Connection, per_month: int = 10) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM articles WHERE published_at IS NOT NULL AND story_id IS NOT NULL "
        "AND substr(published_at,1,10) BETWEEN '2012-01-01' AND '2026-08-07'"
    ).fetchall()
    groups: dict[tuple[str, str], dict[str, tuple[float, sqlite3.Row]]] = defaultdict(dict)
    for row in rows:
        code = row["source_code"].upper()
        source = "KPL" if code.startswith("KPL") else ("PASAXON" if code.startswith("PASAXON") else code)
        key = (source, row["published_at"][:7])
        score, _ = _score(row)
        prior = groups[key].get(row["story_id"])
        if prior is None or score > prior[0]:
            groups[key][row["story_id"]] = (score, row)
    result: list[sqlite3.Row] = []
    for key in sorted(groups):
        ranked = sorted(groups[key].values(), key=lambda item: (-item[0], item[1]["record_id"]))
        result.extend(item[1] for item in ranked[:per_month])
    return result


def select_monthly_sample(
    conn: sqlite3.Connection,
    *,
    sample_id: str = "main-2012-2026-v1",
    target: int = DEFAULT_TARGET_PER_SOURCE_MONTH,
    require_body: bool = True,
) -> int:
    conn.execute("DELETE FROM sample_memberships WHERE sample_id=?", (sample_id,))
    rows = rank_hydration_candidates(conn, per_month=10000)
    groups: dict[tuple[str, str], list[tuple[float, bool, sqlite3.Row]]] = defaultdict(list)
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        code = row["source_code"].upper()
        source = "KPL" if code.startswith("KPL") else ("PASAXON" if code.startswith("PASAXON") else code)
        key = (source, row["published_at"][:7])
        if row["story_id"] in seen[key]:
            continue
        seen[key].add(row["story_id"])
        formal = row["evidence_grade"] in FORMAL_GRADES
        provisional = row["evidence_grade"] == "C1" and json.loads(row["metadata_json"] or "{}").get("second_corroboration")
        has_body = bool(row["body_original"] or row["body_file"])
        if not (formal or provisional) or (require_body and not has_body):
            continue
        score, syndicated = _score(row)
        groups[key].append((score, syndicated, row))

    selected = 0
    now = datetime.now(timezone.utc).isoformat()
    syndicated_cap = math.ceil(target / 2)
    for key in sorted(groups):
        ranked = sorted(groups[key], key=lambda item: (-item[0], item[2]["record_id"]))
        chosen: list[tuple[float, bool, sqlite3.Row]] = []
        syndicated_count = 0
        deferred: list[tuple[float, bool, sqlite3.Row]] = []
        for item in ranked:
            if len(chosen) >= target:
                break
            if item[1] and syndicated_count >= syndicated_cap:
                deferred.append(item)
                continue
            chosen.append(item)
            syndicated_count += int(item[1])
        for item in deferred:
            if len(chosen) >= target:
                break
            chosen.append(item)
        for rank, (score, syndicated, row) in enumerate(chosen, 1):
            reason = "deterministic_priority"
            if syndicated and rank > syndicated_cap:
                reason = "syndicated_fallback_insufficient_local"
            conn.execute(
                "INSERT INTO sample_memberships "
                "(sample_id,story_id,record_id,source_code,year_month,selection_rank,score,quota_reason,selected_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sample_id, row["story_id"], row["record_id"], key[0], key[1], rank, score, reason, now),
            )
            selected += 1
    conn.commit()
    return selected
