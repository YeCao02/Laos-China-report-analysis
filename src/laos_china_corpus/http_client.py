from __future__ import annotations

import gzip
import json
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import MIN_FREE_BYTES, ProjectPaths
from .normalize import sha256_bytes


class FetchBlocked(RuntimeError):
    """Raised when an access-control response requires stopping the source."""


class StorageSafetyError(RuntimeError):
    """Raised before a fetch when the configured free-space floor is crossed."""


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    payload: bytes
    fetched_at: str
    content_sha256: str
    raw_file: Path
    metadata_file: Path
    from_cache: bool = False


class RateLimitedFetcher:
    def __init__(
        self,
        paths: ProjectPaths,
        *,
        requests_per_second: float = 1.0,
        timeout_seconds: int = 45,
        max_retries: int = 4,
        user_agent: str = "academic-research/1.0 (local corpus; respectful crawler)",
        min_free_bytes: int = MIN_FREE_BYTES,
    ) -> None:
        self.paths = paths
        self.delay = 1.0 / max(requests_per_second, 0.01)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.min_free_bytes = min_free_bytes
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _wait(self) -> None:
        with self._lock:
            remaining = self.delay - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()

    def _check_storage(self) -> None:
        free = shutil.disk_usage(self.paths.root).free
        if free < self.min_free_bytes:
            raise StorageSafetyError(
                f"Free space {free} bytes is below safety floor {self.min_free_bytes} bytes"
            )

    def fetch(self, url: str, source_code: str, *, force: bool = False) -> FetchResult:
        self._check_storage()
        day = datetime.now(timezone.utc).date().isoformat()
        digest = sha256(url.encode("utf-8")).hexdigest()
        raw_dir = self.paths.raw / source_code / day
        raw_dir.mkdir(parents=True, exist_ok=True)
        metadata_file = raw_dir / f"{digest}.json"
        if metadata_file.exists() and not force:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            raw_file = raw_dir / metadata["raw_filename"]
            if raw_file.suffix == ".gz":
                payload = gzip.decompress(raw_file.read_bytes())
            else:
                payload = raw_file.read_bytes()
            return FetchResult(
                url=url,
                status=metadata["status"],
                content_type=metadata["content_type"],
                payload=payload,
                fetched_at=metadata["fetched_at"],
                content_sha256=metadata["content_sha256"],
                raw_file=raw_file,
                metadata_file=metadata_file,
                from_cache=True,
            )

        retryable = {408, 429, 500, 502, 503, 504}
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._wait()
            request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read()
                    status = int(response.status)
                    content_type = response.headers.get_content_type()
                break
            except HTTPError as exc:
                last_error = exc
                if exc.code in {401, 403}:
                    raise FetchBlocked(f"Access blocked for {url}: HTTP {exc.code}") from exc
                if exc.code not in retryable or attempt == self.max_retries:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else min(30, 2**attempt))
            except URLError as exc:
                last_error = exc
                if attempt == self.max_retries:
                    raise
                time.sleep(min(30, 2**attempt))
        else:
            raise RuntimeError(f"Fetch failed for {url}: {last_error}")

        lower = payload[:8000].decode("utf-8", errors="ignore").casefold()
        if any(marker in lower for marker in ("captcha", "cloudflare", "verify you are human")):
            raise FetchBlocked(f"Challenge page detected for {url}")

        fetched_at = datetime.now(timezone.utc).isoformat()
        content_hash = sha256_bytes(payload)
        if content_type == "application/pdf" or payload.startswith(b"%PDF"):
            raw_file = raw_dir / f"{digest}.pdf"
            raw_file.write_bytes(payload)
        else:
            raw_file = raw_dir / f"{digest}.html.gz"
            raw_file.write_bytes(gzip.compress(payload, compresslevel=6))
        metadata = {
            "request_url": url,
            "status": status,
            "content_type": content_type,
            "fetched_at": fetched_at,
            "content_sha256": content_hash,
            "raw_filename": raw_file.name,
        }
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return FetchResult(
            url=url,
            status=status,
            content_type=content_type,
            payload=payload,
            fetched_at=fetched_at,
            content_sha256=content_hash,
            raw_file=raw_file,
            metadata_file=metadata_file,
        )

