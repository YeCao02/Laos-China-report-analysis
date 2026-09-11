from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.archives.pasaxon_round5 import collect, read_queue, verify


class Fetcher:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []
    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payloads[url]


def page(title: str, body: str, printed_date: str | None = None) -> bytes:
    date = f"<div>{printed_date}</div>" if printed_date else ""
    return f"<html><body><h1>{title}</h1>{date}<p>{body}</p></body></html>".encode()


class Round5Tests(unittest.TestCase):
    def test_queue_resume_and_prior_quota_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/catalog").mkdir(parents=True)
            with (root / "data/catalog/monthly_coverage.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["source", "year_month", "sample_story_count"])
                writer.writeheader()
                writer.writerow({"source": "PASAXON", "year_month": "2017-10", "sample_story_count": 2})
                writer.writerow({"source": "PASAXON", "year_month": "2018-04", "sample_story_count": 0})
            conn = sqlite3.connect(root / "data/corpus.sqlite3")
            conn.execute("CREATE TABLE articles(source_code TEXT, published_at TEXT, original_url TEXT, content_sha256 TEXT)")
            conn.commit(); conn.close()
            round4 = root / "data/staging/pasaxon_round4_wayback"
            round4.mkdir(parents=True)
            queue = [
                {
                    "url_key": "/hotnews/10-10-17/h1.html", "target_month": "2017-10",
                    "source_path": "hotnews", "original_url": "http://www.pasaxon.org.la/hotnews/10-10-17/h1.html",
                    "archive_url": "https://web.archive.org/web/20171011id_/http://www.pasaxon.org.la/hotnews/10-10-17/h1.html",
                    "archive_capture_timestamp": "20171011", "exact_url_date": "2017-10-10",
                },
                {
                    "url_key": "/hotnews/10-04-18/h1.html", "target_month": "2018-04",
                    "source_path": "hotnews", "original_url": "http://www.pasaxon.org.la/hotnews/10-04-18/h1.html",
                    "archive_url": "https://web.archive.org/web/20180411id_/http://www.pasaxon.org.la/hotnews/10-04-18/h1.html",
                    "archive_capture_timestamp": "20180411", "exact_url_date": "2018-04-10",
                },
            ]
            queue_path = round4 / "pending_queue.ndjson"
            queue_path.write_text("".join(json.dumps(x) + "\n" for x in queue), encoding="utf-8")
            self.assertEqual(len(read_queue(queue_path)), 2)
            fetcher = Fetcher({
                queue[1]["archive_url"]: page("China cooperation", "Laos and China cooperate " * 8),
            })
            result = collect(root=root, max_replays=1, fetcher=fetcher)
            self.assertEqual(result["article_replays_total"], 1)
            self.assertEqual(result["importable_records"], 1)
            self.assertEqual(fetcher.calls, [queue[1]["archive_url"]])
            resumed = Fetcher({})
            result2 = collect(root=root, max_replays=1, fetcher=resumed)
            self.assertEqual(result2["article_replays_this_run"], 0)
            self.assertEqual(resumed.calls, [])
            self.assertTrue(verify(root)["all_verified"])

    def test_queue_rejects_non_wayback_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.ndjson"
            row = {
                "url_key": "/x", "target_month": "2018-01", "source_path": "x",
                "original_url": "http://www.pasaxon.org.la/x",
                "archive_url": "https://example.org/x", "archive_capture_timestamp": "20180101",
            }
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside web.archive.org"):
                read_queue(path)


if __name__ == "__main__":
    unittest.main()
