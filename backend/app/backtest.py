from dataclasses import dataclass
from math import isfinite
from uuid import uuid4

import numpy as np

from .models import DecisionOutput


def assess_risk(output: DecisionOutput | None, *, max_exposure: float = 1.0) -> dict:
    if output is None:
        return {"status": "SKIPPED", "approved_weight": None, "reason_codes": ["INVALID_DECISION"]}
    weight = output.target_weight
    if isinstance(weight, bool) or not isfinite(weight) or weight not in (0, 0.5, 1) or weight > max_exposure:
        return {"status": "REJECTED", "approved_weight": None, "reason_codes": ["INVALID_TARGET"]}
    return {"status": "APPROVED", "approved_weight": weight, "reason_codes": []}


@dataclass
class Ledger:
    cash: float
    shares: float = 0

    def equity(self, price: float) -> float:
        return self.cash + self.shares * price

    def rebalance(self, target: float, opening: float, fee_bps: float, slippage_bps: float, session, decision_id: str) -> dict | None:
        if not all(isfinite(v) for v in (target, opening, fee_bps, slippage_bps, self.cash, self.shares)):
            raise ValueError("nonfinite ledger input")
        if isinstance(target, bool) or target not in (0, .5, 1) or opening <= 0 or not (0 <= fee_bps <= 100 and 0 <= slippage_bps <= 100):
            raise ValueError("invalid execution parameters")
        if self.cash < -1e-7 or self.shares < 0:
            raise ValueError("invalid ledger state")
        equity = self.equity(opening)
        delta = target * equity - self.shares * opening
        if abs(delta) < 1e-7:
            return None
        fee_rate, slip = fee_bps / 10000, slippage_bps / 10000
        if delta > 0:
            side = "BUY"
            price = opening * (1 + slip)
            cost_per_share = price * (1 + fee_rate) - opening
            quantity = min(delta / (opening + target * cost_per_share), self.cash / (price * (1 + fee_rate)))
            fee = quantity * price * fee_rate
            self.cash -= quantity * price + fee
            self.shares += quantity
        else:
            side = "SELL"
            price = opening * (1 - slip)
            cost_per_share = opening - price * (1 - fee_rate)
            quantity = min(-delta / (opening - target * cost_per_share), self.shares)
            fee = quantity * price * fee_rate
            self.cash += quantity * price - fee
            self.shares -= quantity
        if abs(self.cash) < 1e-7:
            self.cash = 0.0
        if abs(self.shares) < 1e-10:
            self.shares = 0.0
        if self.cash < 0 or self.shares < 0:
            raise ArithmeticError("negative cash or shares")
        return {"trade_id": str(uuid4()), "decision_id": decision_id, "execution_session": str(session),
                "side": side, "quantity": quantity, "reference_open": opening, "execution_price": price,
                "notional": quantity * price, "fee": fee, "slippage_cost": quantity * abs(price - opening),
                "cash_after": self.cash, "shares_after": self.shares,
                "weight_after": self.shares * opening / self.equity(opening)}


def performance(points: list[dict], trades: list[dict], initial: float) -> dict:
    if not points:
        raise ValueError("no valuation sessions")
    values = np.array([p["equity"] for p in points], dtype=float)
    returns = values / np.concatenate(([initial], values[:-1])) - 1
    peaks = np.maximum.accumulate(np.concatenate(([initial], values)))[1:]
    drawdowns = values / peaks - 1
    for point, drawdown in zip(points, drawdowns):
        point["drawdown"] = float(drawdown)
    total = values[-1] / initial - 1
    cagr = (values[-1] / initial) ** (252 / len(values)) - 1
    std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0
    downside = float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2)))
    max_dd = float(drawdowns.min())
    return {"total_return": float(total), "cagr": float(cagr),
            "sharpe_ratio": float(returns.mean() / std * np.sqrt(252)) if std > 1e-12 else None,
            "sortino_ratio": float(returns.mean() / downside * np.sqrt(252)) if downside > 1e-12 else None,
            "max_drawdown": max_dd, "calmar_ratio": float(cagr / abs(max_dd)) if max_dd < -1e-12 else None,
            "trade_count": len(trades), "turnover": sum(t["notional"] for t in trades) / float(values.mean()),
            "total_fees": sum(t["fee"] for t in trades), "slippage_cost": sum(t["slippage_cost"] for t in trades),
            "exposure_fraction": sum(p["shares"] > 0 for p in points) / len(points),
            "final_equity": float(values[-1]), "valuation_sessions": len(points)}
