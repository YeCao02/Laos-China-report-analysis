from __future__ import annotations

import json
import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from .adapters.kpl import fetch_search_pages, parse_detail_page, plan_search_partitions
from .adapters.pasaxon import (
    build_tag_url,
    parse_article,
    parse_epaper_results,
    parse_search_results,
    plan_small_crawl,
)
from .archives.commoncrawl import build_index_query, fetch_index, records_to_historical_candidates
from .archives.wayback import (
    build_cdx_query,
    build_exact_cdx_query,
    fetch_cdx_year,
    fetch_exact_cdx,
    is_lao_china_title,
    is_china_related,
    load_cdx,
    parse_old_kpl_article,
    parse_old_pasaxon_article,
    parse_old_pasaxon_home,
    prioritized_captures,
    save_cdx,
)
from .config import EN_DIRECT_QUERIES, LAO_DIRECT_QUERIES, ProjectPaths
from .db import upsert_article
from .http_client import RateLimitedFetcher
from .models import ArticleRecord
from .sampling import rank_hydration_candidates


@dataclass(slots=True)
class CrawlSummary:
    listings_fetched: int = 0
    discoveries: int = 0
    articles_fetched: int = 0
    articles_upserted: int = 0
    failures: int = 0


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _append_failure(paths: ProjectPaths, task: str, url: str, error: Exception) -> None:
    paths.audit.mkdir(parents=True, exist_ok=True)
    payload = {
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "task": task,
        "url": url,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    with (paths.audit / "failed_tasks.ndjson").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _save_partition(
    conn: sqlite3.Connection,
    *,
    partition_id: str,
    source: str,
    query: str,
    page: int,
    parsed_rows: int,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO crawl_partitions "
        "(partition_id,source_code,query,page,parsed_rows,status,attempts,last_checked_at,error) "
        "VALUES (?,?,?,?,?,?,1,?,?) ON CONFLICT(partition_id) DO UPDATE SET "
        "parsed_rows=excluded.parsed_rows,status=excluded.status,attempts=crawl_partitions.attempts+1,"
        "last_checked_at=excluded.last_checked_at,error=excluded.error",
        (
            partition_id,
            source,
            query,
            page,
            parsed_rows,
            status,
            datetime.now(timezone.utc).isoformat(),
            error,
        ),
    )


def _save_evidence(
    conn: sqlite3.Connection, record: ArticleRecord, *, evidence_type: str = "official_html"
) -> None:
    evidence_id = f"{record.record_id}:{record.content_sha256 or 'nohash'}"
    conn.execute(
        "INSERT OR REPLACE INTO evidence_objects "
        "(evidence_id,record_id,evidence_type,evidence_grade,evidence_url,local_file,"
        "content_sha256,observed_at,title_match,date_match,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            evidence_id,
            record.record_id,
            evidence_type,
            record.evidence_grade,
            record.original_url,
            record.raw_file,
            record.content_sha256,
            record.retrieved_at,
            1,
            int(bool(record.published_at)),
            "Official source HTML parsed by source-specific DOM adapter",
        ),
    )


def crawl_pasaxon_probe(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    queries: Iterable[str] = LAO_DIRECT_QUERIES,
    pages_per_query: int = 2,
    epaper_pages: int = 1,
    max_articles: int = 30,
    published_after: date | None = None,
    force: bool = False,
) -> CrawlSummary:
    """Run a bounded, respectful current-site probe and import verified articles."""

    fetcher = RateLimitedFetcher(paths)
    summary = CrawlSummary()
    discoveries: dict[str, dict[str, object]] = {}
    for request in plan_small_crawl(
        queries, pages_per_query=pages_per_query, epaper_pages=epaper_pages
    ):
        partition_id = f"pasaxon:{request.kind}:{request.query or 'epaper'}:{request.page}"
        try:
            result = fetcher.fetch(request.url, "pasaxon", force=force)
            html = result.payload.decode("utf-8", errors="replace")
            listing = (
                parse_epaper_results(html, request.url)
                if request.kind == "epaper"
                else parse_search_results(html, request.url, request.query or "")
            )
            summary.listings_fetched += 1
            summary.discoveries += len(listing.discoveries)
            _save_partition(
                conn,
                partition_id=partition_id,
                source="PASAXON",
                query=request.query or "__epaper__",
                page=request.page,
                parsed_rows=len(listing.discoveries),
                status="complete",
            )
            for item in listing.discoveries:
                if item.kind != "article":
                    continue
                bucket = discoveries.setdefault(
                    item.url,
                    {"queries": [], "search_urls": [], "published_at": item.published_at},
                )
                if item.query and item.query not in bucket["queries"]:
                    bucket["queries"].append(item.query)
                if item.search_url and item.search_url not in bucket["search_urls"]:
                    bucket["search_urls"].append(item.search_url)
        except Exception as exc:
            summary.failures += 1
            _save_partition(
                conn,
                partition_id=partition_id,
                source="PASAXON",
                query=request.query or "__epaper__",
                page=request.page,
                parsed_rows=0,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            _append_failure(paths, "pasaxon_listing", request.url, exc)
            if type(exc).__name__ == "FetchBlocked":
                break

    for url, context in list(discoveries.items())[:max_articles]:
        try:
            result = fetcher.fetch(url, "pasaxon", force=force)
            summary.articles_fetched += 1
            article = parse_article(
                result.payload.decode("utf-8", errors="replace"),
                url,
                matched_queries=context["queries"],
                search_url=(context["search_urls"] or [None])[0],
                retrieved_at=result.fetched_at,
            )
            article.raw_file = _relative(result.raw_file, paths.root)
            article.metadata["search_urls"] = context["search_urls"]
            article.metadata["response_sha256"] = result.content_sha256
            article.china_note_zh = "该报道涉及中国或中老关系；具体事项应结合原文正文进一步编码。"
            if published_after and article.published_at and article.published_at[:10] < published_after.isoformat():
                continue
            upsert_article(conn, article)
            _save_evidence(conn, article)
            summary.articles_upserted += 1
        except Exception as exc:
            summary.failures += 1
            _append_failure(paths, "pasaxon_article", url, exc)
    conn.commit()
    return summary


def fetch_wayback_kpl_index(paths: ProjectPaths, *, year: int) -> dict[str, object]:
    captures = fetch_cdx_year(year)
    output = paths.staging / "archive_ocr" / f"wayback_kpl_{year}_html.json"
    count = save_cdx(captures, output)
    return {"year": year, "captures": count, "output": str(output), "query": build_cdx_query(year)}


def fetch_wayback_pasaxon_index(paths: ProjectPaths, *, year: int) -> dict[str, object]:
    url = "http://www.pasaxon.org.la/"
    captures = fetch_exact_cdx(url, year)
    output = paths.staging / "archive_ocr" / f"wayback_pasaxon_home_{year}.json"
    count = save_cdx(captures, output)
    return {
        "year": year,
        "captures": count,
        "output": str(output),
        "query": build_exact_cdx_query(url, year),
    }


def crawl_wayback_kpl(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    index_file: Path,
    scan_per_month: int = 30,
    target_per_month: int = 4,
    force: bool = False,
) -> dict[str, object]:
    """Scan official archived KPL pages, stopping after a bounded per-month quota."""

    captures = load_cdx(index_file)
    groups = prioritized_captures(captures)
    fetcher = RateLimitedFetcher(paths)
    found_by_month: dict[str, int] = defaultdict(int)
    scanned_by_month: dict[str, int] = defaultdict(int)
    failures = 0
    skipped_existing = 0
    skipped_screened = 0
    for month, candidates in groups.items():
        existing = conn.execute(
            "SELECT count(*) FROM articles WHERE source_code='kpl_english_archive' "
            "AND substr(published_at,1,7)=?",
            (month,),
        ).fetchone()[0]
        found_by_month[month] = int(existing)
        if existing >= target_per_month:
            continue
        for capture in candidates:
            if scanned_by_month[month] >= scan_per_month or found_by_month[month] >= target_per_month:
                break
            partition_id = f"wayback:kpl:{capture.timestamp}:{hashlib.sha256(capture.original_url.encode()).hexdigest()[:12]}"
            screened = conn.execute(
                "SELECT 1 FROM crawl_partitions WHERE partition_id=? AND status IN "
                "('china_match','screened_not_relevant')",
                (partition_id,),
            ).fetchone()
            if screened:
                skipped_screened += 1
                continue
            prior = conn.execute(
                "SELECT 1 FROM articles WHERE original_url=? LIMIT 1", (capture.original_url,)
            ).fetchone()
            if prior:
                skipped_existing += 1
                continue
            scanned_by_month[month] += 1
            try:
                result = fetcher.fetch(capture.replay_url, "wayback_kpl", force=force)
                parsed = parse_old_kpl_article(result.payload, capture.original_url)
                related, hits = is_china_related(parsed)
                _save_partition(
                    conn,
                    partition_id=partition_id,
                    source="KPL_ARCHIVE",
                    query="China|Chinese|Lao-China|Yunnan|Beijing|Guangxi|Kunming",
                    page=parsed.slot,
                    parsed_rows=1,
                    status="china_match" if related else "screened_not_relevant",
                )
                if not related:
                    conn.commit()
                    continue
                suffix = hashlib.sha256(capture.original_url.encode("utf-8")).hexdigest()[:16]
                record = ArticleRecord(
                    record_id=f"KPL-ARCH-EN-{suffix}",
                    source_code="kpl_english_archive",
                    source_article_id=None,
                    story_id=f"KPL-ARCH-STORY-{suffix}",
                    language="en",
                    title_original=parsed.title,
                    published_at=parsed.published_date,
                    date_precision="url_day",
                    body_original=parsed.body,
                    body_method="wayback_old_kpl_html",
                    china_note_zh="KPL旧站官方存档报道涉及中国或中老关系；详细主题待后续人工编码。",
                    topic_labels=["china_general"],
                    content_origin="kpl_archive",
                    matched_queries=hits,
                    original_url=capture.original_url,
                    archive_url=capture.replay_url,
                    search_url=build_cdx_query(int(parsed.published_date[:4])),
                    raw_file=_relative(result.raw_file, paths.root),
                    evidence_grade="B1",
                    retrieval_tier="T1_DIRECT_CHINA",
                    content_sha256=hashlib.sha256(parsed.body.encode("utf-8")).hexdigest(),
                    retrieved_at=result.fetched_at,
                    metadata={
                        "archive_capture_timestamp": capture.timestamp,
                        "archive_digest": capture.digest,
                        "section": parsed.section,
                        "old_kpl_slot": parsed.slot,
                        "parser": "old_kpl_wayback_v1",
                    },
                )
                upsert_article(conn, record)
                _save_evidence(conn, record, evidence_type="wayback_official_html")
                found_by_month[month] += 1
                conn.commit()
            except Exception as exc:
                failures += 1
                _save_partition(
                    conn,
                    partition_id=partition_id,
                    source="KPL_ARCHIVE",
                    query="China archive screening",
                    page=0,
                    parsed_rows=0,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                _append_failure(paths, "wayback_kpl", capture.replay_url, exc)
                conn.commit()
    return {
        "months_in_index": len(groups),
        "captures_in_index": len(captures),
        "scanned": sum(scanned_by_month.values()),
        "found_total": sum(found_by_month.values()),
        "found_by_month": dict(sorted(found_by_month.items())),
        "scanned_by_month": dict(sorted(scanned_by_month.items())),
        "skipped_existing": skipped_existing,
        "skipped_screened": skipped_screened,
        "failures": failures,
    }


def crawl_wayback_pasaxon(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    index_file: Path,
    scan_homepages_per_month: int = 20,
    target_per_month: int = 2,
    force: bool = False,
) -> dict[str, object]:
    """Use archived Pasaxon home pages as daily tables of contents."""

    captures = load_cdx(index_file)
    fetcher = RateLimitedFetcher(paths)
    by_capture_month: dict[str, list] = defaultdict(list)
    for capture in sorted(captures, key=lambda item: item.timestamp):
        by_capture_month[f"{capture.timestamp[:4]}-{capture.timestamp[4:6]}"].append(capture)
    found_by_month = {
        row["ym"]: row["n"]
        for row in conn.execute(
            "SELECT substr(published_at,1,7) ym,count(*) n FROM articles "
            "WHERE source_code='pasaxon_archive' GROUP BY ym"
        )
    }
    scanned_by_month: dict[str, int] = defaultdict(int)
    failures = 0
    screened_skips = 0
    details_fetched = 0
    for capture_month, month_captures in sorted(by_capture_month.items()):
        if found_by_month.get(capture_month, 0) >= target_per_month:
            continue
        for capture in month_captures:
            if scanned_by_month[capture_month] >= scan_homepages_per_month:
                break
            home_partition = f"wayback:pasaxon:home:{capture.timestamp}:{capture.digest or 'nodigest'}"
            if conn.execute(
                "SELECT 1 FROM crawl_partitions WHERE partition_id=? AND status='complete_v3'",
                (home_partition,),
            ).fetchone():
                screened_skips += 1
                continue
            scanned_by_month[capture_month] += 1
            try:
                home_result = fetcher.fetch(capture.replay_url, "wayback_pasaxon", force=force)
                links = parse_old_pasaxon_home(home_result.payload, capture.original_url)
                _save_partition(
                    conn,
                    partition_id=home_partition,
                    source="PASAXON_ARCHIVE",
                    query="ຈີນ|ສປ ຈີນ|ລາວ-ຈີນ|ຈີນ-ລາວ",
                    page=0,
                    parsed_rows=len(links),
                    status="complete_v3",
                )
                for link in links:
                    provisional_month = (
                        link.published_date[:7] if link.published_date else capture_month
                    )
                    if found_by_month.get(provisional_month, 0) >= target_per_month:
                        continue
                    related, hits = is_lao_china_title(link.title)
                    if not related:
                        continue
                    if conn.execute(
                        "SELECT 1 FROM articles WHERE original_url=? LIMIT 1", (link.original_url,)
                    ).fetchone():
                        continue
                    replay_url = f"https://web.archive.org/web/{capture.timestamp}id_/{link.original_url}"
                    detail_result = fetcher.fetch(replay_url, "wayback_pasaxon", force=force)
                    details_fetched += 1
                    published, body, slot = parse_old_pasaxon_article(
                        detail_result.payload, link.original_url, listing_title=link.title
                    )
                    month = published[:7]
                    if found_by_month.get(month, 0) >= target_per_month:
                        continue
                    suffix = hashlib.sha256(link.original_url.encode("utf-8")).hexdigest()[:16]
                    record = ArticleRecord(
                        record_id=f"PASAXON-ARCH-LO-{suffix}",
                        source_code="pasaxon_archive",
                        source_article_id=None,
                        story_id=f"PASAXON-ARCH-STORY-{suffix}",
                        language="lo",
                        title_original=link.title,
                        published_at=published,
                        date_precision="url_day",
                        body_original=body,
                        body_method="wayback_old_pasaxon_html",
                        china_note_zh="Pasaxon旧站官方存档报道，老挝文标题直接涉及中国或中老关系；详细主题待人工编码。",
                        topic_labels=["china_general"],
                        content_origin="pasaxon_archive",
                        matched_queries=hits,
                        original_url=link.original_url,
                        archive_url=replay_url,
                        search_url=capture.replay_url,
                        raw_file=_relative(detail_result.raw_file, paths.root),
                        evidence_grade="B1",
                        retrieval_tier="T1_DIRECT_CHINA",
                        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        retrieved_at=detail_result.fetched_at,
                        metadata={
                            "archive_capture_timestamp": capture.timestamp,
                            "archive_digest": capture.digest,
                            "old_pasaxon_slot": slot,
                            "parser": "old_pasaxon_wayback_v1",
                            "homepage_raw_file": _relative(home_result.raw_file, paths.root),
                        },
                    )
                    upsert_article(conn, record)
                    _save_evidence(conn, record, evidence_type="wayback_official_html")
                    found_by_month[month] = found_by_month.get(month, 0) + 1
                conn.commit()
                if found_by_month.get(capture_month, 0) >= target_per_month:
                    break
            except Exception as exc:
                failures += 1
                _save_partition(
                    conn,
                    partition_id=home_partition,
                    source="PASAXON_ARCHIVE",
                    query="China title archive screening",
                    page=0,
                    parsed_rows=0,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                _append_failure(paths, "wayback_pasaxon", capture.replay_url, exc)
                conn.commit()
    return {
        "captures_in_index": len(captures),
        "capture_months": len(by_capture_month),
        "homepages_scanned": sum(scanned_by_month.values()),
        "details_fetched": details_fetched,
        "found_total": sum(found_by_month.values()),
        "found_by_month": dict(sorted(found_by_month.items())),
        "scanned_by_month": dict(sorted(scanned_by_month.items())),
        "screened_skips": screened_skips,
        "failures": failures,
    }


def _pasaxon_discovery_record(discovery, *, query: str, raw_file: str, retrieved_at: str) -> ArticleRecord:
    stable_id = discovery.source_article_id or __import__("hashlib").sha256(
        discovery.url.encode("utf-8")
    ).hexdigest()[:16]
    return ArticleRecord(
        record_id=f"PASAXON-LO-{stable_id}",
        source_code="pasaxon",
        source_article_id=discovery.source_article_id,
        story_id=f"PASAXON-STORY-{stable_id}",
        language="lo",
        title_original=discovery.title_original,
        published_at=discovery.published_at,
        date_precision="listing_date" if discovery.published_at else "unknown",
        body_method="none",
        matched_queries=[query],
        original_url=discovery.url,
        search_url=discovery.search_url,
        raw_file=raw_file,
        evidence_grade="A2",
        retrieval_tier="T1_DIRECT_CHINA" if "ຈີນ" in query else "T3_ENTITY_TOPIC",
        retrieved_at=retrieved_at,
        metadata={"parser": "pasaxon_listing_v1", **discovery.metadata},
    )


def crawl_pasaxon_history(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    query: str = "ຈີນ",
    start_page: int = 1,
    max_pages: int = 100,
    date_from: date = date(2012, 1, 1),
    date_to: date = date(2020, 12, 31),
    force: bool = False,
) -> CrawlSummary:
    """Traverse Pasaxon tag pages and preserve dated listing discoveries as A2."""

    if start_page < 1 or max_pages < 1:
        raise ValueError("start_page and max_pages must be positive")
    fetcher = RateLimitedFetcher(paths)
    summary = CrawlSummary()
    url = build_tag_url(query, start_page)
    for offset in range(max_pages):
        page_number = start_page + offset
        partition_id = f"pasaxon:history:{query}:{page_number}"
        try:
            result = fetcher.fetch(url, "pasaxon", force=force)
            listing = parse_search_results(
                result.payload.decode("utf-8", errors="replace"), url, query
            )
            summary.listings_fetched += 1
            summary.discoveries += len(listing.discoveries)
            dated = []
            imported = 0
            for discovery in listing.discoveries:
                if discovery.kind != "article":
                    continue
                if discovery.published_at:
                    dated.append(discovery.published_at[:10])
                if not discovery.published_at or not (
                    date_from.isoformat() <= discovery.published_at[:10] <= date_to.isoformat()
                ):
                    continue
                record = _pasaxon_discovery_record(
                    discovery,
                    query=query,
                    raw_file=_relative(result.raw_file, paths.root),
                    retrieved_at=result.fetched_at,
                )
                upsert_article(conn, record)
                imported += 1
            summary.articles_upserted += imported
            _save_partition(
                conn,
                partition_id=partition_id,
                source="PASAXON",
                query=query,
                page=page_number,
                parsed_rows=len(listing.discoveries),
                status="complete",
            )
            conn.commit()
            if not listing.next_url:
                break
            if dated and max(dated) < date_from.isoformat():
                break
            url = listing.next_url
        except Exception as exc:
            summary.failures += 1
            _save_partition(
                conn,
                partition_id=partition_id,
                source="PASAXON",
                query=query,
                page=page_number,
                parsed_rows=0,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            _append_failure(paths, "pasaxon_history", url, exc)
            conn.commit()
            if type(exc).__name__ == "FetchBlocked":
                break
            url = build_tag_url(query, page_number + 1)
    return summary


def crawl_kpl_window(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    date_from: date,
    date_to: date,
    max_pages: int = 1,
    force: bool = False,
) -> CrawlSummary:
    """Discover KPL records in a bounded date window; no scheduler is created."""

    fetcher = RateLimitedFetcher(paths)
    summary = CrawlSummary()
    plans = []
    for query in LAO_DIRECT_QUERIES:
        plans.extend(plan_search_partitions("lo", [query], date_from, date_to))
    for query in EN_DIRECT_QUERIES:
        plans.extend(plan_search_partitions("en", [query], date_from, date_to))
    for partition in plans:
        partition_id = (
            f"kpl:{partition.language}:{partition.query}:{partition.date_from}:"
            f"{partition.date_to}:1-{max_pages}"
        )
        try:
            pages = fetch_search_pages(fetcher, partition, max_pages=max_pages, force=force)
            summary.listings_fetched += len(pages)
            parsed = 0
            for page in pages:
                for article in page.articles:
                    if not article.published_at or not (
                        date_from.isoformat() <= article.published_at[:10] <= date_to.isoformat()
                    ):
                        continue
                    if article.raw_file:
                        article.raw_file = _relative(Path(article.raw_file), paths.root)
                    upsert_article(conn, article)
                    parsed += 1
            summary.discoveries += parsed
            summary.articles_upserted += parsed
            _save_partition(
                conn,
                partition_id=partition_id,
                source="KPL",
                query=partition.query,
                page=max_pages,
                parsed_rows=parsed,
                status="complete",
            )
        except Exception as exc:
            summary.failures += 1
            _save_partition(
                conn,
                partition_id=partition_id,
                source="KPL",
                query=partition.query,
                page=max_pages,
                parsed_rows=0,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            _append_failure(paths, "kpl_window", partition.page_url(1), exc)
            if type(exc).__name__ == "FetchBlocked":
                break
    conn.commit()
    return summary


def _row_to_article(row: sqlite3.Row) -> ArticleRecord:
    return ArticleRecord(
        record_id=row["record_id"],
        source_code=row["source_code"],
        source_article_id=row["source_article_id"],
        story_id=row["story_id"],
        language=row["language"],
        title_original=row["title_original"],
        excerpt_original=row["excerpt_original"],
        body_original=row["body_original"],
        body_method=row["body_method"],
        published_at=row["published_at"],
        date_precision=row["date_precision"],
        china_note_zh=row["china_note_zh"],
        topic_labels=json.loads(row["topic_labels_json"] or "[]"),
        content_origin=row["content_origin"],
        matched_queries=json.loads(row["matched_queries_json"] or "[]"),
        original_url=row["original_url"],
        archive_url=row["archive_url"],
        search_url=row["search_url"],
        body_file=row["body_file"],
        raw_file=row["raw_file"],
        evidence_grade=row["evidence_grade"],
        retrieval_tier=row["retrieval_tier"],
        content_sha256=row["content_sha256"],
        ocr_confidence=row["ocr_confidence"],
        retrieved_at=row["retrieved_at"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def hydrate_kpl(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    limit: int = 20,
    date_from: date | None = None,
    date_to: date | None = None,
    per_month: int | None = None,
    force: bool = False,
) -> CrawlSummary:
    """Hydrate the highest-ranked not-yet-downloaded KPL seed records."""

    fetcher = RateLimitedFetcher(paths)
    summary = CrawlSummary()
    ranked = rank_hydration_candidates(conn, per_month=10)
    selected = [
        row for row in ranked
        if row["source_code"].upper().startswith("KPL")
        and not row["body_original"]
        and (date_from is None or (row["published_at"] or "")[:10] >= date_from.isoformat())
        and (date_to is None or (row["published_at"] or "")[:10] <= date_to.isoformat())
    ]
    if per_month is not None:
        monthly: dict[str, int] = defaultdict(int)
        balanced = []
        for row in selected:
            ym = row["published_at"][:7]
            if monthly[ym] >= per_month:
                continue
            balanced.append(row)
            monthly[ym] += 1
        selected = balanced
    for row in selected[:limit]:
        base = _row_to_article(row)
        url = base.original_url or ""
        if not url:
            continue
        try:
            result = fetcher.fetch(url, base.source_code, force=force)
            summary.articles_fetched += 1
            article = parse_detail_page(
                result.payload,
                url,
                base_record=base,
                retrieved_at=result.fetched_at,
                raw_file=_relative(result.raw_file, paths.root),
                response_sha256=result.content_sha256,
            )
            article.china_note_zh = article.china_note_zh or "该报道涉及中国或中老关系；具体事项应结合原文正文进一步编码。"
            upsert_article(conn, article)
            _save_evidence(conn, article)
            summary.articles_upserted += 1
        except Exception as exc:
            summary.failures += 1
            _append_failure(paths, "kpl_hydration", url, exc)
            if type(exc).__name__ == "FetchBlocked":
                break
    conn.commit()
    return summary


def discover_commoncrawl(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    index: str,
    domain: str,
    page_size: int = 200,
    url_pattern: str | None = None,
    match_type: str = "domain",
) -> dict[str, object]:
    """Write bounded C2 archive discoveries without inventing publication dates."""

    pattern = (url_pattern or domain).rstrip("/")
    query_url = build_index_query(
        index,
        pattern,
        filters=("status:200", "mime:text/html"),
        collapse="urlkey",
        match_type=match_type,
        page=0,
        page_size=page_size,
    )
    pattern_key = hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:12]
    partition_id = f"commoncrawl:{index}:{domain}:{pattern_key}:0"
    output = paths.staging / "archive_ocr" / f"commoncrawl_{domain.replace('.', '_')}_{index}_{pattern_key}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        records = fetch_index(query_url)
        candidates = records_to_historical_candidates(records)
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            for candidate in candidates:
                handle.write(json.dumps(candidate.to_dict(), ensure_ascii=False) + "\n")
        _save_partition(
            conn,
            partition_id=partition_id,
            source="ARCHIVE",
            query=f"CommonCrawl {index} {pattern}",
            page=0,
            parsed_rows=len(candidates),
            status="complete",
        )
        conn.commit()
        return {"index": index, "domain": domain, "url_pattern": pattern, "match_type": match_type, "candidates": len(candidates), "output": str(output)}
    except Exception as exc:
        _save_partition(
            conn,
            partition_id=partition_id,
            source="ARCHIVE",
            query=f"CommonCrawl {index} {pattern}",
            page=0,
            parsed_rows=0,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        _append_failure(paths, "commoncrawl_index", query_url, exc)
        conn.commit()
        raise
