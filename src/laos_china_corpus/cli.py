from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .acquire import (
    crawl_kpl_window,
    crawl_pasaxon_history,
    crawl_pasaxon_probe,
    crawl_wayback_kpl,
    crawl_wayback_pasaxon,
    discover_commoncrawl,
    fetch_wayback_kpl_index,
    fetch_wayback_pasaxon_index,
    hydrate_kpl,
)
from .audit import export_backfill_queue
from .clustering import assign_story_ids
from .config import ProjectPaths
from .db import connect
from .exporter import export_articles, export_monthly_coverage
from .manifest import write_manifest
from .inventory import export_source_year_inventory
from .quality import build_quality_report
from .reporting import build_status_report_artifact
from .sampling import rank_hydration_candidates, select_monthly_sample
from .staging_import import (
    import_pasaxon_commoncrawl, import_pasaxon_epaper_articles, import_pasaxon_listing_discoveries,
    import_pasaxon_round2, import_pasaxon_wayback_upgrades,
)


def cmd_import_pasaxon_epaper(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    ndjson = Path(args.input).resolve() if args.input else (
        paths.staging / "archive_ocr/pasaxon_epaper_archive_2022/article_records.ndjson"
    )
    conn = connect(paths.database)
    result = import_pasaxon_epaper_articles(conn, paths, ndjson)
    conn.close()
    print(json.dumps(result, ensure_ascii=False))
from .translation import queue_selected_translations


def default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _paths(value: str | None) -> ProjectPaths:
    paths = ProjectPaths(Path(value).resolve() if value else default_root())
    paths.ensure()
    return paths


def cmd_init(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    write_manifest(conn, paths, command="init")
    conn.close()
    print(paths.database)


def cmd_import_preview(args: argparse.Namespace) -> None:
    from .import_preview import import_preview

    paths = _paths(args.root)
    preview = Path(args.input).resolve() if args.input else paths.root.parent / "preview.md"
    conn = connect(paths.database)
    count = import_preview(conn, preview)
    conn.commit()
    conn.close()
    print(json.dumps({"imported": count, "database": str(paths.database)}, ensure_ascii=False))


def cmd_cluster(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    updated = assign_story_ids(conn)
    conn.close()
    print(json.dumps({"updated": updated}, ensure_ascii=False))


def cmd_plan_hydration(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    rows = rank_hydration_candidates(conn, per_month=args.per_month)
    out = paths.audit / "hydration_queue.csv"
    fields = ("record_id", "story_id", "source_code", "language", "published_at", "title_original", "original_url", "evidence_grade")
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fields})
    conn.close()
    print(json.dumps({"planned": len(rows), "output": str(out)}, ensure_ascii=False))


def cmd_sample(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    count = select_monthly_sample(
        conn, sample_id=args.sample_id, target=args.target, require_body=not args.allow_excerpts
    )
    conn.close()
    print(json.dumps({"selected": count, "require_body": not args.allow_excerpts}, ensure_ascii=False))


def cmd_queue_translations(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    count = queue_selected_translations(conn, paths.root)
    out = paths.catalog / "translation_queue.jsonl"
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        for row in conn.execute("SELECT * FROM translation_queue ORDER BY record_id"):
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    conn.close()
    print(json.dumps({"queued": count, "output": str(out)}, ensure_ascii=False))


def cmd_export(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    article_count = export_articles(conn, paths, per_record=not args.no_per_record)
    cells = export_monthly_coverage(conn, paths)
    backfill = export_backfill_queue(conn, paths)
    conn.close()
    print(json.dumps({"articles": article_count, "coverage_cells": cells, "backfill_cells": backfill}, ensure_ascii=False))


def cmd_quality(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    report = build_quality_report(conn, paths)
    export_source_year_inventory(conn, paths)
    write_manifest(conn, paths, command="quality")
    conn.close()
    print(json.dumps(report["profile"], ensure_ascii=False))


def cmd_bootstrap(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    from .import_preview import import_preview

    preview = Path(args.input).resolve() if args.input else paths.root.parent / "preview.md"
    imported = import_preview(conn, preview)
    clustered = assign_story_ids(conn)
    hydration = rank_hydration_candidates(conn, per_month=10)
    out = paths.audit / "hydration_queue.csv"
    fields = ("record_id", "story_id", "source_code", "language", "published_at", "title_original", "original_url", "evidence_grade")
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in hydration:
            writer.writerow({key: row[key] for key in fields})
    exported = export_articles(conn, paths, per_record=not args.no_per_record)
    cells = export_monthly_coverage(conn, paths)
    backfill = export_backfill_queue(conn, paths)
    report = build_quality_report(conn, paths)
    manifest = write_manifest(conn, paths, command="bootstrap")
    conn.close()
    print(json.dumps({
        "imported": imported, "clustered": clustered, "hydration_planned": len(hydration),
        "exported": exported, "coverage_cells": cells, "backfill_cells": backfill, "profile": report["profile"],
        "run_id": manifest["run_id"],
    }, ensure_ascii=False))


def cmd_crawl_pasaxon(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    kwargs = {
        "pages_per_query": args.pages,
        "epaper_pages": args.epaper_pages,
        "max_articles": args.max_articles,
        "force": args.force,
    }
    if args.query:
        kwargs["queries"] = args.query
    summary = crawl_pasaxon_probe(conn, paths, **kwargs)
    conn.close()
    print(json.dumps(asdict(summary), ensure_ascii=False))


def cmd_hydrate_kpl(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    summary = hydrate_kpl(
        conn,
        paths,
        limit=args.limit,
        date_from=date.fromisoformat(args.date_from) if args.date_from else None,
        date_to=date.fromisoformat(args.date_to) if args.date_to else None,
        per_month=args.per_month,
        force=args.force,
    )
    conn.close()
    print(json.dumps(asdict(summary), ensure_ascii=False))


def cmd_crawl_pasaxon_history(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    summary = crawl_pasaxon_history(
        conn,
        paths,
        query=args.query,
        start_page=args.start_page,
        max_pages=args.max_pages,
        date_from=date.fromisoformat(args.date_from),
        date_to=date.fromisoformat(args.date_to),
        force=args.force,
    )
    conn.close()
    print(json.dumps(asdict(summary), ensure_ascii=False))


def cmd_fetch_wayback_kpl_index(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    result = fetch_wayback_kpl_index(paths, year=args.year)
    print(json.dumps(result, ensure_ascii=False))


def cmd_crawl_wayback_kpl(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    result = crawl_wayback_kpl(
        conn,
        paths,
        index_file=Path(args.index_file).resolve(),
        scan_per_month=args.scan_per_month,
        target_per_month=args.target_per_month,
        force=args.force,
    )
    conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_fetch_wayback_pasaxon_index(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    result = fetch_wayback_pasaxon_index(paths, year=args.year)
    print(json.dumps(result, ensure_ascii=False))


def cmd_crawl_wayback_pasaxon(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    result = crawl_wayback_pasaxon(
        conn,
        paths,
        index_file=Path(args.index_file).resolve(),
        scan_homepages_per_month=args.scan_homepages_per_month,
        target_per_month=args.target_per_month,
        force=args.force,
    )
    conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_discover_commoncrawl(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    try:
        result = discover_commoncrawl(
            conn, paths, index=args.index, domain=args.domain, page_size=args.page_size,
            url_pattern=args.url_pattern, match_type=args.match_type,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_incremental_update(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    as_of = date.fromisoformat(args.as_of)
    start = as_of - timedelta(days=args.days)
    kpl = crawl_kpl_window(
        conn, paths, date_from=start, date_to=as_of, max_pages=args.kpl_pages, force=args.force
    )
    pasaxon = crawl_pasaxon_probe(
        conn,
        paths,
        pages_per_query=args.pasaxon_pages,
        epaper_pages=1,
        max_articles=args.max_pasaxon_articles,
        published_after=start,
        force=args.force,
    )
    conn.close()
    print(json.dumps({"window": {"from": start.isoformat(), "to": as_of.isoformat()}, "kpl": asdict(kpl), "pasaxon": asdict(pasaxon)}, ensure_ascii=False))


def cmd_inventory(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    rows = export_source_year_inventory(conn, paths)
    conn.close()
    print(json.dumps({"source_year_rows": len(rows), "output": str(paths.audit / 'source_inventory.md')}, ensure_ascii=False))


def cmd_status_report_artifact(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    conn = connect(paths.database)
    out = build_status_report_artifact(conn, paths)
    conn.close()
    print(json.dumps({"artifact": str(out)}, ensure_ascii=False))


def cmd_import_pasaxon_round2(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    ndjson = Path(args.input).resolve() if args.input else paths.staging / "pasaxon_round2" / "records.ndjson"
    conn = connect(paths.database)
    result = import_pasaxon_round2(conn, paths, ndjson)
    conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_import_pasaxon_commoncrawl(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    ndjson = Path(args.input).resolve() if args.input else paths.staging / "archive_ocr" / "commoncrawl_pasaxon_2012" / "records.ndjson"
    conn = connect(paths.database)
    result = import_pasaxon_commoncrawl(conn, paths, ndjson)
    conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_import_pasaxon_listings(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    ndjson = Path(args.input).resolve()
    conn = connect(paths.database)
    result = import_pasaxon_listing_discoveries(conn, paths, ndjson)
    conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_import_pasaxon_wayback_upgrades(args: argparse.Namespace) -> None:
    paths = _paths(args.root)
    ndjson = Path(args.input).resolve()
    conn = connect(paths.database)
    result = import_pasaxon_wayback_upgrades(conn, paths, ndjson)
    conn.close()
    print(json.dumps(result, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="laos-china-corpus")
    parser.add_argument("--root", help="Project root; defaults to package project directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    imp = sub.add_parser("import-preview")
    imp.add_argument("--input")
    imp.set_defaults(func=cmd_import_preview)
    sub.add_parser("cluster").set_defaults(func=cmd_cluster)
    hydration = sub.add_parser("plan-hydration")
    hydration.add_argument("--per-month", type=int, default=10)
    hydration.set_defaults(func=cmd_plan_hydration)
    sample = sub.add_parser("sample")
    sample.add_argument("--sample-id", default="main-2012-2026-v1")
    sample.add_argument("--target", type=int, default=4)
    sample.add_argument("--allow-excerpts", action="store_true")
    sample.set_defaults(func=cmd_sample)
    sub.add_parser("queue-translations").set_defaults(func=cmd_queue_translations)
    export = sub.add_parser("export")
    export.add_argument("--no-per-record", action="store_true")
    export.set_defaults(func=cmd_export)
    sub.add_parser("quality").set_defaults(func=cmd_quality)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--input")
    bootstrap.add_argument("--no-per-record", action="store_true")
    bootstrap.set_defaults(func=cmd_bootstrap)
    pasaxon = sub.add_parser("crawl-pasaxon")
    pasaxon.add_argument("--query", action="append", help="Repeat for multiple Lao/English queries")
    pasaxon.add_argument("--pages", type=int, choices=(1, 2, 3), default=2)
    pasaxon.add_argument("--epaper-pages", type=int, choices=(0, 1, 2), default=1)
    pasaxon.add_argument("--max-articles", type=int, default=30)
    pasaxon.add_argument("--force", action="store_true")
    pasaxon.set_defaults(func=cmd_crawl_pasaxon)
    kpl = sub.add_parser("hydrate-kpl")
    kpl.add_argument("--limit", type=int, default=20)
    kpl.add_argument("--date-from")
    kpl.add_argument("--date-to")
    kpl.add_argument("--per-month", type=int)
    kpl.add_argument("--force", action="store_true")
    kpl.set_defaults(func=cmd_hydrate_kpl)
    pasaxon_history = sub.add_parser("crawl-pasaxon-history")
    pasaxon_history.add_argument("--query", default="ຈີນ")
    pasaxon_history.add_argument("--start-page", type=int, default=1)
    pasaxon_history.add_argument("--max-pages", type=int, default=100)
    pasaxon_history.add_argument("--date-from", default="2012-01-01")
    pasaxon_history.add_argument("--date-to", default="2020-12-31")
    pasaxon_history.add_argument("--force", action="store_true")
    pasaxon_history.set_defaults(func=cmd_crawl_pasaxon_history)
    wayback_index = sub.add_parser("fetch-wayback-kpl-index")
    wayback_index.add_argument("--year", type=int, required=True)
    wayback_index.set_defaults(func=cmd_fetch_wayback_kpl_index)
    wayback_crawl = sub.add_parser("crawl-wayback-kpl")
    wayback_crawl.add_argument("--index-file", required=True)
    wayback_crawl.add_argument("--scan-per-month", type=int, default=30)
    wayback_crawl.add_argument("--target-per-month", type=int, default=4)
    wayback_crawl.add_argument("--force", action="store_true")
    wayback_crawl.set_defaults(func=cmd_crawl_wayback_kpl)
    pasaxon_wayback_index = sub.add_parser("fetch-wayback-pasaxon-index")
    pasaxon_wayback_index.add_argument("--year", type=int, required=True)
    pasaxon_wayback_index.set_defaults(func=cmd_fetch_wayback_pasaxon_index)
    pasaxon_wayback_crawl = sub.add_parser("crawl-wayback-pasaxon")
    pasaxon_wayback_crawl.add_argument("--index-file", required=True)
    pasaxon_wayback_crawl.add_argument("--scan-homepages-per-month", type=int, default=20)
    pasaxon_wayback_crawl.add_argument("--target-per-month", type=int, default=2)
    pasaxon_wayback_crawl.add_argument("--force", action="store_true")
    pasaxon_wayback_crawl.set_defaults(func=cmd_crawl_wayback_pasaxon)
    commoncrawl = sub.add_parser("discover-commoncrawl")
    commoncrawl.add_argument("--index", required=True, help="For example CC-MAIN-2014-52")
    commoncrawl.add_argument("--domain", required=True, help="Historical source domain")
    commoncrawl.add_argument("--page-size", type=int, default=200)
    commoncrawl.add_argument("--url-pattern", help="Path-bounded pattern, for example pasaxon.org.la/conten/*")
    commoncrawl.add_argument("--match-type", choices=("exact", "prefix", "host", "domain"), default="domain")
    commoncrawl.set_defaults(func=cmd_discover_commoncrawl)
    incremental = sub.add_parser("incremental-update")
    incremental.add_argument("--as-of", default="2026-08-07")
    incremental.add_argument("--days", type=int, default=30)
    incremental.add_argument("--kpl-pages", type=int, default=1)
    incremental.add_argument("--pasaxon-pages", type=int, choices=(1, 2, 3), default=2)
    incremental.add_argument("--max-pasaxon-articles", type=int, default=30)
    incremental.add_argument("--force", action="store_true")
    incremental.set_defaults(func=cmd_incremental_update)
    sub.add_parser("inventory").set_defaults(func=cmd_inventory)
    sub.add_parser("status-report-artifact").set_defaults(func=cmd_status_report_artifact)
    staging_import = sub.add_parser("import-pasaxon-round2")
    staging_import.add_argument("--input")
    staging_import.set_defaults(func=cmd_import_pasaxon_round2)
    commoncrawl_import = sub.add_parser("import-pasaxon-commoncrawl")
    commoncrawl_import.add_argument("--input")
    commoncrawl_import.set_defaults(func=cmd_import_pasaxon_commoncrawl)
    listing_import = sub.add_parser("import-pasaxon-listings")
    listing_import.add_argument("--input", required=True)
    listing_import.set_defaults(func=cmd_import_pasaxon_listings)
    upgrade_import = sub.add_parser("import-pasaxon-wayback-upgrades")
    upgrade_import.add_argument("--input", required=True)
    upgrade_import.set_defaults(func=cmd_import_pasaxon_wayback_upgrades)
    epaper_import = sub.add_parser("import-pasaxon-epaper")
    epaper_import.add_argument("--input")
    epaper_import.set_defaults(func=cmd_import_pasaxon_epaper)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
