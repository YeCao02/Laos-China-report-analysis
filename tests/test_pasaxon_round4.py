from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.archives.pasaxon_round4 import (
    collect,
    clean_modern_article_segment,
    load_candidates,
    php_target_month,
    url_key,
    verify,
)
from laos_china_corpus.archives.wayback import WaybackCapture, save_cdx


class Fetcher:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []
    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payloads[url]


def article(title: str, body: str, date: str | None = None) -> bytes:
    printed = f"<div>{date}</div>" if date else ""
    return f"<html><body><h1>{title}</h1>{printed}<p>{body}</p></body></html>".encode()


class Round4Tests(unittest.TestCase):
    def test_recovers_article_title_after_navigation(self) -> None:
        title, body = clean_modern_article_segment(
            "\u0eab\u0e99\u0eb1\u0e87\u0eaa\u0eb7\u0e9e\u0eb4\u0ea1 \u0e9b\u0eb0\u0e8a\u0eb2\u0e8a\u0ebb\u0e99",
            "\u0eab\u0e99\u0ec9\u0eb2\u0e97\u0eb3\u0ead\u0eb4\u0e94\n\n\u0eaa\u0eb0\u0edd\u0eb1\u0e81\u0eaa\u0eb0\u0ea1\u0eb2\u0e8a\u0eb4\u0e81\n\n"
            "\u0eae\u0ead\u0e87\u0e99\u0eb2\u0e8d\u0ebb\u0e81\u0ea2\u0ec9\u0ebd\u0ea1\u0ea2\u0eb2\u0ea1\u0e97\u0eb2\u0e87\u0ea5\u0ebb\u0e94\u0ec4\u0e9f \u0ea5\u0eb2\u0ea7-\u0e88\u0eb5\u0e99\n\n"
            + ("\u0ea5\u0eb2\u0ea7 \u0ec1\u0ea5\u0eb0 \u0e88\u0eb5\u0e99 \u0eae\u0ec8\u0ea7\u0ea1\u0ea1\u0eb7\u0e81\u0eb1\u0e99. " * 10),
        )
        self.assertIn("\u0ea5\u0eb2\u0ea7-\u0e88\u0eb5\u0e99", title)
        self.assertNotIn("\u0eab\u0e99\u0ec9\u0eb2\u0e97\u0eb3\u0ead\u0eb4\u0e94", body)

    def test_keys_and_php_windows(self) -> None:
        self.assertEqual(url_key("http://www.pasaxon.org.la:80/HOTNEWS/01-03-17/h1.html"), "/hotnews/01-03-17/h1.html")
        self.assertEqual(url_key("http://www.pasaxon.org.la/pasaxon-detail.php?p_id=1900&act=x"), "php:1900")
        self.assertEqual(php_target_month(1900), "2020-08")
        self.assertIsNone(php_target_month(1000))

    def test_bounded_resume_and_hash_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/catalog").mkdir(parents=True)
            with (root / "data/catalog/monthly_coverage.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["source", "year_month", "sample_story_count"])
                writer.writeheader()
                writer.writerow({"source": "PASAXON", "year_month": "2015-08", "sample_story_count": 1})
            database = root / "data/corpus.sqlite3"
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE articles(source_code TEXT, original_url TEXT)")
            conn.commit(); conn.close()
            captures = [
                WaybackCapture("20150901000001", "http://www.pasaxon.org.la/index/01-08-15/content1.html"),
                WaybackCapture("20150901000002", "http://www.pasaxon.org.la/index/02-08-15/content1.html"),
            ]
            index = root / "index.json"
            save_cdx(captures, index)
            grouped, _ = load_candidates(root=root, index_files=[index])
            self.assertEqual(len(grouped["2015-08"]), 2)
            payloads = {
                captures[0].replay_url: article("Local", "Domestic affairs " * 10),
                captures[1].replay_url: article("China visit", "Laos and China cooperation " * 8),
            }
            first = Fetcher(payloads)
            result = collect(root=root, max_replays=2, fetcher=first, index_files=[index])
            self.assertEqual(result["article_replays_total"], 2)
            self.assertEqual(result["importable_records"], 1)
            second = Fetcher(payloads)
            resumed = collect(root=root, max_replays=2, fetcher=second, index_files=[index])
            self.assertEqual(resumed["article_replays_this_run"], 0)
            self.assertEqual(second.calls, [])
            self.assertTrue(verify(root)["all_verified"])


if __name__ == "__main__":
    unittest.main()
