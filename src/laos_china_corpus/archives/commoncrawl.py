"""Common Crawl discovery helpers.

The CDX ``timestamp`` describes when Common Crawl fetched a resource.  It is
deliberately exposed as ``captured_at`` and is never promoted to an article's
publication date.
"""

from __future__ import annotations

import json
import gzip
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen


INDEX_API_ROOT = "https://index.commoncrawl.org"
WARC_DATA_ROOT = "https://data.commoncrawl.org"


def _capture_datetime(value: str) -> str | None:
    """Convert a CDX timestamp to ISO-8601 without treating it as publication."""

    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return parsed.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CommonCrawlRecord:
    """One parsed row from the Common Crawl Index API."""

    url: str
    timestamp: str
    filename: str
    offset: int
    length: int
    status: int | None = None
    mime: str | None = None
    digest: str | None = None
    urlkey: str | None = None
    languages: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def captured_at(self) -> str | None:
        return _capture_datetime(self.timestamp)

    @property
    def warc_url(self) -> str:
        return f"{WARC_DATA_ROOT}/{self.filename.lstrip('/')}"

    @property
    def range_header(self) -> str:
        return f"bytes={self.offset}-{self.offset + self.length - 1}"

    def warc_locator(self) -> dict[str, Any]:
        """Return all metadata required for a HTTP range fetch of the WARC row."""

        return {
            "warc_url": self.warc_url,
            "filename": self.filename,
            "offset": self.offset,
            "length": self.length,
            "range_header": self.range_header,
            "captured_at": self.captured_at,
            "capture_timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceCandidate:
    """Archive discovery candidate, not yet a verified news article."""

    original_url: str
    evidence_url: str
    captured_at: str | None
    capture_timestamp: str
    evidence_type: str = "commoncrawl_warc"
    evidence_grade: str = "C2"
    publication_date: None = None
    date_precision: str = "unknown"
    warc: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_index_query(
    index: str,
    url_pattern: str,
    *,
    from_timestamp: str | int | None = None,
    to_timestamp: str | int | None = None,
    filters: Iterable[str] = ("status:200",),
    collapse: str | None = "digest",
    match_type: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    show_num_pages: bool = False,
) -> str:
    """Build a deterministic Common Crawl Index API URL.

    ``from_timestamp`` and ``to_timestamp`` may be years (``2012``), dates, or
    full CDX timestamps.  They constrain capture time only.
    """

    if not index or "/" in index or "?" in index:
        raise ValueError("index must be a Common Crawl index name, e.g. CC-MAIN-2025-30")
    if not url_pattern:
        raise ValueError("url_pattern must not be empty")

    params: list[tuple[str, str]] = [("url", url_pattern), ("output", "json")]
    if from_timestamp is not None:
        params.append(("from", str(from_timestamp)))
    if to_timestamp is not None:
        params.append(("to", str(to_timestamp)))
    for value in filters:
        params.append(("filter", value))
    if collapse:
        params.append(("collapse", collapse))
    if match_type:
        params.append(("matchType", match_type))
    if page is not None:
        if page < 0:
            raise ValueError("page must be non-negative")
        params.append(("page", str(page)))
    if page_size is not None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        params.append(("pageSize", str(page_size)))
    if show_num_pages:
        params.append(("showNumPages", "true"))
    endpoint = index if index.endswith("-index") else f"{index}-index"
    return f"{INDEX_API_ROOT}/{endpoint}?{urlencode(params)}"


def _record_from_mapping(row: Mapping[str, Any]) -> CommonCrawlRecord:
    required = ("url", "timestamp", "filename", "offset", "length")
    missing = [key for key in required if row.get(key) in (None, "")]
    if missing:
        raise ValueError(f"Common Crawl row missing required fields: {', '.join(missing)}")
    try:
        offset = int(row["offset"])
        length = int(row["length"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Common Crawl offset and length must be integers") from exc
    if offset < 0 or length <= 0:
        raise ValueError("Common Crawl offset must be non-negative and length positive")

    status_value = row.get("status")
    try:
        status = int(status_value) if status_value not in (None, "") else None
    except (TypeError, ValueError):
        status = None
    known = {
        "url", "timestamp", "filename", "offset", "length", "status", "mime",
        "digest", "urlkey", "languages",
    }
    return CommonCrawlRecord(
        url=str(row["url"]),
        timestamp=str(row["timestamp"]),
        filename=str(row["filename"]),
        offset=offset,
        length=length,
        status=status,
        mime=str(row["mime"]) if row.get("mime") is not None else None,
        digest=str(row["digest"]) if row.get("digest") is not None else None,
        urlkey=str(row["urlkey"]) if row.get("urlkey") is not None else None,
        languages=str(row["languages"]) if row.get("languages") is not None else None,
        extra={key: value for key, value in row.items() if key not in known},
    )


def parse_index_response(payload: str | bytes | Iterable[Mapping[str, Any]]) -> list[CommonCrawlRecord]:
    """Parse NDJSON (the normal API response) or already-decoded mappings."""

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        rows: list[Mapping[str, Any]] = []
        for line_number, line in enumerate(payload.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid Common Crawl JSON on line {line_number}: {exc.msg}") from exc
            if not isinstance(decoded, Mapping):
                raise ValueError(f"Common Crawl line {line_number} is not a JSON object")
            rows.append(decoded)
    else:
        rows = list(payload)
    return [_record_from_mapping(row) for row in rows]


def fetch_index(
    query_url: str,
    *,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urlopen,
    user_agent: str = "laos-china-media-corpus/0.1 (+research archive discovery)",
) -> list[CommonCrawlRecord]:
    """Fetch a prepared Index API query; ``opener`` is injectable for tests."""

    request = Request(query_url, headers={"User-Agent": user_agent, "Accept": "application/x-ndjson"})
    with opener(request, timeout=timeout) as response:
        return parse_index_response(response.read())


def extract_archive_http_payload(compressed_member: bytes) -> bytes:
    """Extract the archived HTTP entity body from one ARC/WARC gzip member."""

    try:
        member = gzip.decompress(compressed_member)
    except (OSError, EOFError) as exc:
        raise ValueError("Common Crawl range is not a complete gzip member") from exc
    if member.startswith(b"WARC/"):
        split = member.split(b"\r\n\r\n", 1)
        if len(split) != 2:
            raise ValueError("WARC record has no header terminator")
        response = split[1]
    else:
        # Legacy ARC: the first LF-terminated line is the ARC record header.
        split = member.split(b"\n", 1)
        if len(split) != 2 or not split[0].startswith((b"http://", b"https://")):
            raise ValueError("Unsupported Common Crawl ARC/WARC member")
        response = split[1]
    if response.startswith(b"HTTP/"):
        split = response.split(b"\r\n\r\n", 1)
        if len(split) != 2:
            raise ValueError("Archived HTTP response has no header terminator")
        body = split[1]
    else:
        body = response
    if body.startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise ValueError("Archived HTTP gzip entity is incomplete") from exc
    if not body:
        raise ValueError("Archived HTTP entity body is empty")
    return body


def fetch_archive_payload(
    record: CommonCrawlRecord,
    *,
    timeout: float = 45.0,
    opener: Callable[..., Any] = urlopen,
    user_agent: str = "laos-china-media-corpus/0.1 (+research archive retrieval)",
) -> tuple[bytes, bytes]:
    """Range-fetch one ARC/WARC member and return ``(raw_member, HTTP body)``."""

    request = Request(
        record.warc_url,
        headers={"User-Agent": user_agent, "Range": record.range_header, "Accept-Encoding": "identity"},
    )
    with opener(request, timeout=timeout) as response:
        raw_member = response.read()
    if len(raw_member) != record.length:
        raise ValueError(
            f"Common Crawl range length mismatch: expected {record.length}, got {len(raw_member)}"
        )
    return raw_member, extract_archive_http_payload(raw_member)


def to_historical_candidate(record: CommonCrawlRecord) -> HistoricalEvidenceCandidate:
    """Convert a CDX row to a C2 discovery candidate.

    Publication date is intentionally ``None``.  A separate page-level parser
    must recover and corroborate it before the candidate can be promoted.
    """

    metadata = {
        "status": record.status,
        "mime": record.mime,
        "digest": record.digest,
        "urlkey": record.urlkey,
        "languages": record.languages,
        **record.extra,
    }
    return HistoricalEvidenceCandidate(
        original_url=record.url,
        evidence_url=record.warc_url,
        captured_at=record.captured_at,
        capture_timestamp=record.timestamp,
        warc=record.warc_locator(),
        metadata={key: value for key, value in metadata.items() if value is not None},
    )


def records_to_historical_candidates(
    records: Iterable[CommonCrawlRecord],
) -> list[HistoricalEvidenceCandidate]:
    """Convert discovery rows while preserving capture/publication separation."""

    return [to_historical_candidate(record) for record in records]
