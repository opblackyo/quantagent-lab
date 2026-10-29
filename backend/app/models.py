from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FEATURE_VERSION = "wilder-v1"
ENGINE_VERSION = "next-open-v1"
PROMPT_VERSION = "numeric-only-v1"
FeatureName = Literal["last_close", "ma20", "ma60", "rsi14", "atr14", "atr_pct14", "realized_vol20", "return_5d", "volume_ratio20"]
FEATURE_NAMES = tuple(FeatureName.__args__)
Weight = Literal[0.0, 0.5, 1.0]
RiskCode = Literal["VOLATILITY_HIGH", "TREND_CONFLICT", "WEAK_EVIDENCE", "NO_OBJECTION"]
Strategy = Literal["buy_hold", "ma_crossover", "single_agent", "multi_agent"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True, frozen=True)


class MarketBar(Contract):
    session_date: date
    open: Annotated[float, Field(gt=0)]
    high: Annotated[float, Field(gt=0)]
    low: Annotated[float, Field(gt=0)]
    close: Annotated[float, Field(gt=0)]
    volume: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def ohlc(self):
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("invalid OHLC range")
        return self


class FeatureSnapshot(Contract):
    snapshot_id: UUID
    symbol: Literal["SPY"] = "SPY"
    decision_session: date
    as_of: datetime
    last_close: float
    ma20: float
    ma60: float
    rsi14: Annotated[float, Field(ge=0, le=100)]
    atr14: Annotated[float, Field(ge=0)]
    atr_pct14: Annotated[float, Field(ge=0)]
    realized_vol20: Annotated[float, Field(ge=0)]
    return_5d: float
    volume_ratio20: Annotated[float, Field(ge=0)]
    dataset_hash: str
    feature_version: str = FEATURE_VERSION

    def numeric_input(self) -> dict:
        return {key: getattr(self, key) for key in FEATURE_NAMES}


class AnalysisOutput(Contract):
    regime: Literal["bullish", "bearish", "sideways"]
    thesis: Annotated[str, Field(min_length=1, max_length=500)]
    evidence: tuple[FeatureName, ...]


class CriticOutput(Contract):
    risk_level: Literal["low", "medium", "high"]
    risk_flags: tuple[RiskCode, ...]
    counterarguments: Annotated[tuple[Annotated[str, Field(max_length=500)], ...], Field(max_length=3)]

    @model_validator(mode="after")
    def exclusive_flag(self):
        if "NO_OBJECTION" in self.risk_flags and len(self.risk_flags) > 1:
            raise ValueError("NO_OBJECTION cannot accompany a risk flag")
        return self


class DecisionOutput(Contract):
    target_weight: Weight
    confidence: Annotated[float, Field(strict=True, ge=0, le=1)]
    rationale: Annotated[str, Field(min_length=1, max_length=500)]
    evidence: tuple[FeatureName, ...]
    acknowledged_risks: tuple[RiskCode, ...] = ()

    @field_validator("target_weight", mode="before")
    @classmethod
    def numeric_weight(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("weight must be a numeric 0, 0.5, or 1")
        return value


class ExperimentConfig(Contract):
    strategy_id: Strategy = "multi_agent"
    dataset_id: str = "synthetic-spy-v1"
    symbol: Literal["SPY"] = "SPY"
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2024, 12, 31)
    initial_cash: Annotated[float, Field(strict=True, gt=0, le=1e12)] = 100000
    fee_bps: Annotated[float, Field(strict=True, ge=0, le=100)] = 5
    slippage_bps: Annotated[float, Field(strict=True, ge=0, le=100)] = 5
    max_exposure: Literal[1.0] = 1.0
    llm_mode: Literal["mock", "api", "replay"] = "mock"
    model_provider: Literal["openai", "kuaiapi"] | None = None
    model_id: str | None = None
    prompt_version: Literal["numeric-only-v1"] = PROMPT_VERSION
    dataset_hash: str | None = None
    replay_source_run_id: UUID | None = None
    max_llm_calls: Annotated[int, Field(strict=True, ge=0, le=100000)] = 2000
    max_llm_budget_usd: Annotated[float, Field(strict=True, ge=0, le=1000)] = 2
    max_attempts_per_role: Annotated[int, Field(strict=True, ge=1, le=2)] = 2
    research_group_id: str | None = None

    @model_validator(mode="after")
    def range_and_replay(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.llm_mode == "replay" and not self.replay_source_run_id:
            raise ValueError("Replay requires a source run")
        if self.llm_mode != "replay" and self.replay_source_run_id:
            raise ValueError("source run is only valid in Replay mode")
        return self
