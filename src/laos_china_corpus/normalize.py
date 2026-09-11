from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")
WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = html.unescape(value)
    value = unicodedata.normalize("NFC", value)
    value = ZERO_WIDTH.sub("", value)
    return WHITESPACE.sub(" ", value).strip()


def normalize_for_match(value: str | None) -> str:
    value = normalize_text(value) or ""
    value = value.casefold()
    return re.sub(r"[^\w\u0e80-\u0eff]+", "", value)


def canonicalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in {"utm_source", "utm_medium", "utm_campaign", "fbclid", "gclid"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def iso_from_dmy(value: str | None) -> str | None:
    if not value:
        return None
    value = normalize_text(value)
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).isoformat(timespec="minutes")
        except ValueError:
            pass
    return None

