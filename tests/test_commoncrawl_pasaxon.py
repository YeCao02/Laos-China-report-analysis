from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.archives.commoncrawl import parse_index_response
from laos_china_corpus.archives.commoncrawl_pasaxon import (
    collect_from_index_files,
    collect_from_records,
    quarantine_contaminated_staging,
)
from laos_china_corpus.archives.wayback import old_pasaxon_url_parts
from laos_china_corpus.config import ProjectPaths
from laos_china_corpus.db import connect


class CommonCrawlPasaxonTests(unittest.TestCase):
    @staticmethod
    def _record(url: str, length: int):
        return parse_index_response([{
            "url": url, "timestamp": "20120403000000", "filename": "x.arc.gz",
            "offset": 0, "length": length, "status": "200",
        }])

    def test_collector_writes_atomic_ledger_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            connect(paths.database).close()
            china = "\u0e88\u0eb5\u0e99"
            body = f"<html><body><h1>{china}</h1><p>{china * 30}</p></body></html>"
            member = gzip.compress(b"ARC\n" + body.encode())
            records = self._record("http://www.pasaxon.org.la/conten/2-4-12/1.htm", len(member))

            def fetcher(_):
                return member, body.encode()

            first = collect_from_records(root=root, records=records, interval=0, fetcher=fetcher)
            second = collect_from_records(root=root, records=records, interval=0, fetcher=fetcher)
            self.assertEqual(first["matches"], 1)
            self.assertEqual(second["screened_new"], 0)
            staging = root / "data/staging/archive_ocr/commoncrawl_pasaxon_2012"
            self.assertEqual(len((staging / "records.ndjson").read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(len((staging / "screened.ndjson").read_text(encoding="utf-8").splitlines()), 1)

    def test_mojibake_title_is_replaced_and_failed_fetch_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            connect(paths.database).close()
            headline = "\u0e9e\u0eb2\u0e94\u0eab\u0ebb\u0ea7\u0e82\u0ecd\u0ec9"
            china = "\u0e88\u0eb5\u0e99"
            paragraphs = [headline] + [(china + " ") * 20 + str(index) for index in range(6)]
            body = "\n".join(paragraphs)
            html_body = "".join(f"<p>{part}</p>" for part in paragraphs)
            payload = f"<html><head><title>mojibake-cjk-title</title></head><body>{html_body}</body></html>".encode()
            member = gzip.compress(b"ARC\n" + payload)
            records = self._record("http://www.pasaxon.org.la/conten/2-4-12/2.htm", len(member))
            calls = 0

            def fetcher(_):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("transient 503")
                return member, payload

            failed = collect_from_records(root=root, records=records, interval=0, fetcher=fetcher)
            retried = collect_from_records(root=root, records=records, interval=0, fetcher=fetcher)
            self.assertEqual(failed["failures"], 1)
            self.assertEqual(retried["matches"], 1)
            row = json.loads((root / "data/staging/archive_ocr/commoncrawl_pasaxon_2012/records.ndjson").read_text(encoding="utf-8"))
            self.assertTrue(row["title_original"].startswith(headline))


    def test_new_pasaxon_year_month_day_paths_are_dated(self):
        self.assertEqual(
            old_pasaxon_url_parts("http://pasaxon.org.la/econo/2019/11/15/e3.html"),
            ("2019-11-15", 3),
        )
        self.assertEqual(
            old_pasaxon_url_parts("http://pasaxon.org.la/cooperation/2019/11/26-11-19/2.html"),
            ("2019-11-26", 2),
        )

    def test_per_month_quota_stops_range_requests_and_preserves_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            connect(paths.database).close()
            body = "<html><body><h1>China cooperation</h1><p>" + "China and Laos cooperation report. " * 20 + "</p></body></html>"
            member = gzip.compress(b"WARC/1.0\r\n\r\n" + body.encode())
            records = parse_index_response([{
                "url": f"http://pasaxon.org.la/econo/2019/11/15/e{slot}.html",
                "timestamp": "20191201000000", "filename": "x.warc.gz",
                "offset": slot * 100, "length": len(member), "status": "200",
            } for slot in range(1, 4)])
            calls = 0

            def fetcher(_):
                nonlocal calls
                calls += 1
                return member, body.encode()

            result = collect_from_records(
                root=root, records=records, staging_name="bounded", interval=0,
                max_candidates=120, max_matches_per_month=2, fetcher=fetcher,
            )
            record_path = root / "data/staging/archive_ocr/bounded/records.ndjson"
            rows = [json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(calls, 2)
            self.assertEqual(result["matches"], 2)
            self.assertEqual(result["month_quota_skipped"], 1)
            self.assertTrue(all(len(row["raw_sha256"]) == 64 for row in rows))
            self.assertTrue(all(len(row["payload_sha256"]) == 64 for row in rows))

    def test_saved_index_orchestrator_enforces_per_index_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            connect(paths.database).close()
            body = "<html><body><h1>General report</h1><p>" + "ordinary domestic reporting " * 20 + "</p></body></html>"
            member = gzip.compress(b"WARC/1.0\r\n\r\n" + body.encode())
            index_file = root / "commoncrawl_pasaxon_CC-MAIN-2019-51.jsonl"
            rows = []
            for slot in range(1, 6):
                rows.append({
                    "original_url": f"http://pasaxon.org.la/econo/2019/11/15/e{slot}.html",
                    "capture_timestamp": "20191201000000",
                    "warc": {"filename": "x.warc.gz", "offset": slot * 100,
                             "length": len(member), "capture_timestamp": "20191201000000"},
                    "metadata": {"status": 200, "mime": "text/html"},
                })
            index_file.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
            calls = 0

            def fetcher(_):
                nonlocal calls
                calls += 1
                return member, body.encode()

            manifest = collect_from_index_files(
                root=root, index_files=[index_file], staging_name="isolated",
                max_per_index=3, max_matches_per_month=2, interval=0, fetcher=fetcher,
            )
            self.assertEqual(calls, 3)
            self.assertEqual(manifest["max_per_index"], 3)
            self.assertEqual(
                manifest["per_index"]["CC-MAIN-2019-51"]["range_requests"], 3
            )
            self.assertEqual(len(manifest["inputs"][0]["sha256"]), 64)

    def test_php_article_requires_visible_page_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            connect(paths.database).close()
            dated = "<html><body><h1>China cooperation</h1><p>21/02/2020</p><p>" + "China Laos cooperation. " * 20 + "</p></body></html>"
            undated = dated.replace("21/02/2020", "date unavailable")
            dated_member = gzip.compress(b"WARC/1.0\r\n\r\n" + dated.encode())
            undated_member = gzip.compress(b"WARC/1.0\r\n\r\n" + undated.encode())
            records = parse_index_response([{
                "url": f"http://pasaxon.org.la/pasaxon-detail.php?p_id={pid}&act=cooperation-detail",
                "timestamp": "20201201000000", "filename": "x.warc.gz",
                "offset": pid * 100, "length": len(member), "status": "200",
            } for pid, member in ((100, dated_member), (101, undated_member))])

            def fetcher(record):
                member, body = (dated_member, dated) if "p_id=100" in record.url else (undated_member, undated)
                return member, body.encode()

            result = collect_from_records(
                root=root, records=records, staging_name="php", interval=0,
                max_matches_per_month=2, allow_page_date=True, fetcher=fetcher,
            )
            rows = [json.loads(line) for line in (root / "data/staging/archive_ocr/php/records.ndjson").read_text(encoding="utf-8").splitlines()]
            ledger = [json.loads(line) for line in (root / "data/staging/archive_ocr/php/screened.ndjson").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["range_requests"], 2)
            self.assertEqual(rows[0]["published_at"], "2020-02-21")
            self.assertEqual(rows[0]["date_precision"], "page_day")
            self.assertIn("unverifiable_date", {row["status"] for row in ledger})
            self.assertTrue(all(row.get("raw_file") for row in ledger if row["status"] == "unverifiable_date"))

    def test_contaminated_php_template_is_not_importable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ProjectPaths(root)
            paths.ensure()
            connect(paths.database).close()
            spam = "<html><body><h1>ຈີນ</h1><p>09/01/2020</p><p>Toggle navigation agen togel daftar sbobet bertaruh online lapak online</p></body></html>"
            member = gzip.compress(b"WARC/1.0\r\n\r\n" + spam.encode())
            record = parse_index_response([{
                "url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=315&act=pasaxon-detail",
                "timestamp": "20201201000000", "filename": "x.warc.gz",
                "offset": 100, "length": len(member), "status": "200",
            }])[0]
            result = collect_from_records(
                root=root, records=[record], staging_name="spam", interval=0,
                allow_page_date=True, fetcher=lambda _: (member, spam.encode()),
            )
            ledger = json.loads((root / "data/staging/archive_ocr/spam/screened.ndjson").read_text(encoding="utf-8"))
            self.assertEqual(result["matches"], 0)
            self.assertEqual(ledger["status"], "contaminated_template")
            self.assertFalse((root / "data/staging/archive_ocr/spam/records.ndjson").exists())
            self.assertEqual(quarantine_contaminated_staging(root=root, staging_name="spam")["records_removed"], 0)


if __name__ == "__main__":
    unittest.main()
