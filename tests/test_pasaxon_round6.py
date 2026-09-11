from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.archives.pasaxon_round6 import (
    collect, eligible_queue, reconcile_current_database, verify,
)


class Fetcher:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads; self.calls: list[str] = []
    def fetch(self, url: str) -> bytes:
        self.calls.append(url); return self.payloads[url]


class Round6Tests(unittest.TestCase):
    def test_excludes_php_and_resumes_exact_date(self) -> None:
        rows = [
            {"url_key": "php:1", "target_month": "2018-04", "source_path": "pasaxon-detail.php",
             "original_url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=1", "archive_url": "https://web.archive.org/web/1id_/http://pasaxon.org.la/x", "archive_capture_timestamp": "1", "exact_url_date": None},
            {"url_key": "/hotnews/2018/04/10/h1.html", "target_month": "2018-04", "source_path": "hotnews",
             "original_url": "http://pasaxon.org.la/hotnews/2018/04/10/h1.html", "archive_url": "https://web.archive.org/web/20180411id_/http://pasaxon.org.la/hotnews/2018/04/10/h1.html", "archive_capture_timestamp": "20180411", "exact_url_date": "2018-04-10"},
        ]
        self.assertEqual(len(eligible_queue(rows)), 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "data/catalog").mkdir(parents=True)
            with (root / "data/catalog/monthly_coverage.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["source", "year_month", "sample_story_count"]); writer.writeheader()
                writer.writerow({"source": "PASAXON", "year_month": "2018-04", "sample_story_count": 0})
            conn = sqlite3.connect(root / "data/corpus.sqlite3")
            conn.execute("CREATE TABLE articles(source_code TEXT, published_at TEXT, original_url TEXT, content_sha256 TEXT)"); conn.commit(); conn.close()
            queue = root / "queue.ndjson"; queue.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
            html = "<html><body><h1>Laos China</h1><p>Laos and China cooperation " + "continues " * 12 + "</p></body></html>"
            fetcher = Fetcher({rows[1]["archive_url"]: html.encode()})
            result = collect(root=root, max_replays=1, fetcher=fetcher, queue_file=queue)
            self.assertEqual(result["article_replays_total"], 1); self.assertEqual(result["importable_records"], 1)
            self.assertEqual(fetcher.calls, [rows[1]["archive_url"]]); self.assertTrue(verify(root)["all_verified"])
            resumed = Fetcher({}); result = collect(root=root, max_replays=1, fetcher=resumed, queue_file=queue)
            self.assertEqual(result["article_replays_this_run"], 0); self.assertEqual(resumed.calls, [])
            conn = sqlite3.connect(root / "data/corpus.sqlite3")
            saved = json.loads((root / "data/staging/pasaxon_round6_wayback/records.ndjson").read_text())
            conn.execute(
                "INSERT INTO articles VALUES(?,?,?,?)",
                ("pasaxon_archive", saved["published_at"], saved["original_url"], saved["content_sha256"]),
            )
            conn.commit(); conn.close()
            reconciled = reconcile_current_database(root)
            self.assertEqual(reconciled["moved_database_duplicates"], 1)
            self.assertEqual(reconciled["importable_records"], 0)


if __name__ == "__main__": unittest.main()
