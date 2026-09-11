from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.archives.pasaxon_round3 import (
    AtomicJsonLedger,
    collect,
    discover_path_index,
)
from laos_china_corpus.archives.wayback import WaybackCapture, save_cdx


def _article(title: str, body: str) -> bytes:
    return f"<html><body><h1>{title}</h1><p>{body}</p></body></html>".encode("utf-8")


class FakeFetcher:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payloads[url]


class PasaxonRound3Tests(unittest.TestCase):
    def test_isolated_lao_name_substring_is_not_china(self) -> None:
        from laos_china_corpus.archives.pasaxon_round2 import _china_hits
        self.assertEqual(
            _china_hits("ADB project", "\u0e99\u0eb3\u0ec2\u0e94\u0e8d\u0e97\u0ec8\u0eb2\u0e99 \u0e88\u0ebb\u0e87\u0e88\u0eb5\u0e99\u0eb2\u0e8d \u0eab\u0ebb\u0ea7\u0eab\u0e99\u0ec9\u0eb2\u0eab\u0ec9\u0ead\u0e87\u0e81\u0eb2\u0e99"),
            [],
        )
        self.assertIn(
            "\u0e88\u0eb5\u0e99",
            _china_hits("Aid", "\u0ec4\u0e94\u0ec9\u0eae\u0eb1\u0e9a\u0e81\u0eb2\u0e99\u0e8a\u0ec8\u0ea7\u0e8d\u0ec0\u0eab\u0ea5\u0eb7\u0ead\u0e88\u0eb2\u0e81\u0ea5\u0eb1\u0e94\u0e96\u0eb0\u0e9a\u0eb2\u0e99 \u0e88\u0eb5\u0e99"),
        )
        self.assertEqual(
            _china_hits("Vietnam", "\u0e9e\u0eb1\u0e81\u0e81\u0ead\u0ea1\u0ea1\u0eb9\u0e99\u0eb4\u0e94\u0ead\u0eb4\u0e99\u0e94\u0eb9\u0e88\u0eb5\u0e99"),
            [],
        )

    def test_path_index_discovery_is_bounded_and_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "articles.json"
            capture = WaybackCapture(
                "20130101000001", "http://www.pasaxon.org.la/articles/6-2-12/6.htm"
            )
            rows = [
                ["timestamp", "original", "mimetype", "digest"],
                [capture.timestamp, capture.original_url, "text/html", "ABC"],
            ]
            class IndexFetcher:
                def __init__(self) -> None:
                    self.urls: list[str] = []
                def fetch(self, url: str) -> bytes:
                    self.urls.append(url)
                    return json.dumps(rows).encode()
            fetcher = IndexFetcher()
            self.assertEqual(
                discover_path_index(
                    path_prefix="articles", output=output, fetcher=fetcher
                ),
                1,
            )
            self.assertIn("articles%2F%2A", fetcher.urls[0])
            self.assertIn("from=2012", fetcher.urls[0])
            self.assertIn("to=2020", fetcher.urls[0])
            self.assertTrue(output.exists())

    def test_atomic_ledger_reloads_latest_complete_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "screened.ndjson"
            ledger = AtomicJsonLedger(path)
            ledger.put({"canonical_url": "http://example/a", "status": "not_china"})
            ledger.put({"canonical_url": "http://example/b", "status": "china_match"})
            loaded = AtomicJsonLedger(path)
            self.assertEqual(len(loaded.rows), 2)
            self.assertEqual(loaded.get("http://example/b")["status"], "china_match")
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)

    def test_collect_is_resume_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            database = root / "data" / "corpus.sqlite3"
            conn = sqlite3.connect(database)
            conn.execute(
                "CREATE TABLE articles(source_code TEXT, published_at TEXT, original_url TEXT)"
            )
            conn.commit()
            conn.close()
            captures = [
                WaybackCapture("20130101000001", "http://www.pasaxon.org.la/conten/1-4-12/1.htm"),
                WaybackCapture("20130101000002", "http://www.pasaxon.org.la/conten/2-4-12/1.htm"),
                WaybackCapture("20130101000003", "http://www.pasaxon.org.la/conten/3-4-12/1.htm"),
            ]
            index = root / "index.json"
            save_cdx(captures, index)
            payloads = {
                captures[0].replay_url: _article("Domestic report", "Local affairs " * 10),
                captures[1].replay_url: _article("China cooperation", "Laos and China cooperate " * 8),
                captures[2].replay_url: _article("China visit", "Chinese delegation in Laos " * 8),
            }
            first = FakeFetcher(payloads)
            summary = collect(
                root=root,
                index_file=index,
                months=["2012-04"],
                target_per_month=2,
                fetcher=first,
                database=database,
            )
            self.assertEqual(summary["network_replays_this_run"], 3)
            self.assertEqual(summary["importable_records_total"], 2)
            second = FakeFetcher(payloads)
            resumed = collect(
                root=root,
                index_file=index,
                months=["2012-04"],
                target_per_month=2,
                fetcher=second,
                database=database,
            )
            self.assertEqual(resumed["network_replays_this_run"], 0)
            self.assertEqual(second.calls, [])
            self.assertEqual(resumed["ledger_records_total"], 3)
            self.assertEqual(resumed["importable_records_total"], 2)
            records = (root / "data" / "staging" / "pasaxon_round3" / "records.ndjson")
            self.assertEqual(len(records.read_text(encoding="utf-8").splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
