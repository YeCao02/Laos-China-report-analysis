from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .wayback import (
    WaybackCapture,
    build_pasaxon_path_cdx_query,
    is_lao_china_title,
    load_cdx,
    old_pasaxon_url_parts,
    parse_cdx,
    parse_direct_pasaxon_article,
    save_cdx,
)


USER_AGENT = "laos-china-media-corpus/0.2 (+academic archive research)"


class ArchiveBlocked(RuntimeError):
    pass


class PoliteFetcher:
    def __init__(self, *, interval: float = 1.0, timeout: float = 90.0) -> None:
        self.interval = max(1.0, interval)
        self.timeout = timeout
        self._last_request = 0.0

    def fetch(self, url: str) -> bytes:
        delay = self.interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            if exc.code in {403, 429}:
                raise ArchiveBlocked(f"Wayback access stopped after HTTP {exc.code}: {url}") from exc
            raise
        finally:
            self._last_request = time.monotonic()
        return payload


def _append_json(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_existing(database: Path) -> tuple[dict[str, int], set[str]]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        counts = {
            str(month): int(count)
            for month, count in conn.execute(
                "SELECT substr(published_at,1,7),count(*) FROM articles "
                "WHERE source_code='pasaxon_archive' AND published_at BETWEEN "
                "'2012-01-01' AND '2020-12-31' GROUP BY 1"
            )
        }
        urls = {
            str(row[0])
            for row in conn.execute(
                "SELECT original_url FROM articles WHERE source_code='pasaxon_archive' "
                "AND original_url IS NOT NULL"
            )
        }
        return counts, urls
    finally:
        conn.close()


def _priority_groups(captures: list[WaybackCapture]) -> dict[str, list[WaybackCapture]]:
    grouped: dict[str, dict[str, list[tuple[int, WaybackCapture]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for capture in captures:
        parts = old_pasaxon_url_parts(capture.original_url)
        if not parts:
            continue
        published, slot = parts
        grouped[published[:7]][published].append((slot, capture))
    result: dict[str, list[WaybackCapture]] = {}
    for month, days in grouped.items():
        for values in days.values():
            values.sort(key=lambda pair: (pair[0], pair[1].original_url))
        ordered: list[WaybackCapture] = []
        depth = 0
        while True:
            added = False
            for day in sorted(days):
                if depth < len(days[day]):
                    ordered.append(days[day][depth][1])
                    added = True
            if not added:
                break
            depth += 1
        result[month] = ordered
    return result


def _china_hits(title: str, body: str) -> list[str]:
    text = f"{title}\n{body}"
    # Indochina is a Lao compound containing the glyph sequence for China,
    # but it does not denote China itself.
    text = re.sub(r"ອິນ[\s\u200b-]*(?:ດູ|ໂດ)[\s\u200b-]*ຈີນ", " ", text)
    compact = re.sub(r"[\s\u200b\-\u2013\u2014]+", "", text)
    hits: list[str] = []
    for token, label in (
        ("\u0eaa\u0e9b\u0e88\u0eb5\u0e99", "\u0eaa\u0e9b \u0e88\u0eb5\u0e99"),
        ("\u0ea5\u0eb2\u0ea7\u0e88\u0eb5\u0e99", "\u0ea5\u0eb2\u0ea7-\u0e88\u0eb5\u0e99"),
        ("\u0e88\u0eb5\u0e99\u0ea5\u0eb2\u0ea7", "\u0e88\u0eb5\u0e99-\u0ea5\u0eb2\u0ea7"),
    ):
        if token in compact:
            hits.append(label)
    china_mentions = list(re.finditer("\u0e88\u0eb5\u0e99", text))
    institutional_context = re.search(
        r"(?:\u0e9b\u0eb0\u0ec0\u0e97\u0e94|\u0ea5\u0eb1\u0e94\u0e96\u0eb0\u0e9a\u0eb2\u0e99|\u0e97\u0eb9\u0e94|\u0e99\u0eb1\u0e81\u0e97\u0eb8\u0ea5\u0eb0\u0e81\u0eb4\u0e94|\u0e9a\u0ecd\u0ea5\u0eb4\u0eaa\u0eb1\u0e94|\u0e9e\u0eb1\u0e81|\u0eaa\u0eb0\u0e9e\u0eb2|\u0ea5\u0eb2\u0ea7).{0,35}\u0e88\u0eb5\u0e99"
        r"|\u0e88\u0eb5\u0e99.{0,35}(?:\u0e9b\u0eb0\u0ec0\u0e97\u0e94|\u0ea5\u0eb1\u0e94\u0e96\u0eb0\u0e9a\u0eb2\u0e99|\u0e97\u0eb9\u0e94|\u0e9a\u0ecd\u0ea5\u0eb4\u0eaa\u0eb1\u0e94|\u0e9e\u0eb1\u0e81|\u0eaa\u0eb0\u0e9e\u0eb2|\u0ea5\u0eb2\u0ea7)",
        text,
    )
    if (len(china_mentions) >= 2 or institutional_context) and "\u0e88\u0eb5\u0e99" not in hits:
        hits.append("\u0e88\u0eb5\u0e99")
    folded = text.casefold()
    for term in ("china", "chinese", "lao-china", "china-laos", "yunnan", "beijing"):
        if term in folded and term not in hits:
            hits.append(term)
    return hits


def collect(
    *,
    root: Path,
    years: list[int],
    target_per_month: int = 2,
    scan_per_month: int = 40,
    refresh_indexes: bool = False,
    index_files: list[Path] | None = None,
    months: set[str] | None = None,
) -> dict[str, object]:
    staging = root / "data" / "staging" / "pasaxon_round2"
    index_dir = staging / "cdx"
    raw_dir = staging / "raw"
    records_path = staging / "records.ndjson"
    failures_path = staging / "failures.ndjson"
    database = root / "data" / "corpus.sqlite3"
    staging.mkdir(parents=True, exist_ok=True)
    records_path.touch(exist_ok=True)
    existing_counts, existing_urls = _read_existing(database)
    already_staged: set[str] = set()
    staged_counts: dict[str, int] = defaultdict(int)
    if records_path.exists():
        for line in records_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            already_staged.add(str(row["original_url"]))
            staged_counts[str(row["published_at"])[:7]] += 1

    fetcher = PoliteFetcher()
    all_captures: list[WaybackCapture] = []
    index_counts: dict[str, int] = {}
    stopped_reason: str | None = None
    prefixes = ("conten", "articles", "index", "hotnews", "worldnews", "cooperation", "pasaxon-detail.php")
    discovery_jobs: list[tuple[str, Path]] = []
    if index_files:
        discovery_jobs = [(path.stem, path) for path in index_files]
    else:
        discovery_jobs = [
            (prefix, index_dir / f"pasaxon_{prefix.replace('.', '_')}_2012_2020.json")
            for prefix in prefixes
        ]
    for prefix, index_path in discovery_jobs:
        try:
            if index_files:
                captures = load_cdx(index_path)
            elif refresh_indexes or not index_path.exists():
                payload = fetcher.fetch(
                    build_pasaxon_path_cdx_query(
                        prefix, year_from=min(years), year_to=max(years)
                    )
                )
                captures = parse_cdx(payload)
                save_cdx(captures, index_path)
            else:
                captures = load_cdx(index_path)
            index_counts[prefix] = len(captures)
            all_captures.extend(captures)
        except ArchiveBlocked as exc:
            stopped_reason = str(exc)
            break
        except Exception as exc:  # preserve a complete audit trail and continue to next year
            _append_json(
                failures_path,
                {"stage": "cdx", "path_prefix": prefix, "error": f"{type(exc).__name__}: {exc}"},
            )

    groups = _priority_groups(all_captures)
    scanned: dict[str, int] = defaultdict(int)
    found: dict[str, int] = defaultdict(int)
    for month, captures in sorted(groups.items()):
        if stopped_reason:
            break
        if not ("2012-01" <= month <= "2020-12"):
            continue
        if months and month not in months:
            continue
        need = target_per_month - existing_counts.get(month, 0) - staged_counts.get(month, 0)
        if need <= 0:
            continue
        for capture in captures:
            if found[month] >= need or scanned[month] >= scan_per_month:
                break
            if capture.original_url in existing_urls or capture.original_url in already_staged:
                continue
            scanned[month] += 1
            try:
                payload = fetcher.fetch(capture.replay_url)
                article = parse_direct_pasaxon_article(payload, capture.original_url)
                hits = _china_hits(article.title, article.body)
                if not hits:
                    continue
                digest = hashlib.sha256(payload).hexdigest()
                body_digest = hashlib.sha256(article.body.encode("utf-8")).hexdigest()
                destination = raw_dir / month / f"{digest}.html.gz"
                destination.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(destination, "wb") as handle:
                    handle.write(payload)
                row = {
                    "source_code": "pasaxon_archive",
                    "language": "lo",
                    "title_original": article.title,
                    "published_at": article.published_date,
                    "date_precision": "url_day",
                    "body_original": article.body,
                    "body_method": "wayback_direct_pasaxon_html",
                    "matched_queries": hits,
                    "original_url": capture.original_url,
                    "archive_url": capture.replay_url,
                    "raw_file": destination.relative_to(root).as_posix(),
                    "evidence_grade": "B1",
                    "retrieval_tier": "T1_DIRECT_CHINA",
                    "raw_sha256": digest,
                    "content_sha256": body_digest,
                    "archive_capture_timestamp": capture.timestamp,
                    "archive_digest": capture.digest,
                    "old_pasaxon_slot": article.slot,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "parser": "old_pasaxon_direct_wayback_v2",
                }
                _append_json(records_path, row)
                already_staged.add(capture.original_url)
                found[month] += 1
                staged_counts[month] += 1
            except ArchiveBlocked as exc:
                stopped_reason = str(exc)
                break
            except Exception as exc:
                _append_json(
                    failures_path,
                    {
                        "stage": "article",
                        "month": month,
                        "original_url": capture.original_url,
                        "archive_url": capture.replay_url,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )

    summary: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database_mode": "read_only",
        "years": years,
        "index_counts": index_counts,
        "dated_capture_months": {month: len(values) for month, values in sorted(groups.items())},
        "scanned_by_month": dict(sorted(scanned.items())),
        "new_records_by_month": dict(sorted(found.items())),
        "new_records": sum(found.values()),
        "all_staged_records": sum(staged_counts.values()),
        "stopped_reason": stopped_reason,
    }
    (staging / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Pasaxon Wayback backfill round 2")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2012, 2021)))
    parser.add_argument("--target-per-month", type=int, default=2)
    parser.add_argument("--scan-per-month", type=int, default=40)
    parser.add_argument("--refresh-indexes", action="store_true")
    parser.add_argument(
        "--index-file", type=Path, action="append",
        help="Use only an existing CDX file; repeatable and performs no CDX discovery",
    )
    parser.add_argument("--month", action="append", help="Restrict replay to YYYY-MM")
    args = parser.parse_args()
    print(
        json.dumps(
            collect(
                root=args.root,
                years=args.years,
                target_per_month=args.target_per_month,
                scan_per_month=args.scan_per_month,
                refresh_indexes=args.refresh_indexes,
                index_files=args.index_file,
                months=set(args.month) if args.month else None,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
