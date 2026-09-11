"""Bounded Common Crawl recovery for exact-dated, non-PHP Pasaxon pages in 2013/2020."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .commoncrawl_pasaxon_round7 import run as run_generic


STAGING_NAME = "commoncrawl_pasaxon_round8"
PRIOR_STAGING = (
    "commoncrawl_pasaxon_round5",
    "commoncrawl_pasaxon_round6",
    "commoncrawl_pasaxon_round7",
    STAGING_NAME,
)
BASE_EXCLUDED = {
    "CC-MAIN-2017-51", "CC-MAIN-2018-51", "CC-MAIN-2019-51", "CC-MAIN-2020-50",
}


def run(
    *, root: Path, max_index_requests: int = 10, max_range_requests: int = 90,
    interval: float = 1.0,
) -> dict[str, object]:
    return run_generic(
        root=root, max_index_requests=max_index_requests,
        max_range_requests=max_range_requests, interval=interval,
        staging_name=STAGING_NAME, allowed_years=(2013, 2020),
        prior_staging=PRIOR_STAGING, base_excluded=BASE_EXCLUDED,
        non_php_only=True,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-index-requests", type=int, default=10)
    parser.add_argument("--max-range-requests", type=int, default=90)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    print(json.dumps(run(
        root=args.root.resolve(), max_index_requests=args.max_index_requests,
        max_range_requests=args.max_range_requests, interval=args.interval,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
