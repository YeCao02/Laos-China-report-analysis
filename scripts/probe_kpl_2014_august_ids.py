from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from laos_china_corpus.adapters.kpl import (
    KPLParseError,
    build_detail_url,
    parse_detail_page,
)


LAO_TERMS = ("ສປ ຈີນ", "ສປຈີນ", "ລາວ-ຈີນ", "ຈີນ-ລາວ", "ຈີນ")
ENGLISH_TERMS = ("china", "chinese", "lao-china", "laos-china", "china-laos")
FRENCH_TERMS = ("chine", "chinois", "chinoise", "laos-chine", "lao-chine")


def china_hits(text: str, language: str) -> list[str]:
    normalized = " ".join(text.casefold().split())
    # “Indochina” is not by itself evidence that an article concerns China.
    normalized = normalized.replace("ອິນດູຈີນ", "").replace("indochina", "")
    # Old global IDs may render Lao, English, or French content at either
    # current route, so detection must not depend on the route language.
    terms = LAO_TERMS + ENGLISH_TERMS + FRENCH_TERMS
    return [term for term in terms if term in normalized]


def write_ndjson(path: Path, rows: list[dict[str, object]]) -> None:
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(payload, encoding="utf-8")


def run(root: Path, first_id: int, last_id: int, delay: float) -> dict[str, object]:
    staging = root / "data/staging/kpl_2014_august_id_probe"
    raw_dir = staging / "raw"
    body_dir = staging / "body"
    raw_dir.mkdir(parents=True, exist_ok=True)
    body_dir.mkdir(parents=True, exist_ok=True)
    screened: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    requests = failures = 0
    stopped_reason = "completed_bounded_probe"
    headers = {"User-Agent": "Laos-China-media-corpus/1.0 (research archive; 1 request/s)"}

    for article_id in range(first_id, last_id + 1):
        # KPL's old global IDs return the same record through both current
        # language routes. Probe one route to avoid duplicate traffic.
        for language in ("lo",):
            url = build_detail_url(language, article_id)
            observed_at = datetime.now(timezone.utc).isoformat()
            status = None
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = response.read()
                    status = response.status
                    response_headers = dict(response.headers.items())
                requests += 1
                raw_sha = hashlib.sha256(payload).hexdigest()
                raw_path = raw_dir / f"{language}_{article_id}_{raw_sha}.html.gz"
                raw_path.write_bytes(gzip.compress(payload))
                try:
                    article = parse_detail_page(
                        payload,
                        url,
                        retrieved_at=observed_at,
                        raw_file=str(raw_path.relative_to(root)).replace("\\", "/"),
                        response_sha256=raw_sha,
                    )
                except KPLParseError as exc:
                    screened.append({"url": url, "http_status": status, "status": "parse_failure", "reason": str(exc), "raw_file": str(raw_path.relative_to(root)).replace("\\", "/"), "raw_sha256": raw_sha})
                    failures += 1
                    time.sleep(delay)
                    continue
                combined = f"{article.title_original}\n{article.excerpt_original or ''}\n{article.body_original or ''}"
                hits = china_hits(combined, language)
                published_day = str(article.published_at or "")[:10]
                qualifies = published_day.startswith("2014-08-") and bool(hits)
                row = {
                    "url": url,
                    "http_status": status,
                    "status": "china_match" if qualifies else "not_target",
                    "published_at": article.published_at,
                    "title_original": article.title_original,
                    "matched_queries": hits,
                    "raw_file": str(raw_path.relative_to(root)).replace("\\", "/"),
                    "raw_sha256": raw_sha,
                    "response_headers": response_headers,
                }
                screened.append(row)
                if qualifies:
                    body = article.body_original or ""
                    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
                    body_path = body_dir / f"{language}_{article_id}_{body_sha}.txt"
                    body_path.write_text(body, encoding="utf-8")
                    article.raw_file = row["raw_file"]
                    article.body_file = str(body_path.relative_to(root)).replace("\\", "/")
                    article.content_sha256 = body_sha
                    article.evidence_grade = "A1"
                    article.retrieval_tier = "T1_DIRECT_CHINA"
                    article.matched_queries = hits
                    article.china_note_zh = "KPL官方详情页正文直接命中涉华关键词；具体主题待人工精编。"
                    article.metadata.update({"probe": "kpl_2014_august_id_gap", "response_headers": response_headers, "response_sha256": raw_sha})
                    record = asdict(article)
                    record["raw_sha256"] = raw_sha
                    records.append(record)
            except urllib.error.HTTPError as exc:
                requests += 1
                screened.append({"url": url, "http_status": exc.code, "status": "http_error", "reason": str(exc)})
                if exc.code in {403, 429}:
                    stopped_reason = f"circuit_breaker_http_{exc.code}"
                    write_ndjson(staging / "screened.ndjson", screened)
                    write_ndjson(staging / "records.ndjson", records)
                    return {"requests": requests, "failures": failures + 1, "records": len(records), "stopped_reason": stopped_reason}
            except Exception as exc:
                requests += 1
                failures += 1
                screened.append({"url": url, "status": "request_failure", "reason": repr(exc)})
            time.sleep(delay)

    write_ndjson(staging / "screened.ndjson", screened)
    write_ndjson(staging / "records.ndjson", records)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {"article_ids": [first_id, last_id], "routes": ["lo"], "old_ids_are_global": True, "target_month": "2014-08"},
        "requests": requests,
        "screened": len(screened),
        "failures": failures,
        "records": len(records),
        "stopped_reason": stopped_reason,
        "network_stopped": True,
        "canonical_database_modified": False,
    }
    (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--first-id", type=int, default=96)
    parser.add_argument("--last-id", type=int, default=120)
    parser.add_argument("--delay", type=float, default=1.05)
    args = parser.parse_args()
    print(json.dumps(run(args.root.resolve(), args.first_id, args.last_id, args.delay), ensure_ascii=False))


if __name__ == "__main__":
    main()
