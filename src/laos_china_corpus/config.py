from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

SNAPSHOT_START = date(2012, 1, 1)
SNAPSHOT_END = date(2026, 8, 7)
DEFAULT_TARGET_PER_SOURCE_MONTH = 4
MINIMUM_PER_SOURCE_MONTH = 2
MAXIMUM_PER_SOURCE_MONTH = 10
MIN_FREE_BYTES = 100 * 1024**3

LAO_DIRECT_QUERIES = (
    "ຈີນ",
    "ສປ ຈີນ",
    "ສປຈີນ",
    "ລາວ-ຈີນ",
    "ຈີນ-ລາວ",
    "ລາວ ຈີນ",
    "ຈີນ ລາວ",
)
EN_DIRECT_QUERIES = (
    "China",
    "Chinese",
    "Lao-China",
    "Laos-China",
    "China-Laos",
)
ENTITY_QUERIES = (
    "Xi Jinping",
    "Laos-China Railway",
    "Boten",
    "Yunnan",
    "Belt and Road",
    "Lancang-Mekong",
    "Chinese investment",
    "Chinese company",
)


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def database(self) -> Path:
        return self.data / "corpus.sqlite3"

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def records(self) -> Path:
        return self.data / "records"

    @property
    def text(self) -> Path:
        return self.data / "text"

    @property
    def catalog(self) -> Path:
        return self.data / "catalog"

    @property
    def audit(self) -> Path:
        return self.data / "audit"

    @property
    def staging(self) -> Path:
        return self.data / "staging"

    def ensure(self) -> None:
        for path in (
            self.data,
            self.raw,
            self.records,
            self.text,
            self.catalog,
            self.audit,
            self.staging,
        ):
            path.mkdir(parents=True, exist_ok=True)

