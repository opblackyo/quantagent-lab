"""Generate a reproducible, explicitly synthetic offline fixture."""
import csv
import hashlib
import json
from pathlib import Path

import exchange_calendars as xc
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def generate():
    directory = ROOT / "data/sample"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "synthetic-spy-v1.csv"
    sessions = xc.get_calendar("XNYS", start="2018-01-01", end="2025-12-31").sessions
    rng = np.random.default_rng(20261024)
    previous = 275.0
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["session_date", "open", "high", "low", "close", "volume"])
        for i, stamp in enumerate(sessions):
            regime = .0004 + .0012 * np.sin(i / 85)
            opening = previous * np.exp(rng.normal(0, .003))
            close = opening * np.exp(regime + rng.normal(0, .009))
            high = max(opening, close) * (1 + rng.uniform(.001, .012))
            low = min(opening, close) * (1 - rng.uniform(.001, .012))
            writer.writerow([str(stamp.date()), *[f"{v:.6f}" for v in (opening, high, low, close)], int(rng.uniform(3e7, 12e7))])
            previous = close
    meta = {"id": "synthetic-spy-v1", "file": path.name, "kind": "synthetic", "symbol": "SPY",
            "source": "Deterministic synthetic fixture; NOT historical SPY market prices",
            "acquired_at": "2026-10-24T00:00:00Z", "currency": "USD", "frequency": "1d", "adjustment_mode": "raw",
            "start_date": str(sessions[0].date()), "end_date": str(sessions[-1].date()), "rows": len(sessions),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "redistributable": True,
            "return_basis": "price-only", "seed": 20261024}
    path.with_suffix(".metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    generate()
