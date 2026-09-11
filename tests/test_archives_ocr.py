from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gzip
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from laos_china_corpus.archives.commoncrawl import (
    build_index_query,
    extract_archive_http_payload,
    fetch_archive_payload,
    parse_index_response,
    records_to_historical_candidates,
    to_historical_candidate,
)
from laos_china_corpus.archives import ocr


FIXTURES = Path(__file__).parent / "fixtures"


def _touch_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "archive_scan.pdf"
    path.write_bytes(b"%PDF-test-double")
    return path


@dataclass
class FakeItem:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float


@dataclass
class FakePage:
    page_num: int
    text_items: list[FakeItem]
    text: str = ""


@dataclass
class FakeResult:
    pages: list[FakePage]


class ArchiveOCRTests(unittest.TestCase):
    def test_arc_and_warc_range_payload_extraction(self) -> None:
        body = b"<html>China Laos</html>"
        arc = gzip.compress(
            b"http://example.test/a 127.0.0.1 20120101000000 text/html 25\n"
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + body
        )
        warc = gzip.compress(
            b"WARC/1.0\r\nWARC-Type: response\r\n\r\n"
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + body
        )
        self.assertEqual(extract_archive_http_payload(arc), body)
        self.assertEqual(extract_archive_http_payload(warc), body)

        record = parse_index_response([{
            "url": "http://example.test/a", "timestamp": "20120101000000",
            "filename": "x.arc.gz", "offset": 0, "length": len(arc), "status": "200",
        }])[0]

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self): return arc

        def opener(request, timeout):
            self.assertEqual(request.headers["Range"], record.range_header)
            self.assertEqual(timeout, 3)
            return Response()

        raw, extracted = fetch_archive_payload(record, timeout=3, opener=opener)
        self.assertEqual(raw, arc)
        self.assertEqual(extracted, body)

    def test_commoncrawl_query_preserves_repeated_filters_and_capture_bounds(self) -> None:
        url = build_index_query(
            "CC-MAIN-2015-06",
            "kpl.net.la/*",
            from_timestamp=2012,
            to_timestamp=2014,
            filters=("status:200", "mime:text/html"),
            match_type="domain",
            page=0,
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/CC-MAIN-2015-06-index")
        self.assertEqual(query["url"], ["kpl.net.la/*"])
        self.assertEqual(query["filter"], ["status:200", "mime:text/html"])
        self.assertEqual(query["from"], ["2012"])
        self.assertEqual(query["to"], ["2014"])
        self.assertEqual(query["collapse"], ["digest"])

    def test_commoncrawl_response_produces_range_locator_without_publication_date(self) -> None:
        records = parse_index_response((FIXTURES / "archive_commoncrawl.ndjson").read_bytes())
        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first.status, 200)
        self.assertEqual(first.extra["charset"], "UTF-8")
        self.assertEqual(first.range_header, "bytes=5678-6911")
        self.assertEqual(first.captured_at, "2014-01-02T03:04:05Z")

        candidate = to_historical_candidate(first)
        self.assertIsNone(candidate.publication_date)
        self.assertEqual(candidate.date_precision, "unknown")
        self.assertEqual(candidate.capture_timestamp, "20140102030405")
        self.assertEqual(candidate.warc["range_header"], "bytes=5678-6911")
        self.assertEqual(candidate.evidence_grade, "C2")
        self.assertIsNone(records_to_historical_candidates(records)[1].publication_date)

    def test_commoncrawl_parser_reports_bad_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            parse_index_response('{"url":"https://example.test"}\n')

    def test_native_text_short_circuits_ocr(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _touch_pdf(Path(tmp))
            pages = [ocr.PageExtraction(1, "China Laos cooperation " * 10)]

            class MustNotRun:
                def __init__(self, **_: object) -> None:
                    raise AssertionError("OCR should not run for native text")

            with mock.patch.object(ocr, "extract_native_text", return_value=("pypdf", pages, [])):
                result = ocr.extract_pdf(path, liteparse_factory=MustNotRun)
            self.assertEqual(result.status, "native_text")
            self.assertEqual(result.method, "pypdf")
            self.assertFalse(result.needs_review)

    def test_missing_liteparse_is_structured_nonfatal_status(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _touch_pdf(Path(tmp))
            message = "LiteParse is not installed; install liteparse==2.0.0 to enable OCR"
            with mock.patch.object(ocr, "extract_native_text", return_value=("pypdf", [], [])), mock.patch.object(
                ocr, "_load_liteparse", return_value=(None, message)
            ):
                result = ocr.extract_pdf(path)
            self.assertEqual(result.status, "ocr_unavailable")
            self.assertTrue(result.needs_review)
            self.assertIn("ocr_dependency_unavailable", result.review_reasons)
            self.assertIn("LiteParse is not installed", result.message or "")

    def test_liteparse_ocr_uses_300dpi_lao_eng_and_marks_low_confidence(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _touch_pdf(Path(tmp))
            received: dict[str, object] = {}

            class FakeLiteParse:
                def __init__(self, **kwargs: object) -> None:
                    received.update(kwargs)

                def parse(self, source: str) -> FakeResult:
                    self_outer.assertEqual(source, str(path))
                    return FakeResult(
                        pages=[
                            FakePage(1, [FakeItem("ລາວ-ຈີນ", 1, 2, 30, 10, 91)]),
                            FakePage(2, [FakeItem("China", 3, 4, 20, 8, 0.42)]),
                        ]
                    )

            self_outer = self
            with mock.patch.object(ocr, "extract_native_text", return_value=("pypdf", [], [])):
                result = ocr.extract_pdf(path, liteparse_factory=FakeLiteParse)
            self.assertEqual(received["dpi"], 300)
            self.assertEqual(received["ocr_language"], "lao+eng")
            self.assertTrue(received["ocr_enabled"])
            self.assertEqual(result.status, "ocr_review_required")
            self.assertEqual(result.pages[0].text_items[0].bbox, (1.0, 2.0, 30.0, 10.0))
            self.assertAlmostEqual(result.pages[0].confidence or 0, 0.91)
            self.assertTrue(result.pages[1].needs_review)
            self.assertIn("low_ocr_confidence", result.pages[1].review_reasons)
            self.assertEqual(
                result.to_dict()["pages"][0]["text_items"][0]["bbox"],
                [1.0, 2.0, 30.0, 10.0],
            )

    def test_liteparse_high_confidence_is_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _touch_pdf(Path(tmp))

            class FakeLiteParse:
                def __init__(self, **_: object) -> None:
                    pass

                def parse(self, _: str) -> FakeResult:
                    return FakeResult([FakePage(1, [FakeItem("China", 0, 0, 10, 5, 0.95)])])

            with mock.patch.object(ocr, "extract_native_text", return_value=("none", [], [])):
                result = ocr.extract_pdf(path, liteparse_factory=FakeLiteParse)
            self.assertEqual(result.status, "ocr_complete")
            self.assertFalse(result.needs_review)
            self.assertAlmostEqual(result.confidence or 0, 0.95)


if __name__ == "__main__":
    unittest.main()
