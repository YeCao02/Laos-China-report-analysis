from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .archives.commoncrawl import extract_archive_http_payload
from .archives.pasaxon_php_clean import titles_agree
from .config import ProjectPaths
from .db import upsert_article
from .models import ArticleRecord


def _canonical_url(value: str) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in {80, 443} else ""
    return urlunsplit(((parts.scheme or "http").lower(), host + port, parts.path, parts.query, ""))


def _safe_file(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise ValueError(f"staging raw_file escapes project root: {value}")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _usable_title(title: str, body: str) -> str:
    replacement_share = title.count("\ufffd") / max(len(title), 1)
    lao_count = sum("\u0e80" <= char <= "\u0eff" for char in title)
    if not title.strip() or replacement_share > 0.2 or lao_count < 3:
        return next((line.strip() for line in body.splitlines() if line.strip()), title)
    return title.strip()


def _pasaxon_php_id(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    if not parts.path.casefold().endswith("pasaxon-detail.php"):
        return None
    candidate = str(parse_qs(parts.query).get("p_id", [""])[0])
    return candidate if candidate.isdigit() else None


def import_pasaxon_round2(
    conn: sqlite3.Connection, paths: ProjectPaths, ndjson_path: Path
) -> dict[str, int]:
    """Validate and idempotently merge import-ready official Wayback bodies."""
    imported = skipped = failed = 0
    for line_number, line in enumerate(ndjson_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            body = str(item["body_original"])
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != item["content_sha256"]:
                raise ValueError("body SHA-256 mismatch")
            raw_path = _safe_file(paths.root, item["raw_file"])
            raw_payload = gzip.decompress(raw_path.read_bytes())
            if hashlib.sha256(raw_payload).hexdigest() != item["raw_sha256"]:
                raise ValueError("raw response SHA-256 mismatch")
            original_url = _canonical_url(item["original_url"])
            duplicate = conn.execute(
                "SELECT record_id FROM articles WHERE lower(replace(original_url,':80/','/'))=?",
                (original_url.lower(),),
            ).fetchone()
            if duplicate:
                skipped += 1
                continue
            is_current_official = item.get("source_code") == "pasaxon" and item.get("evidence_grade") == "A1"
            suffix = hashlib.sha256(original_url.encode("utf-8")).hexdigest()[:16]
            record_id = str(item.get("record_id")) if is_current_official else f"PASAXON-ARCH-LO-{suffix}"
            story_id = str(item.get("story_id") or f"PASAXON-STORY-{item.get('source_article_id')}") if is_current_official else f"PASAXON-ARCH-STORY-{suffix}"
            source_code = "pasaxon" if is_current_official else "pasaxon_archive"
            evidence_grade = "A1" if is_current_official else "B1"
            content_origin = str(item.get("content_origin") or "unknown") if is_current_official else "pasaxon_archive"
            archive_provider = str(dict(item.get("metadata") or {}).get("archive_provider") or "")
            evidence_type = (
                "official_current_html" if is_current_official else
                "arquivo_pt_official_html" if archive_provider == "Arquivo.pt" else
                "wayback_official_html"
            )
            evidence_url = original_url if is_current_official else item.get("archive_url")
            record = ArticleRecord(
                record_id=record_id,
                source_code=source_code,
                source_article_id=item.get("source_article_id") if is_current_official else None,
                story_id=story_id,
                language=item.get("language", "lo"),
                title_original=_usable_title(str(item.get("title_original") or ""), body),
                excerpt_original=item.get("excerpt_original"),
                published_at=item["published_at"],
                date_precision=item.get("date_precision", "url_day"),
                body_original=body,
                body_method=item["body_method"],
                china_note_zh=item.get("china_note_zh") or ("Pasaxon现站正文直接命中中国相关关键词；具体主题待人工精编。" if is_current_official else "Pasaxon官方旧站网页存档，正文直接命中中国相关老挝语关键词；具体主题待人工精编。"),
                topic_labels=list(item.get("topic_labels") or ["china_general"]),
                content_origin=content_origin,
                matched_queries=list(item.get("matched_queries") or []),
                original_url=original_url,
                archive_url=item.get("archive_url"),
                search_url=item.get("search_url"),
                body_file=item.get("body_file"),
                raw_file=item["raw_file"],
                evidence_grade=evidence_grade,
                retrieval_tier=item.get("retrieval_tier", "T1_DIRECT_CHINA"),
                content_sha256=item["content_sha256"],
                retrieved_at=item.get("retrieved_at"),
                metadata={
                    **dict(item.get("metadata") or {}),
                    "archive_capture_timestamp": item.get("archive_capture_timestamp"),
                    "archive_digest": item.get("archive_digest"),
                    "old_pasaxon_slot": item.get("old_pasaxon_slot"),
                    "parser": item.get("parser"),
                    "raw_response_sha256": item["raw_sha256"],
                    "staging_import": str(ndjson_path.relative_to(paths.root)),
                },
            )
            upsert_article(conn, record)
            conn.execute(
                "INSERT OR REPLACE INTO evidence_objects "
                "(evidence_id,record_id,evidence_type,evidence_grade,evidence_url,local_file,"
                "content_sha256,observed_at,title_match,date_match,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{record.record_id}:{record.content_sha256}", record.record_id,
                    evidence_type, evidence_grade, evidence_url, record.raw_file,
                    record.content_sha256, record.retrieved_at, 1 if is_current_official else 0, 1,
                    ("Pasaxon current official-site body; article timestamp and DOM body verified."
                     if is_current_official else
                     "Pasaxon official old-site body recovered from Arquivo.pt; URL day and local hashes verified."
                     if archive_provider == "Arquivo.pt" else
                     "Pasaxon official old-site body; URL day verified; corrupt title replaced by first visible body line."),
                ),
            )
            imported += 1
        except Exception as exc:
            failed += 1
            raise ValueError(f"invalid staging row {line_number}: {exc}") from exc
    conn.commit()
    return {"imported": imported, "skipped": skipped, "failed": failed}


def import_pasaxon_commoncrawl(
    conn: sqlite3.Connection, paths: ProjectPaths, ndjson_path: Path
) -> dict[str, int]:
    """Validate and idempotently merge Pasaxon bodies recovered from ARC/WARC ranges."""
    imported = skipped = failed = 0
    for line_number, line in enumerate(ndjson_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            body = str(item["body_original"])
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != item["content_sha256"]:
                raise ValueError("body SHA-256 mismatch")
            raw_path = _safe_file(paths.root, item["raw_file"])
            raw_bytes = raw_path.read_bytes()
            if hashlib.sha256(raw_bytes).hexdigest() != item["raw_sha256"]:
                raise ValueError("ARC/WARC member SHA-256 mismatch")
            if item.get("payload_sha256"):
                payload = extract_archive_http_payload(raw_bytes)
                if hashlib.sha256(payload).hexdigest() != item["payload_sha256"]:
                    raise ValueError("archived HTTP payload SHA-256 mismatch")
            original_url = _canonical_url(item["original_url"])
            duplicate = conn.execute(
                "SELECT record_id FROM articles WHERE lower(replace(original_url,':80/','/'))=?",
                (original_url.lower(),),
            ).fetchone()
            if duplicate:
                skipped += 1
                continue
            suffix = hashlib.sha256(original_url.encode("utf-8")).hexdigest()[:16]
            record = ArticleRecord(
                record_id=f"PASAXON-CC-LO-{suffix}", source_code="pasaxon_archive",
                source_article_id=None, story_id=f"PASAXON-CC-STORY-{suffix}", language="lo",
                title_original=_usable_title(str(item.get("title_original") or ""), body),
                published_at=item["published_at"], date_precision=item.get("date_precision", "url_day"),
                body_original=body, body_method=item["body_method"],
                china_note_zh="Pasaxon官方旧站网页的Common Crawl存档，正文直接命中涉华老挝语关键词；具体主题待人工精编。",
                topic_labels=["china_general"], content_origin="pasaxon_archive",
                matched_queries=list(item.get("matched_queries") or []), original_url=original_url,
                archive_url=item["archive_url"], body_file=item.get("body_file"),
                raw_file=item["raw_file"], evidence_grade="B1",
                retrieval_tier=item.get("retrieval_tier", "T1_DIRECT_CHINA"),
                content_sha256=item["content_sha256"], retrieved_at=item.get("retrieved_at"),
                metadata={"capture_timestamp": item.get("capture_timestamp"), "warc": item.get("warc"),
                          "old_pasaxon_slot": item.get("old_pasaxon_slot"), "parser": item.get("parser"),
                          "cleaning": item.get("cleaning"),
                          "raw_member_sha256": item["raw_sha256"],
                          "payload_sha256": item.get("payload_sha256"),
                          "staging_import": str(ndjson_path.relative_to(paths.root))},
            )
            upsert_article(conn, record)
            conn.execute(
                "INSERT OR REPLACE INTO evidence_objects "
                "(evidence_id,record_id,evidence_type,evidence_grade,evidence_url,local_file,"
                "content_sha256,observed_at,title_match,date_match,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"{record.record_id}:{record.content_sha256}", record.record_id,
                 "commoncrawl_official_html", "B1", record.archive_url, record.raw_file,
                 record.content_sha256, record.retrieved_at,
                 1 if item.get("parser") == "pasaxon_php_clean_v1" else 0, 1,
                 ("Pasaxon PHP article recovered from a Common Crawl WARC member after explicit hidden "
                  "SEO-injection nodes were removed; article heading and page date were verified against "
                  "the archived official listing title/date."
                  if item.get("parser") == "pasaxon_php_clean_v1" else
                  "Pasaxon official old-site body recovered from a Common Crawl ARC/WARC range; URL day verified.")),
            )
            imported += 1
        except Exception as exc:
            failed += 1
            raise ValueError(f"invalid Common Crawl staging row {line_number}: {exc}") from exc
    conn.commit()
    return {"imported": imported, "skipped": skipped, "failed": failed}


def import_pasaxon_listing_discoveries(
    conn: sqlite3.Connection, paths: ProjectPaths, ndjson_path: Path
) -> dict[str, int]:
    """Merge verified official listing leads as A2 candidates, never as samples."""

    imported = skipped = evidence_linked = failed = 0
    existing_by_pid = {
        p_id: str(row[0])
        for row in conn.execute(
            "SELECT record_id,original_url FROM articles WHERE source_code='pasaxon_archive' "
            "AND original_url IS NOT NULL"
        )
        if (p_id := _pasaxon_php_id(str(row[1])))
    }
    for line_number, line in enumerate(ndjson_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            p_id = str(item["p_id"])
            if not p_id.isdigit():
                raise ValueError("invalid Pasaxon p_id")
            published = str(item["published_at"])
            if not (published.startswith("2021-") or published.startswith("2022-")):
                raise ValueError("listing publication date is outside 2021--2022")
            title = str(item["title_original"]).strip()
            if sum("\u0e80" <= char <= "\u0eff" for char in title) < 3:
                raise ValueError("listing title lacks Lao text")
            raw_path = _safe_file(paths.root, item["raw_file"])
            raw = raw_path.read_bytes()
            if str(raw_path).casefold().endswith(".html.gz"):
                payload = gzip.decompress(raw)
                if hashlib.sha256(payload).hexdigest() != item["raw_sha256"]:
                    raise ValueError("listing Wayback response SHA-256 mismatch")
            else:
                if hashlib.sha256(raw).hexdigest() != item["raw_sha256"]:
                    raise ValueError("listing WARC member SHA-256 mismatch")
                payload = extract_archive_http_payload(raw)
            if hashlib.sha256(payload).hexdigest() != item["payload_sha256"]:
                raise ValueError("listing HTTP payload SHA-256 mismatch")
            record_id = existing_by_pid.get(p_id)
            if record_id:
                skipped += 1
            else:
                suffix = hashlib.sha256(f"pasaxon-listing:{p_id}".encode("utf-8")).hexdigest()[:16]
                record = ArticleRecord(
                    record_id=f"PASAXON-CC-LIST-LO-{suffix}",
                    source_code="pasaxon_archive", source_article_id=p_id,
                    story_id=f"PASAXON-CC-LIST-STORY-{suffix}", language="lo",
                    title_original=title, excerpt_original=title, published_at=published,
                    date_precision="official_listing_day", body_method="none",
                    china_note_zh="Pasaxon官方历史列表页标题直接涉及中国；正文尚待恢复，不能用于月度样本配额。",
                    topic_labels=["china_general"], content_origin="pasaxon_archive",
                    matched_queries=list(item.get("matched_queries") or []),
                    original_url=_canonical_url(item["original_url"]),
                    archive_url=item["archive_url"], raw_file=item["raw_file"],
                    evidence_grade="A2", retrieval_tier="T1_DIRECT_CHINA",
                    retrieved_at=item.get("retrieved_at"),
                    metadata={
                        "p_id": p_id, "listing_url": item.get("listing_url"),
                        "listing_kind": item.get("listing_kind"),
                        "capture_timestamp": item.get("capture_timestamp"),
                        "source_index": item.get("source_index"),
                        "archive_provider": item.get("archive_provider", "Common Crawl"),
                        "evidence_scope": item.get("evidence_scope"),
                        "raw_member_sha256": item["raw_sha256"],
                        "payload_sha256": item["payload_sha256"],
                        "staging_import": str(ndjson_path.relative_to(paths.root)),
                    },
                )
                upsert_article(conn, record)
                record_id = record.record_id
                existing_by_pid[p_id] = record_id
                imported += 1
            conn.execute(
                "INSERT OR REPLACE INTO evidence_objects "
                "(evidence_id,record_id,evidence_type,evidence_grade,evidence_url,local_file,"
                "content_sha256,observed_at,title_match,date_match,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"{record_id}:listing:{item['payload_sha256']}", record_id,
                 ("wayback_official_listing_html" if item.get("archive_provider") == "Wayback"
                  else "commoncrawl_official_listing_html"), "A2", item["archive_url"],
                 item["raw_file"], item["payload_sha256"], item.get("retrieved_at"), 1, 1,
                 "Archived official Pasaxon listing preserves the article p_id, Lao title, and publication date; "
                 "listing evidence alone does not supply article body text."),
            )
            evidence_linked += 1
        except Exception as exc:
            failed += 1
            raise ValueError(f"invalid listing discovery row {line_number}: {exc}") from exc
    conn.commit()
    return {
        "imported": imported, "skipped_existing_body_or_candidate": skipped,
        "evidence_linked": evidence_linked, "failed": failed,
    }


def import_pasaxon_wayback_upgrades(
    conn: sqlite3.Connection, paths: ProjectPaths, ndjson_path: Path
) -> dict[str, int]:
    """Upgrade existing 2021--2022 A2 listing candidates with verified Wayback bodies."""

    upgraded = skipped = failed = 0
    for line_number, line in enumerate(ndjson_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            existing = conn.execute(
                "SELECT * FROM articles WHERE record_id=?", (item["record_id"],)
            ).fetchone()
            if not existing:
                raise ValueError("target A2 record does not exist")
            if str(existing["source_article_id"]) != str(item["source_article_id"]):
                raise ValueError("source article id mismatch")
            if str(existing["published_at"]) != str(item["published_at"]):
                raise ValueError("Wayback page date conflicts with A2 listing date")
            if not titles_agree(str(item["title_original"]), str(existing["title_original"])):
                raise ValueError("Wayback page title conflicts with A2 listing title")
            body = str(item["body_original"])
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != item["content_sha256"]:
                raise ValueError("body SHA-256 mismatch")
            body_path = _safe_file(paths.root, item["body_file"])
            if hashlib.sha256(body_path.read_bytes()).hexdigest() != item["content_sha256"]:
                raise ValueError("body file SHA-256 mismatch")
            raw_path = _safe_file(paths.root, item["raw_file"])
            raw_payload = gzip.decompress(raw_path.read_bytes())
            if hashlib.sha256(raw_payload).hexdigest() != item["raw_sha256"]:
                raise ValueError("raw Wayback response SHA-256 mismatch")
            if existing["body_original"]:
                if existing["content_sha256"] == item["content_sha256"]:
                    skipped += 1
                    continue
                raise ValueError("target record already has a different body")
            metadata = json.loads(existing["metadata_json"] or "{}")
            metadata.update(dict(item.get("metadata") or {}))
            metadata.update({
                "archive_capture_timestamp": item.get("archive_capture_timestamp"),
                "archive_digest": item.get("archive_digest"),
                "raw_response_sha256": item["raw_sha256"],
                "staging_upgrade": str(ndjson_path.relative_to(paths.root)),
            })
            conn.execute(
                "UPDATE articles SET title_original=?,body_original=?,body_method=?,date_precision=?,"
                "china_note_zh=?,matched_queries_json=?,archive_url=?,body_file=?,raw_file=?,"
                "evidence_grade='B1',retrieval_tier=?,content_sha256=?,retrieved_at=?,metadata_json=? "
                "WHERE record_id=?",
                (
                    item["title_original"], body, item["body_method"], item["date_precision"],
                    "Pasaxon官方历史列表与Wayback详情页共同核验；隐藏SEO注入已限定节点清除，正文直接涉及中国。",
                    json.dumps(list(item.get("matched_queries") or []), ensure_ascii=False),
                    item["archive_url"], item["body_file"], item["raw_file"],
                    item.get("retrieval_tier", "T1_DIRECT_CHINA"), item["content_sha256"],
                    item.get("retrieved_at"), json.dumps(metadata, ensure_ascii=False), item["record_id"],
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO evidence_objects "
                "(evidence_id,record_id,evidence_type,evidence_grade,evidence_url,local_file,"
                "content_sha256,observed_at,title_match,date_match,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"{item['record_id']}:wayback:{item['content_sha256']}", item["record_id"],
                 "wayback_official_html_cleaned", "B1", item["archive_url"], item["raw_file"],
                 item["content_sha256"], item.get("retrieved_at"), 1, 1,
                 "Wayback detail body verified against the archived official Pasaxon listing title/date; "
                 "explicit hidden SEO-injection nodes were removed before body extraction."),
            )
            upgraded += 1
        except Exception as exc:
            failed += 1
            raise ValueError(f"invalid Wayback upgrade row {line_number}: {exc}") from exc
    conn.commit()
    return {"upgraded": upgraded, "skipped": skipped, "failed": failed}


def import_pasaxon_epaper_articles(
    conn: sqlite3.Connection, paths: ProjectPaths, ndjson_path: Path
) -> dict[str, int]:
    """Import article regions only after verifying their PDF, text and parser evidence."""

    imported = skipped = failed = 0
    for line_number, line in enumerate(ndjson_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if item.get("body_method") != "liteparse_native_pdf_region":
                raise ValueError("unexpected e-paper body method")
            if item.get("evidence_grade") != "B1" or item.get("source_code") != "pasaxon_archive":
                raise ValueError("unexpected source or evidence grade")
            body = str(item["body_original"])
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != item["content_sha256"]:
                raise ValueError("body SHA-256 mismatch")
            body_path = _safe_file(paths.root, item["body_file"])
            if hashlib.sha256(body_path.read_bytes()).hexdigest() != item["content_sha256"]:
                raise ValueError("body file SHA-256 mismatch")
            pdf_path = _safe_file(paths.root, item["raw_file"])
            pdf_bytes = pdf_path.read_bytes()
            if not pdf_bytes.startswith(b"%PDF"):
                raise ValueError("raw e-paper file is not a PDF")
            if hashlib.sha256(pdf_bytes).hexdigest() != item["raw_sha256"]:
                raise ValueError("PDF SHA-256 mismatch")
            native_path = _safe_file(paths.root, item["native_json_file"])
            native_bytes = native_path.read_bytes()
            if hashlib.sha256(native_bytes).hexdigest() != item["native_json_sha256"]:
                raise ValueError("native parser JSON SHA-256 mismatch")
            native = json.loads(native_bytes)
            metadata = dict(item.get("metadata") or {})
            if str(native["pdf_sha256"]) != item["raw_sha256"]:
                raise ValueError("native parser JSON points to a different PDF")
            if str(native["published_at"]) != str(item["published_at"]):
                raise ValueError("native parser JSON date mismatch")
            if int(metadata["page_num"]) not in {int(page["page_num"]) for page in native["pages"]}:
                raise ValueError("article page missing from native parser JSON")
            existing = conn.execute(
                "SELECT record_id,content_sha256 FROM articles WHERE record_id=?", (item["record_id"],)
            ).fetchone()
            if existing:
                if existing["content_sha256"] != item["content_sha256"]:
                    raise ValueError("record id already exists with a different body")
                metadata = json.loads(conn.execute(
                    "SELECT metadata_json FROM articles WHERE record_id=?", (item["record_id"],)
                ).fetchone()[0] or "{}")
                metadata.update({
                    "source_body_file": item["body_file"],
                    "source_body_sha256": item["content_sha256"],
                    "native_json_file": item["native_json_file"],
                    "native_json_sha256": item["native_json_sha256"],
                })
                conn.execute(
                    "UPDATE articles SET body_file=?,metadata_json=? WHERE record_id=?",
                    (item["body_file"], json.dumps(metadata, ensure_ascii=False), item["record_id"]),
                )
                skipped += 1
                continue
            record = ArticleRecord(
                record_id=item["record_id"], source_code="pasaxon_archive",
                source_article_id=item["source_article_id"], story_id=item["story_id"],
                language="lo", title_original=item["title_original"],
                published_at=item["published_at"], date_precision="official_issue_day",
                body_original=body, body_method=item["body_method"],
                china_note_zh=item["china_note_zh"], topic_labels=list(item["topic_labels"]),
                content_origin=item["content_origin"], matched_queries=list(item["matched_queries"]),
                original_url=_canonical_url(item["original_url"]), archive_url=item["archive_url"],
                body_file=item["body_file"], raw_file=item["raw_file"], evidence_grade="B1",
                retrieval_tier=item.get("retrieval_tier", "T1_DIRECT_CHINA"),
                content_sha256=item["content_sha256"], retrieved_at=item.get("retrieved_at"),
                metadata={**metadata, "source_body_file": item["body_file"],
                          "source_body_sha256": item["content_sha256"],
                          "native_json_file": item["native_json_file"],
                          "native_json_sha256": item["native_json_sha256"],
                          "staging_import": str(ndjson_path.relative_to(paths.root))},
            )
            upsert_article(conn, record)
            conn.execute(
                "INSERT OR REPLACE INTO evidence_objects "
                "(evidence_id,record_id,evidence_type,evidence_grade,evidence_url,local_file,"
                "content_sha256,observed_at,title_match,date_match,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"{record.record_id}:pdf:{item['raw_sha256']}", record.record_id,
                 "wayback_official_epaper_pdf_native_text", "B1", record.archive_url,
                 record.raw_file, item["raw_sha256"], record.retrieved_at, 1, 1,
                 "Official Pasaxon issue PDF recovered through Wayback; article title/boundary visually "
                 "reviewed and body extracted from native PDF text using recorded page/column coordinates."),
            )
            imported += 1
        except Exception as exc:
            failed += 1
            raise ValueError(f"invalid e-paper staging row {line_number}: {exc}") from exc
    conn.commit()
    return {"imported": imported, "skipped": skipped, "failed": failed}
