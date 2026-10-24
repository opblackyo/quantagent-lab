import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import exchange_calendars as xc

from .models import MarketBar

ROOT = Path(__file__).resolve().parents[2]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


@lru_cache(maxsize=16)
def calendar(start: str, end: str):
    return xc.get_calendar("XNYS", start=start, end=end)


def get_calendar(start: date, end: date):
    return calendar(str(start - timedelta(days=10)), str(end + timedelta(days=10)))


@dataclass(frozen=True)
class Dataset:
    metadata: dict
    bars: tuple[MarketBar, ...]

    @property
    def hash(self) -> str:
        return self.metadata["sha256"]


class DatasetRegistry:
    def __init__(self, directories: list[Path] | None = None):
        self.directories = directories or [ROOT / "data/sample", ROOT / "data/cache"]

    def entries(self) -> list[tuple[Path, dict]]:
        entries = []
        ids = set()
        for directory in self.directories:
            for path in sorted(directory.glob("*.metadata.json")):
                meta = json.loads(path.read_text(encoding="utf-8"))
                if meta["id"] in ids:
                    raise ValueError("duplicate dataset id")
                ids.add(meta["id"])
                entries.append((path, meta))
        return entries

    def list(self) -> list[dict]:
        return [meta for _, meta in self.entries()]

    def load(self, dataset_id: str) -> Dataset:
        for path, meta in self.entries():
            if meta["id"] == dataset_id:
                name = meta["file"]
                if Path(name).name != name or not name.endswith(".csv"):
                    raise ValueError("dataset file must be a local CSV basename")
                if meta.get("adjustment_mode") != "raw" or meta.get("symbol") != "SPY" or meta.get("currency") != "USD":
                    raise ValueError("MVP requires raw SPY USD bars")
                if meta.get("kind") not in ("synthetic", "historical"):
                    raise ValueError("dataset kind must be explicit")
                content = (path.parent / name).read_bytes()
                if sha256(content) != meta["sha256"]:
                    raise ValueError("dataset SHA-256 mismatch")
                bars = parse_csv(content)
                if len(bars) != meta["rows"] or str(bars[0].session_date) != meta["start_date"] or str(bars[-1].session_date) != meta["end_date"]:
                    raise ValueError("dataset metadata does not match CSV")
                return Dataset(meta, bars)
        raise ValueError("unknown dataset id")


def parse_csv(content: bytes) -> tuple[MarketBar, ...]:
    reader = csv.DictReader(content.decode("utf-8-sig").splitlines())
    expected = {"session_date", "open", "high", "low", "close", "volume"}
    if set(reader.fieldnames or []) != expected:
        raise ValueError("CSV requires exactly session_date, open, high, low, close, volume")
    bars = tuple(MarketBar.model_validate(row) for row in reader)
    if not bars:
        raise ValueError("empty dataset")
    dates = [bar.session_date for bar in bars]
    if dates != sorted(set(dates)):
        raise ValueError("dates must be unique and ascending")
    cal = get_calendar(dates[0], dates[-1])
    expected_dates = [stamp.date() for stamp in cal.sessions_in_range(str(dates[0]), str(dates[-1]))]
    if dates != expected_dates:
        raise ValueError("missing or non-trading market session")
    return bars
