from datetime import date
from dataclasses import replace

import numpy as np
import pytest
from pydantic import ValidationError

from app.backtest import Ledger, assess_risk, performance
from app.data import Dataset, DatasetRegistry, parse_csv
from app.features import feature_frame, snapshots, wilder
from app.models import DecisionOutput, MarketBar


@pytest.fixture(scope="session")
def dataset():
    return DatasetRegistry().load("synthetic-spy-v1")


def test_csv_rejects_duplicates_gaps_and_invalid_prices():
    header = "session_date,open,high,low,close,volume\n"
    rows = ["2024-01-02,100,102,99,101,100", "2024-01-03,101,103,100,102,100", "2024-01-04,102,104,101,103,100"]
    assert len(parse_csv((header + "\n".join(rows)).encode())) == 3
    for invalid in ([rows[0], rows[0]], [rows[0], rows[2]], list(reversed(rows)), [rows[0].replace(",100,102,", ",nan,102,")]):
        with pytest.raises(ValueError):
            parse_csv((header + "\n".join(invalid)).encode())
    with pytest.raises(ValidationError):
        MarketBar(session_date="2024-01-02", open=100, high=99, low=90, close=100, volume=1)


def test_wilder_hand_calculated_seed():
    result = wilder(np.array([2., 4., 6., 8., 10.]), 3)
    assert np.isnan(result[:2]).all()
    assert result[2] == 4
    assert result[3] == pytest.approx(16 / 3)
    assert result[4] == pytest.approx((16 / 3 * 2 + 10) / 3)


def test_features_flat_market_and_warmup(dataset):
    bars = tuple(b.model_copy(update={"open": 100., "high": 102., "low": 98., "close": 100., "volume": 100}) for b in dataset.bars[:65])
    flat = Dataset(dataset.metadata, bars)
    frame = feature_frame(flat)
    assert frame.loc[14, "rsi14"] == 50
    assert frame.loc[13, "atr14"] == 4
    assert frame.loc[59, "ma60"] == 100
    assert len(snapshots(flat)) == 6


def test_future_changes_cannot_change_prior_numeric_features(dataset):
    original = Dataset(dataset.metadata, dataset.bars[:90])
    changed = replace(original, bars=original.bars[:70] + tuple(b.model_copy(update={"open": b.open*2, "high": b.high*2, "low": b.low*2, "close": b.close*2}) for b in original.bars[70:]))
    left, right = snapshots(original), snapshots(changed)
    for day in list(left)[:11]:
        assert left[day].numeric_input() == right[day].numeric_input()


@pytest.mark.parametrize("weight", [0, .5, 1])
def test_buy_and_sell_target_after_actual_costs(weight):
    ledger = Ledger(100000)
    trade = ledger.rebalance(weight, 100, 5, 5, date(2024, 1, 8), "d1")
    if weight == 0:
        assert trade is None
        return
    assert trade["weight_after"] == pytest.approx(weight)
    assert ledger.cash >= 0
    before = ledger.equity(110)
    sale = ledger.rebalance(0, 110, 5, 5, date(2024, 1, 9), "d2")
    assert sale["execution_price"] < 110
    assert ledger.shares == 0
    assert ledger.cash == pytest.approx(before - sale["fee"] - sale["slippage_cost"])


def test_half_weight_rebalance_from_fully_invested():
    ledger = Ledger(0, 1000)
    trade = ledger.rebalance(.5, 100, 5, 5, date(2024, 1, 8), "d")
    assert trade["side"] == "SELL"
    assert trade["weight_after"] == pytest.approx(.5)


def test_fixed_trade_cost_accounting():
    ledger = Ledger(100000)
    buy = ledger.rebalance(1, 100, 5, 5, date(2024, 1, 8), "d1")
    sell = ledger.rebalance(0, 105, 5, 5, date(2024, 1, 9), "d2")
    gross = buy["quantity"] * (105 - 100)
    cost = sum(t["fee"] + t["slippage_cost"] for t in [buy, sell])
    assert ledger.cash - 100000 == pytest.approx(gross - cost)


@pytest.mark.parametrize("weight", [True, "1", -.5, .25, float("nan"), float("inf")])
def test_invalid_targets_are_not_decisions(weight):
    with pytest.raises(ValidationError):
        DecisionOutput(target_weight=weight, confidence=.5, rationale="test", evidence=["ma20"])
    assert assess_risk(None)["approved_weight"] is None


def test_performance_includes_initial_equity_and_degenerate_cases():
    points = [{"equity": 90., "shares": 0}, {"equity": 95., "shares": 0}]
    metrics = performance(points, [], 100)
    assert metrics["total_return"] == pytest.approx(-.05)
    assert metrics["max_drawdown"] == pytest.approx(-.1)
    flat = performance([{"equity": 100., "shares": 0} for _ in range(3)], [], 100)
    assert flat["sharpe_ratio"] is None
    assert flat["calmar_ratio"] is None


def test_ledger_conserves_equity_across_many_rebalances():
    rng = np.random.default_rng(42)
    ledger = Ledger(100000)
    for _ in range(500):
        price = float(rng.uniform(50, 500))
        weight = float(rng.choice([0, .5, 1]))
        before = ledger.equity(price)
        trade = ledger.rebalance(weight, price, 5, 5, date(2024, 1, 8), "d")
        assert ledger.cash >= 0 and ledger.shares >= 0
        if trade:
            assert ledger.equity(price) == pytest.approx(before - trade["fee"] - trade["slippage_cost"])
            assert trade["weight_after"] == pytest.approx(weight, abs=1e-10)


def test_rsi_reference_seed_and_atr_gap(dataset):
    closes = [44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28]
    bars = tuple(b.model_copy(update={"open": c, "high": c+1, "low": c-1, "close": c}) for b,c in zip(dataset.bars, closes))
    frame = feature_frame(Dataset(dataset.metadata, bars))
    assert frame.loc[14,"rsi14"] == pytest.approx(70.46413502109705)
    flat = [b.model_copy(update={"open":100.,"high":102.,"low":98.,"close":100.}) for b in dataset.bars[:15]]
    flat[-1] = flat[-1].model_copy(update={"open":110.,"high":112.,"low":109.,"close":111.})
    assert feature_frame(Dataset(dataset.metadata, tuple(flat))).loc[14,"atr14"] == pytest.approx((4*13+12)/14)
