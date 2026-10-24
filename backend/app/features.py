from uuid import NAMESPACE_URL, uuid5

import numpy as np
import pandas as pd

from .data import Dataset, get_calendar
from .models import FEATURE_VERSION, FeatureSnapshot


def wilder(values: np.ndarray, period: int, first: int = 0) -> np.ndarray:
    result = np.full(len(values), np.nan)
    seed = first + period - 1
    if len(values) <= seed:
        return result
    result[seed] = np.mean(values[first:seed + 1])
    for i in range(seed + 1, len(values)):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def feature_frame(dataset: Dataset) -> pd.DataFrame:
    frame = pd.DataFrame([b.model_dump() for b in dataset.bars])
    close = frame.close
    changes = close.diff().to_numpy()
    avg_gain = wilder(np.maximum(changes, 0), 14, 1)
    avg_loss = wilder(np.maximum(-changes, 0), 14, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    rsi[(avg_gain == 0) & (avg_loss == 0)] = 50
    previous_close = close.shift(1)
    tr = pd.concat([frame.high - frame.low, (frame.high - previous_close).abs(), (frame.low - previous_close).abs()], axis=1).max(axis=1)
    frame["last_close"] = close
    frame["ma20"] = close.rolling(20).mean()
    frame["ma60"] = close.rolling(60).mean()
    frame["rsi14"] = rsi
    frame["atr14"] = wilder(tr.to_numpy(), 14)
    frame["atr_pct14"] = frame.atr14 / close
    frame["realized_vol20"] = np.log(close).diff().rolling(20).std(ddof=1) * np.sqrt(252)
    frame["return_5d"] = close / close.shift(5) - 1
    frame["volume_ratio20"] = frame.volume / frame.volume.rolling(20).mean()
    return frame


def snapshots(dataset: Dataset) -> dict:
    frame = feature_frame(dataset)
    cal = get_calendar(dataset.bars[0].session_date, dataset.bars[-1].session_date)
    result = {}
    fields = FeatureSnapshot.model_fields.keys() - {"snapshot_id", "symbol", "decision_session", "as_of", "dataset_hash", "feature_version"}
    for _, row in frame.iterrows():
        values = {name: float(row[name]) for name in fields}
        if not all(np.isfinite(value) for value in values.values()):
            continue
        day = row.session_date
        result[day] = FeatureSnapshot(
            snapshot_id=uuid5(NAMESPACE_URL, f"{dataset.hash}:{FEATURE_VERSION}:{day}"),
            decision_session=day, as_of=cal.session_close(str(day)).to_pydatetime(),
            dataset_hash=dataset.hash, **values,
        )
    return result
