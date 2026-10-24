"""Download raw SPY daily bars to a private local cache for personal research."""
import argparse
import csv
import hashlib
import io
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.data import parse_csv


def save_dataset(content: bytes, dataset_id: str, source: str, *, provider_parameters=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", dataset_id):
        raise ValueError("dataset id must contain lowercase letters, digits and hyphens")
    bars = parse_csv(content)
    directory = ROOT / "data/cache"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{dataset_id}.csv"
    metadata = path.with_suffix(".metadata.json")
    digest = hashlib.sha256(content).hexdigest()
    if path.exists() or metadata.exists():
        raise ValueError("dataset id already exists; use a new versioned id to preserve immutability")
    meta = {"id": dataset_id, "file": path.name, "kind": "historical", "symbol": "SPY", "source": source,
            "acquired_at": datetime.now(timezone.utc).isoformat(), "currency": "USD", "frequency": "1d", "adjustment_mode": "raw",
            "start_date": str(bars[0].session_date), "end_date": str(bars[-1].session_date), "rows": len(bars),
            "sha256": digest, "redistributable": False, "return_basis": "price-only", "provider_parameters": provider_parameters or {}}
    path.write_bytes(content)
    metadata.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", default="spy-yahoo-2018-2025-raw-v1")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 1, 1), help="exclusive")
    parser.add_argument("--csv", type=Path, help="import an existing canonical raw SPY CSV instead of downloading")
    parser.add_argument("--source", help="required provenance for an imported CSV")
    args = parser.parse_args()
    if args.csv:
        if not args.source:
            parser.error("--csv requires --source and an explicit raw-price basis")
        save_dataset(args.csv.read_bytes(), args.id, args.source)
        return
    import yfinance as yf
    parameters = {"start": str(args.start), "end": str(args.end), "interval": "1d", "auto_adjust": False,
                  "back_adjust": False, "actions": True, "repair": False}
    frame = yf.Ticker("SPY").history(**parameters)
    if frame.empty:
        raise ValueError("provider returned no data")
    if "Stock Splits" in frame and (frame["Stock Splits"] != 0).any():
        raise ValueError("split detected; raw-price ledger requires an explicit corporate-action implementation")
    content = io.StringIO(newline="")
    writer = csv.writer(content, lineterminator="\n")
    writer.writerow(["session_date", "open", "high", "low", "close", "volume"])
    for stamp, row in frame.iterrows():
        writer.writerow([str(stamp.date()), *[format(float(row[name]), ".12g") for name in ("Open", "High", "Low", "Close")], int(row.Volume)])
    save_dataset(content.getvalue().encode(), args.id, "Yahoo Finance via yfinance " + yf.__version__ + "; private personal research cache", provider_parameters=parameters)


if __name__ == "__main__":
    main()
