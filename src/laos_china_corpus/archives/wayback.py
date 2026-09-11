from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote, urlparse
from urllib.parse import urljoin
from urllib.request import Request, urlopen


CDX_ROOT = "https://web.archive.org/cdx/search/cdx"
REPLAY_ROOT = "https://web.archive.org/web"
_OLD_KPL_DATE = re.compile(
    r"/newsrecord/(?P<year>20\d{2})/(?P<month>[A-Za-z]+)/"
    r"(?P<day>\d{1,2})\.(?P<month_num>\d{1,2})\.(?P=year)/edn(?P<slot>\d+)\.htm$",
    re.IGNORECASE,
)
_OLD_PASAXON_DATE = re.compile(
    r"/(?:conten/|articles/)?(?P<day>\d{1,2})-(?P<month>\d{1,2})-(?P<year>\d{2,4})/"
    r"(?P<slot>[A-Za-z]*\d+)\.html?$",
    re.IGNORECASE,
)
_OLD_PASAXON_DATE_YMD = re.compile(
    r"/(?P<year>20\d{2})/(?P<month>\d{1,2})/(?P<day>\d{1,2})/"
    r"(?P<slot>[A-Za-z]*\d+)\.html?$",
    re.IGNORECASE,
)
_MODERN_PASAXON_DATE = re.compile(
    r"/(?P<year>20\d{2})/(?P<month>\d{1,2})/(?P<day>\d{1,2})/"
    r"(?P<slot>[A-Za-z]*\d+)\.html?$",
    re.IGNORECASE,
)
_MODERN_PASAXON_MONTH = re.compile(
    r"/(?P<year>20\d{2})/(?P<month>\d{1,2})/"
    r"(?P<slot>[A-Za-z]*\d+)\.html?$",
    re.IGNORECASE,
)
_PASAXON_PAGE_DATE = re.compile(
    r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](20\d{2})(?!\d)"
)


@dataclass(frozen=True, slots=True)
class WaybackCapture:
    timestamp: str
    original_url: str
    digest: str | None = None
    mimetype: str | None = None

    @property
    def replay_url(self) -> str:
        return f"{REPLAY_ROOT}/{self.timestamp}id_/{self.original_url}"


@dataclass(frozen=True, slots=True)
class OldKPLArticle:
    title: str
    body: str
    published_date: str
    slot: int
    section: str | None = None


@dataclass(frozen=True, slots=True)
class OldPasaxonLink:
    title: str
    original_url: str
    published_date: str | None
    slot: int


@dataclass(frozen=True, slots=True)
class OldPasaxonArticle:
    title: str
    body: str
    published_date: str
    slot: int


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag.lower() in {"p", "div", "tr", "td", "h1", "h2", "h3", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1
        if tag.lower() in {"p", "div", "tr", "td", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


class _AnchorParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.current_href: str | None = None
        self.current_text: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "a":
            self.current_href = dict(attrs).get("href")
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.current_href is not None:
            text = re.sub(r"\s+", " ", " ".join(self.current_text)).strip()
            self.anchors.append((urljoin(self.base_url, self.current_href), text))
            self.current_href = None
            self.current_text = []


class _PasaxonHeadlineParser(HTMLParser):
    """Recover the Lao headline from Word-exported historical pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_paragraph = False
        self.bold_depth = 0
        self.parts: list[str] = []
        self.headline: str | None = None
        self.candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        if lowered == "p":
            self.in_paragraph = True
            self.bold_depth = 0
            self.parts = []
        elif lowered in {"b", "strong"} and self.in_paragraph:
            self.bold_depth += 1
        elif lowered == "br" and self.in_paragraph and self.bold_depth:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"b", "strong"} and self.in_paragraph and self.bold_depth:
            self.bold_depth -= 1
        elif lowered == "p" and self.in_paragraph:
            candidate = re.sub(r"\s+", " ", "".join(self.parts)).strip()
            if len(re.findall(r"[\u0e80-\u0eff]", candidate)) >= 3:
                self.candidates.append(candidate)
                if self.headline is None:
                    self.headline = candidate
            self.in_paragraph = False
            self.bold_depth = 0
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.in_paragraph and self.bold_depth:
            self.parts.append(data)


def build_cdx_query(year: int, *, limit: int = 10000) -> str:
    pattern = f"kpl.net.la/english/news/newsrecord/{year}/*"
    params = (
        f"url={quote(pattern, safe='')}&output=json&filter=statuscode%3A200&"
        f"filter=mimetype%3Atext%2Fhtml&collapse=urlkey&"
        f"fl=timestamp%2Coriginal%2Cmimetype%2Cdigest&limit={limit}"
    )
    return f"{CDX_ROOT}?{params}"


def build_exact_cdx_query(url: str, year: int, *, limit: int = 10000) -> str:
    params = (
        f"url={quote(url, safe='')}&from={year}&to={year}&output=json&"
        f"filter=statuscode%3A200&filter=mimetype%3Atext%2Fhtml&collapse=digest&"
        f"fl=timestamp%2Coriginal%2Cmimetype%2Cdigest&limit={limit}"
    )
    return f"{CDX_ROOT}?{params}"


def build_pasaxon_cdx_query(year: int, *, limit: int = 10000) -> str:
    """Return a CDX query for unique HTML URLs on the historical Pasaxon host.

    Homepage-only indexes miss the newspaper's independently archived article
    pages, especially the ``conten`` and ``articles`` trees used in 2012.
    """

    pattern = "www.pasaxon.org.la/*"
    params = (
        f"url={quote(pattern, safe='')}&from={year}&to={year}&output=json&"
        f"filter=statuscode%3A200&filter=mimetype%3Atext%2Fhtml&collapse=urlkey&"
        f"fl=timestamp%2Coriginal%2Cmimetype%2Cdigest&limit={limit}"
    )
    return f"{CDX_ROOT}?{params}"


def build_pasaxon_path_cdx_query(
    path_prefix: str, *, year_from: int = 2012, year_to: int = 2020, limit: int = 10000
) -> str:
    """Build a smaller CDX query for one historical Pasaxon article tree."""

    clean = path_prefix.strip("/")
    pattern = f"www.pasaxon.org.la/{clean}*" if clean.endswith(".php") else f"www.pasaxon.org.la/{clean}/*"
    params = (
        f"url={quote(pattern, safe='')}&from={year_from}&to={year_to}&output=json&"
        f"filter=statuscode%3A200&filter=mimetype%3Atext%2Fhtml&collapse=urlkey&"
        f"fl=timestamp%2Coriginal%2Cmimetype%2Cdigest&limit={limit}"
    )
    return f"{CDX_ROOT}?{params}"


def fetch_cdx_year(year: int, *, timeout: float = 90.0) -> list[WaybackCapture]:
    request = Request(
        build_cdx_query(year),
        headers={"User-Agent": "laos-china-media-corpus/0.1 (+academic archive research)"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return parse_cdx(payload)


def fetch_exact_cdx(url: str, year: int, *, timeout: float = 90.0) -> list[WaybackCapture]:
    request = Request(
        build_exact_cdx_query(url, year),
        headers={"User-Agent": "laos-china-media-corpus/0.1 (+academic archive research)"},
    )
    with urlopen(request, timeout=timeout) as response:
        return parse_cdx(response.read())


def parse_cdx(payload: bytes | str | Iterable[list[str]]) -> list[WaybackCapture]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    rows = json.loads(payload) if isinstance(payload, str) else list(payload)
    if not rows:
        return []
    header = [str(value) for value in rows[0]]
    positions = {name: index for index, name in enumerate(header)}
    required = {"timestamp", "original"}
    if not required.issubset(positions):
        raise ValueError("Wayback CDX response lacks timestamp/original columns")
    captures: list[WaybackCapture] = []
    for row in rows[1:]:
        captures.append(
            WaybackCapture(
                timestamp=str(row[positions["timestamp"]]),
                original_url=str(row[positions["original"]]),
                digest=str(row[positions["digest"]]) if "digest" in positions else None,
                mimetype=str(row[positions["mimetype"]]) if "mimetype" in positions else None,
            )
        )
    return captures


def save_cdx(captures: Iterable[WaybackCapture], path: Path) -> int:
    rows = [
        ["timestamp", "original", "mimetype", "digest"],
        *[[item.timestamp, item.original_url, item.mimetype, item.digest] for item in captures],
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return len(rows) - 1


def load_cdx(path: Path) -> list[WaybackCapture]:
    return parse_cdx(path.read_text(encoding="utf-8-sig"))


def old_kpl_url_parts(url: str) -> tuple[str, int] | None:
    match = _OLD_KPL_DATE.search(url)
    if not match:
        return None
    try:
        published = date(
            int(match.group("year")), int(match.group("month_num")), int(match.group("day"))
        )
    except ValueError:
        return None
    slot_digits = re.sub(r"\D", "", match.group("slot"))
    return published.isoformat(), int(slot_digits)


def prioritized_captures(captures: Iterable[WaybackCapture]) -> dict[str, list[WaybackCapture]]:
    """Group by month and interleave dates so scanning does not overfit one day."""

    by_month_day: dict[str, dict[str, list[tuple[int, WaybackCapture]]]] = {}
    for capture in captures:
        parts = old_kpl_url_parts(capture.original_url)
        if not parts:
            continue
        published, slot = parts
        by_month_day.setdefault(published[:7], {}).setdefault(published, []).append((slot, capture))
    result: dict[str, list[WaybackCapture]] = {}
    for month, days in sorted(by_month_day.items()):
        for items in days.values():
            items.sort(key=lambda value: (value[0], value[1].original_url))
        ordered: list[WaybackCapture] = []
        depth = 0
        while True:
            added = False
            for day in sorted(days):
                if depth < len(days[day]):
                    ordered.append(days[day][depth][1])
                    added = True
            if not added:
                break
            depth += 1
        result[month] = ordered
    return result


def parse_old_kpl_article(payload: bytes | str, original_url: str) -> OldKPLArticle:
    parts = old_kpl_url_parts(original_url)
    if not parts:
        raise ValueError(f"Old KPL URL has no auditable publication date: {original_url}")
    published, slot = parts
    if isinstance(payload, bytes):
        for encoding in ("utf-8", "windows-1252", "windows-1253"):
            try:
                html = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            html = payload.decode("utf-8", errors="replace")
    else:
        html = payload
    parser = _VisibleText()
    parser.feed(html)
    lines = []
    for raw in "".join(parser.parts).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    noise_prefixes = (
        ":: KPL ::", "Copyright ©", "Khaosan Pathet Lao", "Back", "welcome to Lao news"
    )
    content = [line for line in lines if not line.startswith(noise_prefixes)]
    section_index = next(
        (i for i, line in enumerate(content) if line.casefold().endswith("news")), None
    )
    start = (section_index + 1) if section_index is not None else 0
    usable = content[start:]
    if len(usable) < 2:
        raise ValueError(f"Old KPL page has insufficient visible article text: {original_url}")
    title = usable[0]
    body_lines = usable[1:]
    while body_lines and body_lines[-1].startswith(("Copyright", "Khaosan Pathet Lao")):
        body_lines.pop()
    body = "\n\n".join(body_lines)
    if len(body) < 80:
        raise ValueError(f"Old KPL article body is too short: {original_url}")
    section = content[section_index] if section_index is not None else None
    return OldKPLArticle(title, body, published, slot, section)


def is_china_related(article: OldKPLArticle) -> tuple[bool, list[str]]:
    text = f"{article.title}\n{article.body}".casefold()
    terms = (
        "china", "chinese", "lao-china", "china-laos", "yunnan", "beijing",
        "guangxi", "kunming", "people's republic of china", "pr china",
        "xi jinping", "hu jintao", "wen jiabao",
    )
    hits = [term for term in terms if term in text]
    return bool(hits), hits


def old_pasaxon_url_parts(url: str) -> tuple[str, int] | None:
    # Pasaxon used both section/DD-MM-YY/slot.html (2012--2017) and
    # section/YYYY/MM/DD/slot.html (2018--2020).  Keep the publication date
    # tied to the URL path; a Common Crawl capture timestamp is not a
    # publication date.
    match = _OLD_PASAXON_DATE_YMD.search(url) or _OLD_PASAXON_DATE.search(url)
    if not match:
        return None
    year = int(match.group("year"))
    if year < 100:
        year += 2000
    try:
        published = date(year, int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None
    slot_digits = re.sub(r"\D", "", match.group("slot"))
    return published.isoformat(), int(slot_digits)


def pasaxon_url_identity(url: str) -> tuple[str | None, str | None, int] | None:
    """Return exact date/month hint and slot for all known Pasaxon generations."""

    old = old_pasaxon_url_parts(url)
    if old:
        return old[0], old[0][:7], old[1]
    match = _MODERN_PASAXON_DATE.search(url)
    if match:
        try:
            published = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            return None
        return published, published[:7], int(re.sub(r"\D", "", match.group("slot")))
    match = _MODERN_PASAXON_MONTH.search(url)
    if match:
        try:
            month_hint = date(
                int(match.group("year")), int(match.group("month")), 1
            ).strftime("%Y-%m")
        except ValueError:
            return None
        return None, month_hint, int(re.sub(r"\D", "", match.group("slot")))
    parsed_url = urlparse(url)
    php_id = parse_qs(parsed_url.query).get("p_id", [None])[0]
    if parsed_url.path.lower().endswith("pasaxon-detail.php") and php_id and str(php_id).isdigit():
        return None, None, int(str(php_id))
    return None


def parse_old_pasaxon_home(payload: bytes | str, base_url: str) -> list[OldPasaxonLink]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    parser = _AnchorParser(base_url)
    parser.feed(payload)
    links: list[OldPasaxonLink] = []
    seen: set[str] = set()
    for url, title in parser.anchors:
        parts = old_pasaxon_url_parts(url)
        parsed_url = urlparse(url)
        php_id = parse_qs(parsed_url.query).get("p_id", [None])[0]
        is_php_article = parsed_url.path.lower().endswith("pasaxon-detail.php") and bool(
            php_id and str(php_id).isdigit()
        )
        if (not parts and not is_php_article) or not title or url in seen:
            continue
        if parts:
            published, slot = parts
        else:
            published, slot = None, int(str(php_id))
        links.append(OldPasaxonLink(title, url, published, slot))
        seen.add(url)
    return links


def is_lao_china_title(title: str) -> tuple[bool, list[str]]:
    compact = re.sub(r"[\s\u200b-]+", "", title)
    variants = {
        "ຈີນ": "ຈີນ",
        "ສປຈີນ": "ສປ ຈີນ",
        "ລາວຈີນ": "ລາວ-ຈີນ",
        "ຈີນລາວ": "ຈີນ-ລາວ",
    }
    hits = [label for token, label in variants.items() if token in compact]
    return bool(hits), hits


def parse_old_pasaxon_article(
    payload: bytes | str, original_url: str, *, listing_title: str
) -> tuple[str, str, int]:
    parts = old_pasaxon_url_parts(original_url)
    parsed_url = urlparse(original_url)
    php_id = parse_qs(parsed_url.query).get("p_id", [None])[0]
    if parts:
        published, slot = parts
    elif parsed_url.path.lower().endswith("pasaxon-detail.php") and php_id and str(php_id).isdigit():
        published, slot = None, int(str(php_id))
    else:
        raise ValueError(f"Old Pasaxon URL has no supported article identity: {original_url}")
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    parser = _VisibleText()
    parser.feed(payload)
    lines = []
    for raw in "".join(parser.parts).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    noise = ("pasaxon", "Since 04 June", "Copyright ©", "ສຳນັກງານ", "Back")
    usable = [line for line in lines if not line.startswith(noise)]
    if published is None:
        date_match = _PASAXON_PAGE_DATE.search("\n".join(usable))
        if not date_match:
            raise ValueError(f"Pasaxon PHP article has no publication date: {original_url}")
        published = date(
            int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))
        ).isoformat()
    title_index = next((i for i, line in enumerate(usable) if line == listing_title), None)
    if title_index is None:
        title_index = next((i for i, line in enumerate(usable) if listing_title[:20] in line), None)
    body_lines = usable[(title_index + 1 if title_index is not None else 0):]
    body = "\n\n".join(body_lines)
    if len(body) < 60:
        raise ValueError(f"Old Pasaxon body is too short: {original_url}")
    return published, body, slot


def parse_direct_pasaxon_article(payload: bytes | str, original_url: str) -> OldPasaxonArticle:
    """Parse an archived article when no homepage/listing title is available."""

    identity = pasaxon_url_identity(original_url)
    if not identity:
        raise ValueError(f"Pasaxon URL has no supported article identity: {original_url}")
    published, month_hint, slot = identity
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    headline_parser = _PasaxonHeadlineParser()
    headline_parser.feed(payload)
    parser = _VisibleText()
    parser.feed(payload)
    lines: list[str] = []
    for raw in "".join(parser.parts).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    noise = ("pasaxon", "Since 04 June", "Copyright ©", "ສຳນັກງານ", "Back")
    usable = [line for line in lines if not line.startswith(noise)]
    if published is None:
        # Numeric PHP IDs are not chronological.  Only a complete date printed
        # within the archived article is acceptable publication evidence.
        date_match = _PASAXON_PAGE_DATE.search("\n".join(usable))
        if not date_match:
            raise ValueError(f"Pasaxon PHP article has no verifiable page date: {original_url}")
        try:
            published = date(
                int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))
            ).isoformat()
        except ValueError as exc:
            raise ValueError(f"Pasaxon PHP article has an invalid page date: {original_url}") from exc
        if month_hint and published[:7] != month_hint:
            raise ValueError(
                f"Pasaxon page date {published} conflicts with URL month {month_hint}: {original_url}"
            )
    if len(usable) < 2:
        raise ValueError(f"Old Pasaxon page has insufficient article text: {original_url}")
    compact_text = lambda value: re.sub(r"[\s\u200b]+", "", value)
    subscription_indices = [
        index for index, line in enumerate(usable)
        if "ສະໝັກສະມາຊິກ" in compact_text(line)
    ]
    if subscription_indices and subscription_indices[-1] + 1 < len(usable):
        article_start = subscription_indices[-1] + 1
        title = usable[article_start]
        body_start = article_start + 1
        if body_start < len(usable) and re.search(
            r"(?<!\d)\d{1,2}[./]\d{1,2}[./]20\d{2}(?:\s+\d{1,2}:\d{2})?(?!\d)",
            usable[body_start],
        ):
            body_start += 1
        footer = next(
            (index for index in range(body_start, len(usable))
             if usable[index].startswith(("ລິຂະສິດ", "Copyright"))),
            len(usable),
        )
        body_lines = usable[body_start:footer]
        headline_parser.headline = None
    else:
        # The 2018 static template omits the subscription marker but keeps the
        # article headline immediately before its displayed timestamp.  The
        # HTML ``title`` remains the newspaper name, so prefer that structural
        # boundary over the site-shell metadata.
        displayed_date = next(
            (
                index
                for index, line in enumerate(usable)
                if re.fullmatch(
                    r"\d{1,2}[./]\d{1,2}[./]20\d{2}(?:\s+\d{1,2}:\d{2})?",
                    line,
                )
            ),
            None,
        )
        if displayed_date is not None and displayed_date > 0:
            title = usable[displayed_date - 1]
            body_lines = usable[displayed_date + 1:]
            headline_parser.headline = None
        else:
            title = usable[0]
            body_lines = usable[1:]
    if headline_parser.headline:
        title = headline_parser.headline
        title_compact = re.sub(r"[\s\u200b]+", "", title)
        start = next(
            (index for index, line in enumerate(usable)
             if re.search(r"[\u0e80-\u0eff]", line)),
            0,
        )
        accumulated = ""
        consumed = start
        for index in range(start, min(len(usable), start + 12)):
            accumulated += re.sub(r"[\s\u200b]+", "", usable[index])
            consumed = index + 1
            if len(accumulated) >= len(title_compact):
                break
        body_lines = usable[consumed:]
    elif len(re.findall(r"[\u0e80-\u0eff]", title)) < 3:
        # A minority of pages omit the bold markup but still place a wrapped
        # Lao headline immediately before the lead.  Recover that prefix while
        # retaining the text exactly as archived (including legacy PUA glyphs).
        start = next(
            (index for index, line in enumerate(usable)
             if re.search(r"[\u0e80-\u0eff]", line)),
            None,
        )
        headline_lines: list[str] = []
        consumed = start or 0
        compact = ""
        for index in range(start or 0, min(len(usable), (start or 0) + 4)) if start is not None else ():
            line = usable[index]
            line_compact = re.sub(r"[\s\u200b]+", "", line)
            if headline_lines and (
                line.startswith(("\ufffd", "\u0ec3\u0e99"))
                or line[:1].isdigit()
                or (len(line_compact) >= 8 and line_compact[:8] in compact)
            ):
                break
            headline_lines.append(line)
            compact += line_compact
            consumed = index + 1
        if headline_lines:
            title = re.sub(r"\s+", " ", " ".join(headline_lines)).strip()
            body_lines = usable[consumed:]
    body = "\n\n".join(body_lines)
    if len(body) < 60:
        raise ValueError(f"Old Pasaxon body is too short: {original_url}")
    return OldPasaxonArticle(title, body, published, slot)


__all__ = [
    "OldKPLArticle", "OldPasaxonArticle", "OldPasaxonLink", "WaybackCapture", "build_cdx_query",
    "build_pasaxon_cdx_query", "build_pasaxon_path_cdx_query",
    "build_exact_cdx_query", "fetch_cdx_year", "fetch_exact_cdx",
    "is_china_related", "load_cdx", "old_kpl_url_parts", "parse_cdx",
    "parse_old_kpl_article", "prioritized_captures", "save_cdx",
    "is_lao_china_title", "old_pasaxon_url_parts", "parse_old_pasaxon_article",
    "parse_direct_pasaxon_article", "parse_old_pasaxon_home", "pasaxon_url_identity",
]
