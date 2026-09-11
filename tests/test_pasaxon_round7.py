from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from laos_china_corpus.archives.pasaxon_round7 import collect


class Retryable503Fetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        raise HTTPError(url, 503, "Service Unavailable", {}, None)


class Round7Tests(unittest.TestCase):
    def test_three_consecutive_retryable_http_errors_open_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/catalog").mkdir(parents=True)
            with (root / "data/catalog/monthly_coverage.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["source", "year_month", "sample_story_count"])
                writer.writeheader()
                writer.writerow({"source": "PASAXON", "year_month": "2014-01", "sample_story_count": 0})
            conn = sqlite3.connect(root / "data/corpus.sqlite3")
            conn.execute("CREATE TABLE articles(source_code TEXT, published_at TEXT, original_url TEXT, content_sha256 TEXT)")
            conn.commit()
            conn.close()
            rows = []
            for day in range(1, 6):
                original = f"http://pasaxon.org.la/Economic/{day}-1-14/Content1.html"
                rows.append({
                    "url_key": original.lower(), "target_month": "2014-01", "source_path": "Economic",
                    "original_url": original,
                    "archive_url": f"https://web.archive.org/web/201401{day:02d}000000id_/{original}",
                    "archive_capture_timestamp": f"201401{day:02d}000000", "exact_url_date": f"2014-01-{day:02d}",
                })
            queue = root / "queue.ndjson"
            queue.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            fetcher = Retryable503Fetcher()
            result = collect(root=root, max_replays=10, interval=1.0, fetcher=fetcher, queue_file=queue)
            self.assertEqual(len(fetcher.calls), 3)
            self.assertEqual(result["article_replays_this_run"], 3)
            self.assertEqual(result["status_counts_this_run"]["retryable_http_error"], 3)
            self.assertEqual(result["stopped_reason"], "retryable_http_circuit_breaker:503:3")
            self.assertEqual(result["pending_queue_records"], 5)


if __name__ == "__main__":
    unittest.main()
