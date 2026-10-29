"""Offline research planning contracts. No credentials or funding authority."""
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .data import canonical_hash
from .http_policy import HTTPTimeoutPolicy
from .models import Contract, ENGINE_VERSION, FEATURE_VERSION, PROMPT_VERSION

PLANNER_VERSION = "offline-study-plan-v1"
OpaqueID = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Money = Annotated[Decimal, Field(gt=0, le=100, max_digits=15, decimal_places=12)]
Rate = Annotated[Decimal, Field(gt=0, le=1000000, max_digits=19, decimal_places=12)]
NonnegativeRate = Annotated[Decimal, Field(ge=0, le=1000000, max_digits=19, decimal_places=12)]


class StudyRequest(Contract):
    profile_id: OpaqueID
    dataset_id: OpaqueID
    expected_dataset_hash: Digest
    start_date: date = date(2023, 1, 1)
    end_date: date = date(2023, 1, 31)
    initial_cash: Annotated[float, Field(strict=True, gt=0, le=1e12)] = 100000.0
    fee_bps: Annotated[float, Field(strict=True, ge=0, le=100)] = 5.0
    slippage_bps: Annotated[float, Field(strict=True, ge=0, le=100)] = 5.0
    max_exposure: Literal[1.0] = 1.0
    requested_budget_usd: Money
    requested_max_attempts: Annotated[int, Field(strict=True, ge=1, le=64)]
    study_protocol: Literal["acceptance-stage-a-v1", "acceptance-stage-b-v1"] | None = None
    overall_deadline_seconds: Annotated[float, Field(strict=True, gt=0, le=300)] | None = None
    transport_intent: Literal["fixture", "live"] = "fixture"

    @field_validator("max_exposure", mode="before")
    @classmethod
    def numeric_exposure(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 1.0:
            raise ValueError("numeric unit exposure required")
        return value

    @model_validator(mode="after")
    def short_period(self):
        if self.end_date < self.start_date or (self.end_date-self.start_date).days > 62 or self.end_date >= date(2025, 1, 1):
            raise ValueError("short pre-holdout period required")
        return self


class PrivateEvidenceRef(Contract):
    kind: Literal["pricing", "binding", "routing"]
    reference_id: OpaqueID
    sha256: Digest


class SourceContext(Contract):
    """Caller-injected source inventory and clock, never implicitly discovered."""
    source_commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    source_hash: Digest
    uv_lock_hash: Digest
    frontend_lock_hash: Digest
    dependency_versions: tuple[tuple[OpaqueID, str], ...]
    checked_at: datetime
    engine_version: str = ENGINE_VERSION
    feature_version: str = FEATURE_VERSION
    prompt_version: str = PROMPT_VERSION
    planner_version: Literal["offline-study-plan-v1"] = PLANNER_VERSION

    @field_validator("checked_at")
    @classmethod
    def aware_clock(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("explicit timezone-aware clock required")
        return value

    @field_validator("dependency_versions")
    @classmethod
    def versions(cls, value):
        names = [name for name, _ in value]
        if not value or names != sorted(set(names)) or any(not re.fullmatch(r"[A-Za-z0-9.+-]{1,60}", version) for _, version in value):
            raise ValueError("sorted unique dependency versions required")
        return value


class ResearchSettings(Contract):
    max_output_tokens: Annotated[int, Field(strict=True, ge=512, le=8192)]
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    temperature: Annotated[float, Field(strict=True, ge=0, le=2)] | None = None
    service_tier: Literal["auto", "default"] | None = None

    def provider_settings(self):
        result = {"max_output_tokens": self.max_output_tokens, "store": False}
        if self.reasoning_effort is not None:
            result["reasoning"] = {"effort": self.reasoning_effort}
        if self.temperature is not None:
            result["temperature"] = self.temperature
        if self.service_tier is not None:
            result["service_tier"] = self.service_tier
        return result


class ServerResearchProfile(Contract):
    """Allowlisted injection, not browser input or a key/config file loader.

    Rates must already be effective USD/M. Cache-write is additionally reserved;
    no native-unit conversion, discount or multiplier is performed here.
    """
    profile_id: OpaqueID
    revision: Annotated[int, Field(strict=True, ge=1)]
    provider: Literal["openai", "kuaiapi"]
    model_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,119}$")]
    identity_scope: Literal["direct-provider-label", "gateway-reported-label-only"]
    dataset_id: OpaqueID
    dataset_hash: Digest
    dataset_kind: Literal["synthetic", "historical"]
    allowed_start: date = date(2023, 1, 1)
    allowed_end: date = date(2023, 1, 31)
    max_budget_usd: Money
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=64)]
    attempts_per_role: Annotated[int, Field(strict=True, ge=1, le=2)]
    input_usd_per_million: Rate
    output_usd_per_million: Rate
    cache_read_usd_per_million: NonnegativeRate
    cache_write_usd_per_million: NonnegativeRate
    pricing_unit: Literal["effective-usd-per-million"] = "effective-usd-per-million"
    settings: ResearchSettings
    timeout_policy: HTTPTimeoutPolicy = Field(default_factory=HTTPTimeoutPolicy)
    valid_from: datetime
    expires_at: datetime
    expected_source_hash: Digest
    expected_uv_lock_hash: Digest
    expected_frontend_lock_hash: Digest
    private_evidence: tuple[PrivateEvidenceRef, ...]
    drift_policy: Literal["reject-and-refreeze-no-execution"] = "reject-and-refreeze-no-execution"

    @model_validator(mode="after")
    def coherent(self):
        if any(d.tzinfo is None or d.utcoffset() is None for d in (self.valid_from, self.expires_at)) or self.expires_at <= self.valid_from:
            raise ValueError("bounded aware profile validity required")
        if self.allowed_end < self.allowed_start or (self.allowed_end-self.allowed_start).days > 62 or self.allowed_end >= date(2025, 1, 1):
            raise ValueError("short pre-holdout profile period required")
        kinds = [ref.kind for ref in self.private_evidence]
        if len(set(kinds)) != len(kinds) or "pricing" not in kinds or (self.provider == "kuaiapi" and "binding" not in kinds):
            raise ValueError("unique private pricing/binding evidence required")
        expected = "gateway-reported-label-only" if self.provider == "kuaiapi" else "direct-provider-label"
        if self.identity_scope != expected:
            raise ValueError("provider identity scope mismatch")
        return self


class PlannedSession(Contract):
    decision: date
    execution: date
    warmup_bars: int
    numeric_input_hash: Digest


class PlanEstimate(Contract):
    single_nominal_calls: int
    multi_nominal_calls: int
    total_nominal_calls: int
    complete_attempt_ceiling: int
    capped_attempt_ceiling: int
    separate_smoke_calls: Literal[0] = 0
    baseline_calls: Literal[0] = 0
    baseline_cost_usd: Literal[0] = 0
    input_token_bound: Literal[32768] = 32768
    output_token_bound: int
    reservation_usd_per_attempt: Decimal
    nominal_reservation_usd: Decimal
    complete_reservation_usd: Decimal
    capped_reservation_usd: Decimal
    all_attempts_fit_requested_caps: bool


class SafePlanView(Contract):
    manifest_hash: Digest
    profile_id: OpaqueID
    provider: Literal["openai", "kuaiapi"]
    model_id: str
    identity_scope: str
    dataset_id: OpaqueID
    dataset_hash: Digest
    dataset_kind: Literal["synthetic", "historical"]
    start_date: date
    end_date: date
    initial_cash: float
    fee_bps: float
    slippage_bps: float
    max_exposure: float
    return_basis: Literal["price-only"] = "price-only"
    calendar: Literal["XNYS"] = "XNYS"
    strategy_ids: tuple[Literal["buy_hold", "ma_crossover", "single_agent", "multi_agent"], ...]
    study_protocol: Literal["acceptance-stage-a-v1", "acceptance-stage-b-v1"] | None = None
    overall_deadline_seconds: float | None = None
    transport_intent: Literal["fixture", "live"] | None = None
    engine_version: str
    feature_version: str
    prompt_version: str
    planner_version: str
    attempts_per_role: int
    sessions: tuple[PlannedSession, ...]
    estimate: PlanEstimate
    requested_budget_usd: Decimal
    requested_max_attempts: int
    settings: ResearchSettings
    expires_at: datetime
    warnings: tuple[str, ...]
    execution_authorized: Literal[False] = False


class FrozenStudyPlan(Contract):
    """Immutable JSON string prevents nested dict mutation behind the hash."""
    canonical_payload: str
    manifest_hash: Digest

    @model_validator(mode="after")
    def intact(self):
        value = json.loads(self.canonical_payload)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if canonical != self.canonical_payload or canonical_hash(value) != self.manifest_hash:
            raise ValueError("study plan integrity mismatch")
        return self

    @property
    def payload(self):
        return json.loads(self.canonical_payload)  # A detached copy for private evidence.
