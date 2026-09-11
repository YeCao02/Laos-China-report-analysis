from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from laos_china_corpus.adapters.pasaxon import (  # noqa: E402
    PasaxonParseError,
    build_epaper_url,
    build_search_url,
    build_tag_url,
    parse_article,
    parse_epaper_results,
    parse_search_results,
)


BASE = "https://pasaxon.org.la"
ALLOWED_HOST = "pasaxon.org.la"
DATE_FROM = "2021-01-01"
DATE_TO = "2025-12-31"
DIRECT_QUERIES = ("ຈີນ", "China")
DIRECT_TERMS = (
    "ຈີນ", "ສປ ຈີນ", "ສປຈີນ", "ລາວ-ຈີນ", "ຈີນ-ລາວ",
    "china", "chinese", "lao-china", "laos-china", "china-laos",
)
CHALLENGE_MARKERS = (
    "captcha", "cloudflare", "verify you are human", "challenge-platform",
    "cf-chl-", "ຢືນຢັນວ່າທ່ານເປັນມະນຸດ",
)
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.I)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_ndjson(path: Path, rows: list[dict[str, object]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def write_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        absolute = urljoin(req.full_url, newurl)
        if urlparse(absolute).hostname != ALLOWED_HOST:
            raise RuntimeError(f"cross-domain redirect refused: {absolute}")
        return super().redirect_request(req, fp, code, msg, headers, absolute)


class StopNetwork(RuntimeError):
    pass


class BoundedFetcher:
    def __init__(self, out: Path, cap: int, delay: float, *, resume: bool = False) -> None:
        self.out = out
        self.raw = out / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.cap = cap
        self.delay = max(1.0, delay)
        ledger_path = out / "request_ledger.ndjson"
        failure_path = out / "failures.ndjson"
        manifest_path = out / "download_manifest.ndjson"
        self.ledger = [json.loads(line) for line in ledger_path.read_text("utf-8").splitlines()] if resume and ledger_path.exists() else []
        self.failures = [json.loads(line) for line in failure_path.read_text("utf-8").splitlines()] if resume and failure_path.exists() else []
        self.manifest = [json.loads(line) for line in manifest_path.read_text("utf-8").splitlines()] if resume and manifest_path.exists() else []
        self.count = max((int(row["request_number"]) for row in self.ledger), default=0)
        self.last_request = 0.0
        self.stopped = False
        self.stop_reason: str | None = None
        self.opener = build_opener(SameHostRedirect())

    def _flush(self) -> None:
        write_ndjson(self.out / "request_ledger.ndjson", self.ledger)
        write_ndjson(self.out / "failures.ndjson", self.failures)
        write_ndjson(self.out / "download_manifest.ndjson", self.manifest)

    def fetch(self, url: str, purpose: str) -> tuple[bytes, str, str]:
        if self.stopped:
            raise StopNetwork(self.stop_reason or "network already stopped")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
            raise ValueError(f"non-Pasaxon-current URL refused: {url}")
        if self.count >= self.cap:
            self.stopped = True
            self.stop_reason = f"request cap reached ({self.cap})"
            self._flush()
            raise StopNetwork(self.stop_reason)
        wait = self.delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        started = utcnow()
        self.count += 1
        self.last_request = time.monotonic()
        entry: dict[str, object] = {
            "request_number": self.count,
            "requested_at": started,
            "url": url,
            "purpose": purpose,
            "policy_host": ALLOWED_HOST,
        }
        request = Request(
            url,
            headers={
                "User-Agent": "SCNU-academic-corpus/1.0 (bounded Pasaxon audit)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            },
        )
        try:
            with self.opener.open(request, timeout=45) as response:
                payload = response.read()
                final_url = response.geturl()
                status = int(response.status)
                content_type = response.headers.get("Content-Type", "")
                headers = dict(response.headers.items())
            if urlparse(final_url).hostname != ALLOWED_HOST:
                raise RuntimeError(f"final URL left official host: {final_url}")
            lower = payload[:100_000].decode("utf-8", errors="ignore").casefold()
            if status in {403, 429} or any(marker in lower for marker in CHALLENGE_MARKERS):
                self.stopped = True
                self.stop_reason = f"access-control response at request {self.count}: HTTP {status}"
                raise StopNetwork(self.stop_reason)
            digest = sha256(payload)
            raw_name = f"{self.count:03d}_{hashlib.sha256(url.encode()).hexdigest()[:16]}.html.gz"
            raw_path = self.raw / raw_name
            raw_path.write_bytes(gzip.compress(payload, compresslevel=6))
            entry.update({
                "status": status, "final_url": final_url, "content_type": content_type,
                "bytes": len(payload), "raw_sha256": digest,
                "raw_file": raw_path.relative_to(ROOT).as_posix(), "outcome": "saved",
            })
            self.manifest.append({
                "url": final_url, "purpose": purpose,
                "local_file": raw_path.relative_to(ROOT).as_posix(),
                "sha256": digest, "bytes_uncompressed": len(payload),
                "content_type": content_type, "fetched_at": started,
                "response_headers": headers,
            })
            self.ledger.append(entry)
            self._flush()
            return payload, final_url, raw_path.relative_to(ROOT).as_posix()
        except HTTPError as exc:
            entry.update({"status": exc.code, "outcome": "http_error", "error": str(exc)})
            self.ledger.append(entry)
            self.failures.append({"url": url, "purpose": purpose, "at": utcnow(), "error": f"HTTP {exc.code}: {exc.reason}"})
            if exc.code in {403, 429}:
                self.stopped = True
                self.stop_reason = f"HTTP {exc.code} at request {self.count}"
            self._flush()
            if self.stopped:
                raise StopNetwork(self.stop_reason) from exc
            raise
        except StopNetwork as exc:
            entry.update({"status": entry.get("status"), "outcome": "stopped", "error": str(exc)})
            if not self.ledger or self.ledger[-1].get("request_number") != self.count:
                self.ledger.append(entry)
            self.failures.append({"url": url, "purpose": purpose, "at": utcnow(), "error": str(exc)})
            self._flush()
            raise
        except (URLError, TimeoutError, RuntimeError, OSError) as exc:
            entry.update({"outcome": "network_error", "error": f"{type(exc).__name__}: {exc}"})
            self.ledger.append(entry)
            self.failures.append({"url": url, "purpose": purpose, "at": utcnow(), "error": f"{type(exc).__name__}: {exc}"})
            self._flush()
            raise


def decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def in_window(value: str | None) -> bool:
    return bool(value and DATE_FROM <= value[:10] <= DATE_TO)


def direct_hits(text: str) -> list[str]:
    folded = text.casefold()
    return [term for term in DIRECT_TERMS if term.casefold() in folded]


def candidate_key(item: dict[str, object]) -> tuple[str, str]:
    date = str(item.get("published_at") or "9999-99-99")[:10]
    return date, str(item["url"])


def add_discoveries(target: dict[str, dict[str, object]], listing, query: str, raw_file: str) -> None:  # noqa: ANN001
    for discovery in listing.discoveries:
        if discovery.kind != "article" or not in_window(discovery.published_at):
            continue
        row = target.setdefault(discovery.url, {
            "url": discovery.url,
            "source_article_id": discovery.source_article_id,
            "title_original": discovery.title_original,
            "published_at": discovery.published_at,
            "queries": [],
            "listing_urls": [],
            "listing_raw_files": [],
        })
        row["queries"] = sorted(set([*row["queries"], query]))
        row["listing_urls"] = sorted(set([*row["listing_urls"], discovery.search_url]))
        row["listing_raw_files"] = sorted(set([*row["listing_raw_files"], raw_file]))


def select_balanced(candidates: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    by_month: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in sorted(candidates.values(), key=candidate_key, reverse=True):
        month = str(row.get("published_at") or "")[:7]
        if len(month) == 7:
            by_month[month].append(row)
    selected: list[dict[str, object]] = []
    # Breadth first: one candidate per month, then the second per month.
    for rank in (0, 1):
        for month in sorted(by_month, reverse=True):
            if len(by_month[month]) > rank:
                selected.append(by_month[month][rank])
    return selected


def save_article(out: Path, row: dict[str, object], record, raw_sha: str, raw_file: str) -> dict[str, object]:  # noqa: ANN001
    data = asdict(record)
    text = record.body_original or ""
    body_sha = sha256(text.encode("utf-8"))
    record_name = f"{record.record_id}.json"
    text_name = f"{record.record_id}.md"
    records_dir = out / "articles"
    records_dir.mkdir(parents=True, exist_ok=True)
    text_path = records_dir / text_name
    text_path.write_text(
        f"# {record.title_original}\n\n- Published: {record.published_at}\n- URL: {record.original_url}\n\n{text}\n",
        encoding="utf-8",
    )
    data.update({
        "raw_file": raw_file,
        "body_file": text_path.relative_to(ROOT).as_posix(),
        "raw_sha256": raw_sha,
        "body_sha256": body_sha,
        "listing_published_at": row.get("published_at"),
        "listing_raw_files": row.get("listing_raw_files"),
        "china_direct_hits": direct_hits(record.title_original + "\n" + text),
        "round": "pasaxon_current_round6",
    })
    (records_dir / record_name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return data


def run(out: Path, cap: int, delay: float, listing_budget: int) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    fetcher = BoundedFetcher(out, cap, delay)
    candidates: dict[str, dict[str, object]] = {}
    records: list[dict[str, object]] = []
    entry_results: list[dict[str, object]] = []
    pages_by_query = Counter()

    # Read-only entry identification. These are deliberately saved and audited.
    probes = [
        (f"{BASE}/robots.txt", "entry:robots"),
        (f"{BASE}/sitemap.xml", "entry:sitemap"),
        (build_search_url(DIRECT_QUERIES[0]), "entry:search:ຈີນ"),
        (build_tag_url(DIRECT_QUERIES[0], 1), "entry:tag:ຈີນ:1"),
        (build_search_url(DIRECT_QUERIES[1]), "entry:search:China"),
        (build_tag_url(DIRECT_QUERIES[1], 1), "entry:tag:China:1"),
        (build_epaper_url(1), "entry:epaper:1"),
    ]
    prefetched_tags: set[tuple[str, int]] = set()
    try:
        for url, purpose in probes:
            try:
                payload, final_url, raw_file = fetcher.fetch(url, purpose)
            except (HTTPError, URLError, TimeoutError, RuntimeError, OSError):
                if fetcher.stopped:
                    raise StopNetwork(fetcher.stop_reason or "stopped")
                entry_results.append({"url": url, "purpose": purpose, "usable": False})
                continue
            html = decode(payload)
            usable = True
            parsed_rows = None
            try:
                if "/search/" in url:
                    query = "ຈີນ" if "ຈີນ" in purpose else "China"
                    listing = parse_search_results(html, final_url, query)
                    add_discoveries(candidates, listing, query, raw_file)
                    parsed_rows = len(listing.discoveries)
                elif "/tags/" in url:
                    query = "ຈີນ" if "ຈີນ" in purpose else "China"
                    listing = parse_search_results(html, final_url, query)
                    add_discoveries(candidates, listing, query, raw_file)
                    parsed_rows = len(listing.discoveries)
                    prefetched_tags.add((query, 1))
                    pages_by_query[query] += 1
                elif "/epaper" in url:
                    parsed_rows = len(parse_epaper_results(html, final_url).discoveries)
                elif "sitemap" in url:
                    parsed_rows = len(SITEMAP_LOC_RE.findall(html))
            except Exception as exc:  # parser failure remains visible, never becomes a record
                usable = False
                fetcher.failures.append({"url": url, "purpose": purpose, "at": utcnow(), "error": f"parse: {type(exc).__name__}: {exc}"})
            entry_results.append({"url": url, "purpose": purpose, "usable": usable, "parsed_rows": parsed_rows})

        # Traverse both direct-tag streams. Listing pages consume a fixed sub-budget.
        page = 1
        empty_streak = Counter()
        while fetcher.count < min(cap, listing_budget) and any(empty_streak[q] < 2 for q in DIRECT_QUERIES):
            advanced = False
            for query in DIRECT_QUERIES:
                if fetcher.count >= min(cap, listing_budget) or empty_streak[query] >= 2:
                    continue
                if (query, page) in prefetched_tags:
                    continue
                url = build_tag_url(query, page)
                try:
                    payload, final_url, raw_file = fetcher.fetch(url, f"discovery:tag:{query}:{page}")
                    listing = parse_search_results(decode(payload), final_url, query)
                    add_discoveries(candidates, listing, query, raw_file)
                    pages_by_query[query] += 1
                    empty_streak[query] = 0 if listing.discoveries else empty_streak[query] + 1
                    advanced = True
                except StopNetwork:
                    raise
                except Exception as exc:
                    fetcher.failures.append({"url": url, "purpose": "discovery", "at": utcnow(), "error": f"parse/fetch: {type(exc).__name__}: {exc}"})
                    empty_streak[query] += 1
            page += 1
            if not advanced and all(empty_streak[q] >= 2 for q in DIRECT_QUERIES):
                break

        write_ndjson(out / "candidates.ndjson", sorted(candidates.values(), key=candidate_key))

        month_counts = Counter()
        for row in select_balanced(candidates):
            if fetcher.count >= cap:
                break
            month = str(row["published_at"])[:7]
            if month_counts[month] >= 2:
                continue
            url = str(row["url"])
            try:
                payload, final_url, raw_file = fetcher.fetch(url, f"article:{month}")
                raw_hash = sha256(payload)
                record = parse_article(
                    decode(payload), final_url,
                    matched_queries=row["queries"],
                    search_url=(row["listing_urls"] or [None])[0],
                    retrieved_at=utcnow(),
                )
                actual_date = (record.published_at or "")[:10]
                if not in_window(record.published_at):
                    raise ValueError(f"article date outside round window: {record.published_at}")
                actual_month = actual_date[:7]
                if month_counts[actual_month] >= 2:
                    continue
                hits = direct_hits(record.title_original + "\n" + (record.body_original or ""))
                if not hits:
                    raise ValueError("no direct China term in full article")
                if not record.body_original or len(record.body_original.strip()) < 100:
                    raise ValueError("body too short for full-text inclusion")
                data = save_article(out, row, record, raw_hash, raw_file)
                records.append(data)
                month_counts[actual_month] += 1
                write_ndjson(out / "records.ndjson", records)
            except StopNetwork:
                raise
            except (PasaxonParseError, ValueError, HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
                fetcher.failures.append({"url": url, "purpose": "article_validation", "at": utcnow(), "error": f"{type(exc).__name__}: {exc}"})
                fetcher._flush()
    except StopNetwork:
        pass
    finally:
        write_ndjson(out / "candidates.ndjson", sorted(candidates.values(), key=candidate_key))
        write_ndjson(out / "records.ndjson", records)
        fetcher._flush()

    coverage = {f"{year}-{month:02d}": month_counts[f"{year}-{month:02d}"] for year in range(2021, 2026) for month in range(1, 13)}
    audit = {
        "round": "pasaxon_current_round6",
        "completed_at": utcnow(),
        "network_stopped": True,
        "network_stop_reason": fetcher.stop_reason or "bounded run completed; no further requests scheduled",
        "request_cap": cap,
        "request_count": fetcher.count,
        "minimum_delay_seconds": fetcher.delay,
        "allowed_host": ALLOWED_HOST,
        "forbidden_sources_used": [],
        "date_window": {"from": DATE_FROM, "to": DATE_TO},
        "entry_results": entry_results,
        "tag_pages_by_query": dict(pages_by_query),
        "candidate_count": len(candidates),
        "valid_fulltext_records": len(records),
        "records_by_month": coverage,
        "months_with_records": sum(value > 0 for value in coverage.values()),
        "months_at_two": sum(value == 2 for value in coverage.values()),
        "failures": len(fetcher.failures),
        "inclusion_rule": "true article date in 2021-01..2025-12; non-empty DOM body >=100 chars; direct China term in title/body; max 2 per month",
        "sqlite_written": False,
    }
    write_json(out / "audit.json", audit)
    lines = [
        "# Pasaxon current-site Round6 audit", "",
        f"- Completed: {audit['completed_at']}",
        f"- Requests: {fetcher.count}/{cap}; minimum delay {fetcher.delay:.2f}s; single threaded.",
        f"- Scope: only `{ALLOWED_HOST}`; no Wayback, Common Crawl, KPL, or SQLite.",
        f"- Candidates dated 2021–2025: {len(candidates)}.",
        f"- Valid full-text records: {len(records)} across {audit['months_with_records']} months.",
        f"- Stop reason: {audit['network_stop_reason']}.", "",
        "Search/listing snippets are discovery evidence only and never appear in `records.ndjson`. "
        "Every included record passed article-page date, body-length, direct-term, host, and dual-hash checks.",
    ]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audit


def strategic_resume(out: Path, cap: int, delay: float) -> dict[str, object]:
    """Use sparse historical tag-page probes after ordinary pagination proved too shallow."""
    fetcher = BoundedFetcher(out, cap, delay, resume=True)
    records_path = out / "records.ndjson"
    records = [json.loads(line) for line in records_path.read_text("utf-8").splitlines()] if records_path.exists() else []
    existing_urls = {str(row.get("original_url")) for row in records}
    month_counts = Counter(str(row.get("published_at") or "")[:7] for row in records)
    candidates: list[dict[str, object]] = []
    # Page 30 was still 2025. Sparse ten-page jumps are an auditable, bounded way to
    # span older IDs without walking every intervening listing.
    try:
        for page in range(40, 221, 10):
            if fetcher.count >= cap:
                break
            url = build_tag_url("ຈີນ", page)
            try:
                payload, final_url, raw_file = fetcher.fetch(url, f"strategic:tag:ຈີນ:{page}")
                listing = parse_search_results(decode(payload), final_url, "ຈີນ")
                for discovery in listing.discoveries[:2]:
                    candidates.append({
                        "url": discovery.url, "title_original": discovery.title_original,
                        "source_article_id": discovery.source_article_id,
                        "queries": ["ຈີນ"], "listing_urls": [final_url],
                        "listing_raw_files": [raw_file], "strategic_page": page,
                        "published_at": discovery.published_at,
                    })
            except StopNetwork:
                raise
            except Exception as exc:
                fetcher.failures.append({"url": url, "purpose": "strategic_listing", "at": utcnow(), "error": f"{type(exc).__name__}: {exc}"})

        for row in candidates:
            if fetcher.count >= cap:
                break
            url = str(row["url"])
            if url in existing_urls:
                continue
            try:
                payload, final_url, raw_file = fetcher.fetch(url, f"strategic:article:page{row['strategic_page']}")
                record = parse_article(
                    decode(payload), final_url, matched_queries=["ຈີນ"],
                    search_url=str(row["listing_urls"][0]), retrieved_at=utcnow(),
                )
                if not in_window(record.published_at):
                    raise ValueError(f"article date outside round window: {record.published_at}")
                month = str(record.published_at)[:7]
                if month_counts[month] >= 2:
                    continue
                text = record.title_original + "\n" + (record.body_original or "")
                if not direct_hits(text):
                    raise ValueError("no direct China term in full article")
                if not record.body_original or len(record.body_original.strip()) < 100:
                    raise ValueError("body too short for full-text inclusion")
                data = save_article(out, row, record, sha256(payload), raw_file)
                records.append(data)
                existing_urls.add(final_url)
                month_counts[month] += 1
                write_ndjson(records_path, records)
            except StopNetwork:
                raise
            except Exception as exc:
                fetcher.failures.append({"url": url, "purpose": "strategic_article_validation", "at": utcnow(), "error": f"{type(exc).__name__}: {exc}"})
                fetcher._flush()
    except StopNetwork:
        pass
    finally:
        fetcher._flush()

    coverage = {f"{year}-{month:02d}": month_counts[f"{year}-{month:02d}"] for year in range(2021, 2026) for month in range(1, 13)}
    audit_path = out / "audit.json"
    audit = json.loads(audit_path.read_text("utf-8")) if audit_path.exists() else {}
    audit.update({
        "completed_at": utcnow(), "network_stopped": True,
        "network_stop_reason": fetcher.stop_reason or "hard request budget exhausted or strategic queue completed",
        "request_count": fetcher.count, "valid_fulltext_records": len(records),
        "records_by_month": coverage,
        "months_with_records": sum(value > 0 for value in coverage.values()),
        "months_at_two": sum(value == 2 for value in coverage.values()),
        "failures": len(fetcher.failures),
        "strategic_listing_pages": list(range(40, 221, 10)),
        "strategic_candidate_rows": len(candidates),
        "candidate_count": len({str(row["url"]) for row in candidates}),
    })
    write_json(audit_path, audit)
    (out / "README.md").write_text(
        "# Pasaxon current-site Round6 audit\n\n"
        f"- Completed: {audit['completed_at']}\n"
        f"- Requests: {fetcher.count}/{cap}; minimum delay {fetcher.delay:.2f}s; single threaded.\n"
        f"- Scope: only `{ALLOWED_HOST}`; no Wayback, Common Crawl, KPL, or SQLite.\n"
        f"- Valid full-text records: {len(records)} across {audit['months_with_records']} months.\n"
        f"- Stop reason: {audit['network_stop_reason']}.\n\n"
        "The current tag listings omit reliable publication dates. Pages 1–30 were first traversed, "
        "then pages 40–220 were sampled at fixed ten-page intervals to span older article IDs. "
        "Listing rows remained discovery evidence only. Each included item was fetched from its article "
        "URL and passed article-date, full-body, direct-China-term, source-host, two-hash, and per-month-cap checks.\n",
        encoding="utf-8",
    )
    return audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "staging" / "pasaxon_current_round6")
    parser.add_argument("--request-cap", type=int, default=120)
    parser.add_argument("--delay", type=float, default=1.05)
    parser.add_argument("--listing-budget", type=int, default=65)
    parser.add_argument("--strategic-resume", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.request_cap <= 120:
        parser.error("--request-cap must be 1..120")
    if not 7 <= args.listing_budget < args.request_cap:
        parser.error("--listing-budget must leave room for article requests")
    audit = strategic_resume(args.out.resolve(), args.request_cap, args.delay) if args.strategic_resume else run(args.out.resolve(), args.request_cap, args.delay, args.listing_budget)
    # ASCII console output is compatible with legacy Windows code pages; files remain UTF-8.
    print(json.dumps(audit, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
