from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from laos_china_corpus.config import ProjectPaths
from laos_china_corpus.db import connect
from laos_china_corpus.staging_import import (
    import_pasaxon_commoncrawl, import_pasaxon_epaper_articles, import_pasaxon_listing_discoveries,
    import_pasaxon_round2, import_pasaxon_wayback_upgrades,
)


class StagingImportTests(unittest.TestCase):
    def test_current_official_import_preserves_identity_and_a1_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.ensure()
            payload = b"<html>current official</html>"
            raw_sha = hashlib.sha256(payload).hexdigest()
            raw = paths.staging / f"{raw_sha}.html.gz"
            raw.write_bytes(gzip.compress(payload))
            body = "ຂ່າວ ລາວ ຈີນ"
            row = {
                "record_id": "PASAXON-LO-14516", "story_id": "PASAXON-STORY-14516",
                "source_code": "pasaxon", "source_article_id": "14516", "language": "lo",
                "original_url": "https://pasaxon.org.la/china-14516.html",
                "archive_url": None, "search_url": "https://pasaxon.org.la/tags/x.html?page=40",
                "published_at": "2025-07-02T13:38:00+07:00", "date_precision": "article_timestamp",
                "body_original": body, "body_method": "pasaxon_dom", "title_original": "ຂ່າວຈີນ",
                "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "raw_file": str(raw.relative_to(paths.root)), "raw_sha256": raw_sha,
                "evidence_grade": "A1", "matched_queries": ["ຈີນ"],
                "retrieved_at": "2026-08-13T00:00:00Z",
            }
            ndjson = paths.staging / "current-records.ndjson"
            ndjson.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            conn = connect(paths.database)
            self.assertEqual(import_pasaxon_round2(conn, paths, ndjson)["imported"], 1)
            article = conn.execute("SELECT * FROM articles").fetchone()
            self.assertEqual(article["record_id"], "PASAXON-LO-14516")
            self.assertEqual(article["source_code"], "pasaxon")
            self.assertEqual(article["evidence_grade"], "A1")
            evidence = conn.execute("SELECT * FROM evidence_objects").fetchone()
            self.assertEqual(evidence["evidence_type"], "official_current_html")
            self.assertEqual(evidence["evidence_grade"], "A1")
            conn.close()

    def test_validated_import_is_idempotent_and_repairs_corrupt_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.ensure()
            payload = b"<html>official</html>"
            raw_sha = hashlib.sha256(payload).hexdigest()
            raw = paths.staging / f"{raw_sha}.html.gz"
            raw.write_bytes(gzip.compress(payload))
            body = "ລາວ ຈີນ\n正文"
            row = {
                "original_url": "http://www.pasaxon.org.la:80/conten/9-2-12/1.htm",
                "archive_url": "https://web.archive.org/web/20120319042214id_/http://www.pasaxon.org.la/conten/9-2-12/1.htm",
                "published_at": "2012-02-09", "body_original": body,
                "body_method": "wayback_direct_pasaxon_html", "title_original": "���",
                "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "raw_file": str(raw.relative_to(paths.root)), "raw_sha256": raw_sha,
                "matched_queries": ["ຈີນ"], "retrieved_at": "2026-08-13T00:00:00Z",
            }
            ndjson = paths.staging / "records.ndjson"
            ndjson.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            conn = connect(paths.database)
            self.assertEqual(import_pasaxon_round2(conn, paths, ndjson)["imported"], 1)
            self.assertEqual(import_pasaxon_round2(conn, paths, ndjson)["skipped"], 1)
            article = conn.execute("SELECT * FROM articles").fetchone()
            self.assertEqual(article["title_original"], "ລາວ ຈີນ")
            self.assertEqual(article["evidence_grade"], "B1")
            self.assertEqual(conn.execute("SELECT count(*) FROM evidence_objects").fetchone()[0], 1)
            conn.close()

    def test_commoncrawl_import_validates_member_hash_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.ensure()
            member = gzip.compress(b"ARC member")
            raw = paths.staging / "member.arc.gz"
            raw.write_bytes(member)
            body = "ຂ່າວລາວຈີນ\n" + ("ລາວ ຈີນ " * 20)
            row = {
                "original_url": "http://www.pasaxon.org.la/conten/27-1-12/2.htm",
                "archive_url": "https://data.commoncrawl.org/x.arc.gz",
                "published_at": "2012-01-27", "body_original": body,
                "body_method": "commoncrawl_arc_pasaxon_html", "title_original": "脜脿脜录",
                "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "raw_file": str(raw.relative_to(paths.root)),
                "raw_sha256": hashlib.sha256(member).hexdigest(),
                "matched_queries": ["ຈີນ"], "retrieved_at": "2026-08-13T00:00:00Z",
                "capture_timestamp": "20120128000000", "warc": {"offset": 0, "length": len(member)},
            }
            ndjson = paths.staging / "cc-records.ndjson"
            ndjson.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            conn = connect(paths.database)
            self.assertEqual(import_pasaxon_commoncrawl(conn, paths, ndjson)["imported"], 1)
            self.assertEqual(import_pasaxon_commoncrawl(conn, paths, ndjson)["skipped"], 1)
            article = conn.execute("SELECT * FROM articles").fetchone()
            self.assertEqual(article["title_original"], "ຂ່າວລາວຈີນ")
            self.assertEqual(article["evidence_grade"], "B1")
            conn.close()

    def test_listing_discovery_imports_a2_without_body_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.ensure()
            payload = b"<html>official listing</html>"
            response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + payload
            warc = b"WARC/1.0\r\nWARC-Type: response\r\nContent-Length: " + str(len(response)).encode() + b"\r\n\r\n" + response
            member = gzip.compress(warc)
            raw = paths.staging / "listing.warc.gz"
            raw.write_bytes(member)
            row = {
                "p_id": 3413, "title_original": "ການນຳລາວ-ຈີນ",
                "published_at": "2021-02-17",
                "original_url": "http://pasaxon.org.la/pasaxon-detail.php?p_id=3413&act=cooperation-detail",
                "listing_url": "http://pasaxon.org.la/showlistcooperation.php",
                "listing_kind": "/showlistcooperation.php",
                "archive_url": "https://data.commoncrawl.org/x.warc.gz",
                "raw_file": str(raw.relative_to(paths.root)),
                "raw_sha256": hashlib.sha256(member).hexdigest(),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "matched_queries": ["ຈີນ"], "retrieved_at": "2026-09-01T00:00:00Z",
            }
            ndjson = paths.staging / "listing.ndjson"
            ndjson.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            conn = connect(paths.database)
            result = import_pasaxon_listing_discoveries(conn, paths, ndjson)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["evidence_linked"], 1)
            repeat = import_pasaxon_listing_discoveries(conn, paths, ndjson)
            self.assertEqual(repeat["skipped_existing_body_or_candidate"], 1)
            article = conn.execute("SELECT * FROM articles").fetchone()
            self.assertIsNone(article["body_original"])
            self.assertEqual(article["evidence_grade"], "A2")
            self.assertEqual(conn.execute("SELECT count(*) FROM evidence_objects").fetchone()[0], 1)
            body = "ລາວ ຈີນ " * 30
            body_file = paths.staging / "body.txt"
            body_file.write_text(body, encoding="utf-8")
            wayback_payload = b"<html>verified detail</html>"
            wayback_raw = paths.staging / "wayback.html.gz"
            wayback_raw.write_bytes(gzip.compress(wayback_payload))
            upgrade = {
                "record_id": article["record_id"], "source_article_id": "3413",
                "title_original": "ການນຳລາວ-ຈີນ", "published_at": "2021-02-17",
                "date_precision": "page_day_verified_against_listing",
                "body_original": body, "body_method": "wayback_pasaxon_php_html_hidden_injection_removed",
                "matched_queries": ["ຈີນ"], "archive_url": "https://web.archive.org/web/x",
                "body_file": str(body_file.relative_to(paths.root)),
                "raw_file": str(wayback_raw.relative_to(paths.root)),
                "raw_sha256": hashlib.sha256(wayback_payload).hexdigest(),
                "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "retrieved_at": "2026-09-01T00:00:00Z", "metadata": {"archive_provider": "Wayback"},
            }
            upgrade_file = paths.staging / "upgrade.ndjson"
            upgrade_file.write_text(json.dumps(upgrade, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(import_pasaxon_wayback_upgrades(conn, paths, upgrade_file)["upgraded"], 1)
            self.assertEqual(import_pasaxon_wayback_upgrades(conn, paths, upgrade_file)["skipped"], 1)
            upgraded = conn.execute("SELECT * FROM articles").fetchone()
            self.assertEqual(upgraded["evidence_grade"], "B1")
            self.assertEqual(upgraded["body_original"], body)
            self.assertEqual(conn.execute("SELECT count(*) FROM evidence_objects").fetchone()[0], 2)
            conn.close()

    def test_epaper_import_validates_pdf_and_native_parser_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.ensure()
            pdf = paths.staging / "issue.pdf"
            pdf.write_bytes(b"%PDF-1.7\nverified")
            pdf_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            native = paths.staging / "native.json"
            native_payload = {
                "published_at": "2022-01-28", "pdf_sha256": pdf_sha,
                "pages": [{"page_num": 3}],
            }
            native.write_text(json.dumps(native_payload), encoding="utf-8")
            body = "ຂ່າວ ສປ ຈີນ " * 50
            body_file = paths.staging / "article.txt"
            body_file.write_text(body, encoding="utf-8")
            row = {
                "record_id": "PASAXON-EPAPER-LO-test", "story_id": "PASAXON-EPAPER-STORY-test",
                "source_code": "pasaxon_archive", "source_article_id": "epaper-2022-01-28-p3",
                "language": "lo", "published_at": "2022-01-28",
                "title_original": "ສະຖານທູດຈີນ ມອບເຄື່ອງຊ່ວຍເຫຼືອ",
                "body_original": body, "body_method": "liteparse_native_pdf_region",
                "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "china_note_zh": "中国使馆捐赠物资。", "topic_labels": ["bilateral"],
                "content_origin": "local_original", "matched_queries": ["ຈີນ"],
                "original_url": "http://pasaxon.org.la/pdf-detail.php?p_id=470&act=pdf-detail",
                "archive_url": "https://web.archive.org/web/x/http://pasaxon.org.la/a.pdf",
                "body_file": str(body_file.relative_to(paths.root)),
                "raw_file": str(pdf.relative_to(paths.root)), "raw_sha256": pdf_sha,
                "native_json_file": str(native.relative_to(paths.root)),
                "native_json_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
                "evidence_grade": "B1", "retrieved_at": "2026-09-01T00:00:00Z",
                "metadata": {"page_num": 3, "column_ranges": [[4, 45]], "y_range": [64, 112]},
            }
            ndjson = paths.staging / "epaper.ndjson"
            ndjson.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            conn = connect(paths.database)
            self.assertEqual(import_pasaxon_epaper_articles(conn, paths, ndjson)["imported"], 1)
            self.assertEqual(import_pasaxon_epaper_articles(conn, paths, ndjson)["skipped"], 1)
            article = conn.execute("SELECT * FROM articles").fetchone()
            self.assertEqual(article["body_method"], "liteparse_native_pdf_region")
            evidence = conn.execute("SELECT * FROM evidence_objects").fetchone()
            self.assertEqual(evidence["evidence_type"], "wayback_official_epaper_pdf_native_text")
            conn.close()


if __name__ == "__main__":
    unittest.main()
