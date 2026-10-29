"""Offline planner plus a default-disabled study worker.

Importing this module does not construct a provider, budget, store or socket.
plan_study remains a pure preview. execute_study sends only when the caller
passes service_enabled=True and an injected fixture client. The default application leaves that path disabled.
"""
import csv
import json
from decimal import Decimal, localcontext
from typing import Mapping

from .agent_contracts import INSTRUCTIONS, SCHEMAS, strict_schema
from .data import Dataset, DatasetRegistry, canonical_hash, schedule
from .features import snapshots
from .models import ENGINE_VERSION, FEATURE_VERSION, PROMPT_VERSION, ExperimentConfig
from .research_contracts import (
    FrozenStudyPlan, PlanEstimate, PlannedSession, SafePlanView,
    ServerResearchProfile, SourceContext, StudyRequest,
)

STRATEGY_ROLES = {
    "buy_hold": (), "ma_crossover": (), "single_agent": ("decision",),
    "multi_agent": ("analysis", "critic", "decision"),
}


def selected_roles(protocol):
    """Server-owned roles. An omitted protocol is the historical four-strategy plan."""
    if protocol is None:
        return STRATEGY_ROLES
    if protocol == "acceptance-stage-a-v1":
        return {"single_agent": ("decision",)}
    if protocol == "acceptance-stage-b-v1":
        return {"multi_agent": ("analysis", "critic", "decision")}
    raise PlanningError("ACCEPTANCE_PROTOCOL_REJECTED")
PROVIDER_ENDPOINTS = {"openai":"https://api.openai.com/v1/responses", "kuaiapi":"https://kuaiapi.net/v1/responses"}


class PlanningError(ValueError):
    """Stable sanitized rejection, never reflect private profile/path values."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def resolve_profile(profile_id: str, allowlist: Mapping[str, ServerResearchProfile]):
    profile = allowlist.get(profile_id)
    if profile is None or profile.profile_id != profile_id:
        raise PlanningError("PROFILE_NOT_ALLOWLISTED")
    return profile


def plan_study(request: StudyRequest, profile: ServerResearchProfile, registry: DatasetRegistry,
               *, context: SourceContext) -> tuple[FrozenStudyPlan, SafePlanView]:
    # Revalidate even model_copy/model_construct callers; do not trust typed objects.
    request = StudyRequest.model_validate(request.model_dump(mode="json", warnings=False))
    profile = ServerResearchProfile.model_validate(profile.model_dump(mode="json", warnings=False))
    context = SourceContext.model_validate(context.model_dump(mode="json", warnings=False))
    if request.profile_id != profile.profile_id:
        raise PlanningError("PROFILE_NOT_ALLOWLISTED")
    if not profile.valid_from <= context.checked_at < profile.expires_at:
        raise PlanningError("PROFILE_OUTSIDE_VALIDITY")
    if (context.engine_version, context.feature_version, context.prompt_version) != (ENGINE_VERSION, FEATURE_VERSION, PROMPT_VERSION):
        raise PlanningError("VERSION_DRIFT")
    if (context.source_hash, context.uv_lock_hash, context.frontend_lock_hash) != (
            profile.expected_source_hash, profile.expected_uv_lock_hash, profile.expected_frontend_lock_hash):
        raise PlanningError("SOURCE_OR_LOCK_DRIFT")
    if (request.dataset_id, request.expected_dataset_hash) != (profile.dataset_id, profile.dataset_hash):
        raise PlanningError("DATASET_BINDING_MISMATCH")
    if not profile.allowed_start <= request.start_date <= request.end_date <= profile.allowed_end:
        raise PlanningError("PERIOD_NOT_ALLOWLISTED")
    if request.requested_budget_usd > profile.max_budget_usd or request.requested_max_attempts > profile.max_attempts:
        raise PlanningError("REQUESTED_CAP_EXCEEDS_PROFILE")
    try:
        dataset = registry.load(request.dataset_id)
    except (ValueError, KeyError, OSError, TypeError, csv.Error):
        raise PlanningError("DATASET_INVALID_OR_UNAVAILABLE") from None
    if dataset.hash != request.expected_dataset_hash or dataset.metadata.get("kind") != profile.dataset_kind:
        raise PlanningError("DATASET_BINDING_MISMATCH")
    if dataset.metadata.get("return_basis") != "price-only":
        raise PlanningError("PRICE_ONLY_DATA_REQUIRED")
    exp = ExperimentConfig(dataset_id=request.dataset_id, dataset_hash=dataset.hash,
        start_date=request.start_date, end_date=request.end_date, initial_cash=request.initial_cash,
        fee_bps=request.fee_bps, slippage_bps=request.slippage_bps)
    try:
        calendar = schedule(dataset, exp, {})  # Existing calendar/warmup check precedes numerical work.
        # Validate the whole immutable file, but calculate no features after the plan.
        prefix = Dataset(dataset.metadata, tuple(b for b in dataset.bars if b.session_date <= request.end_date))
        features = snapshots(prefix)
    except (ValueError, KeyError, TypeError):
        raise PlanningError("CALENDAR_OR_WARMUP_INVALID") from None
    eligible = [(d, x) for d, x in sorted(calendar.items()) if x <= request.end_date and d in features]
    if not eligible:
        raise PlanningError("NO_EXECUTABLE_FEATURE_SESSIONS")
    protocol = request.study_protocol
    selected = selected_roles(protocol)
    if protocol is not None:
        if profile.attempts_per_role != 1:
            raise PlanningError("ACCEPTANCE_RETRY_NOT_ALLOWED")
        if len(eligible) != 1:
            raise PlanningError("ACCEPTANCE_REQUIRES_ONE_DECISION_WEEK")
    sessions = tuple(PlannedSession(decision=d, execution=x,
        warmup_bars=sum(b.session_date <= d for b in dataset.bars),
        numeric_input_hash=canonical_hash(features[d].numeric_input())) for d, x in eligible)
    single_nominal = len(eligible) if "single_agent" in selected else 0
    multi_nominal = len(eligible)*3 if "multi_agent" in selected else 0
    nominal = single_nominal+multi_nominal
    complete = nominal*profile.attempts_per_role
    capped = min(complete, request.requested_max_attempts)
    # Bounded decimal fields fit exactly; ambient caller precision cannot lower a quote.
    with localcontext() as arithmetic:
        arithmetic.prec = 40
        per_attempt = (Decimal(32768)*(max(profile.input_usd_per_million, profile.cache_read_usd_per_million)+profile.cache_write_usd_per_million)
                       + Decimal(profile.settings.max_output_tokens)*profile.output_usd_per_million)/Decimal(1000000)
        estimate = PlanEstimate(single_nominal_calls=single_nominal, multi_nominal_calls=multi_nominal,
            total_nominal_calls=nominal, complete_attempt_ceiling=complete, capped_attempt_ceiling=capped,
            output_token_bound=profile.settings.max_output_tokens, reservation_usd_per_attempt=per_attempt,
            nominal_reservation_usd=per_attempt*nominal, complete_reservation_usd=per_attempt*complete,
            capped_reservation_usd=per_attempt*capped,
            all_attempts_fit_requested_caps=complete <= request.requested_max_attempts and per_attempt*complete <= request.requested_budget_usd)
    needed = {stage for stages in selected.values() for stage in stages}
    prompts = {stage: {"instructions": INSTRUCTIONS[stage], "schema": schema.model_json_schema(),
        "strict_request_schema": strict_schema(schema.model_json_schema()),
        "schema_hash": canonical_hash(schema.model_json_schema()),
        "prompt_hash": canonical_hash({"version": PROMPT_VERSION, "instructions": INSTRUCTIONS[stage], "schema": schema.model_json_schema()})}
        for stage, schema in SCHEMAS.items() if stage in needed}
    decisions = []
    for day, execution in eligible:
        snapshot = features[day].model_dump(mode="json", exclude={"snapshot_id"})
        numeric = features[day].numeric_input()
        # Actual future predecessor outputs cannot be known in an offline preview.
        templates = {}
        for strategy, stages in selected.items():
            templates[strategy] = [{"stage": stage, "features": numeric,
                "validated_predecessors": list(stages[:index]),
                "predecessor_schema_hashes": {p: prompts[p]["schema_hash"] for p in stages[:index]}}
                for index, stage in enumerate(stages)]
        decisions.append({"decision": str(day), "execution": str(execution), "snapshot": snapshot,
            "snapshot_hash": canonical_hash(snapshot), "numeric_input_hash": canonical_hash(numeric),
            "features_only_payload_hash": canonical_hash({"features": numeric}),
            "role_input_templates": templates, "role_input_templates_hash": canonical_hash(templates)})
    # A null protocol is omitted so an ordinary request keeps the pre-protocol manifest bytes.
    request_body = request.model_dump(mode="json")
    if request_body.get("study_protocol") is None:
        request_body.pop("study_protocol", None)
    if request_body.get("overall_deadline_seconds") is None:
        request_body.pop("overall_deadline_seconds", None)
    if request_body.get("transport_intent") in (None, "fixture"):
        request_body.pop("transport_intent", None)
    payload = {"kind": "offline-study-plan-v1", "request": request_body,
        "server_profile": profile.model_dump(mode="json"), "provider_endpoint":PROVIDER_ENDPOINTS[profile.provider],
        "source": context.model_dump(mode="json"),
        "dataset": {k: dataset.metadata[k] for k in ("id", "sha256", "kind", "symbol", "currency", "adjustment_mode", "return_basis")},
        "sessions": [s.model_dump(mode="json") for s in sessions], "decision_inputs": decisions,
        "non_executable_sessions": [{"decision":str(d), "reason":"NO_EXECUTION_SESSION" if x > request.end_date else "INVALID_FEATURES"}
            for d,x in sorted(calendar.items()) if (d,x) not in eligible],
        "strategy_definitions": {k: {"roles":list(v), "baseline":not v,
            "rule":("target 1 at first common execution then hold" if k == "buy_hold" else
                    "target 1 iff MA20 > MA60 else 0 at common weekly decisions" if k == "ma_crossover" else
                    "validated role outputs through deterministic risk at common weekly decisions")}
            for k,v in selected.items()},
        "prompts": prompts, "estimate": estimate.model_dump(mode="json"),
        "policy": {"execution_policy_version": EXECUTION_POLICY_VERSION,
            "price_basis":"raw", "return_basis":"price-only", "calendar":"XNYS", "warmup_bars":60,
            "signal":"last session of ISO week after close", "execution":"next valid session open within period",
            "dividends":False, "cash_interest":False, "forced_final_liquidation":False,
            "invalid_decision":"skip-retain", "approved_zero":"liquidate", "execution_authorized":False,
            "retry":"server-frozen attempt ceiling; no uncertain transport resend; not execution permission",
            "billing":"estimates only; full input plus cache-write reservation; aggregate actual separate",
            "input_bound_enforcement":"future execution must reject encoded request bytes plus 4096 above 32768 before reserving/sending",
            "predecessors":"templates only; actual payload/output hashes must be saved at execution"}}
    if request.overall_deadline_seconds is not None:
        payload["policy"]["overall_deadline"] = {
            "version": "overall-deadline-v2", "seconds": request.overall_deadline_seconds,
            "semantics": "local socket wait cap and bytes already yielded; blocked waits use whole seconds; DNS before connect is not covered; not an upstream billing stop"}
    if request.transport_intent == "live":
        payload["policy"]["transport_intent"] = "live"
        payload["policy"]["live_start_allowed"] = False
    if protocol is not None:
        payload["policy"]["acceptance_protocol"] = {
            "id": protocol, "selected_strategies": list(selected), "nominal_calls": nominal,
            "attempts_per_role": 1, "baselines_included": False, "real_spending_authorized": False,
            "not_full_four_strategy_research": True, "not_equal_inference_comparison": True,
            "scope": "one decision week; no smoke; no retry; no second week"}
    # JSON serialization detaches nested objects; no UUID or implicit now in payload.
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    frozen = FrozenStudyPlan(canonical_payload=payload_json, manifest_hash=canonical_hash(payload))
    warnings = ["OFFLINE_PREVIEW_NOT_FUNDED_OR_AUTHORIZED", "NO_KEY_OR_CURRENT_BILLING_CHECK",
        "PRICE_ONLY_DIVIDENDS_EXCLUDED", "HISTORICAL_MODEL_KNOWLEDGE_POSSIBLE",
        "SINGLE_MULTI_UNEQUAL_INFERENCE_WORK", "TOKEN_COST_ESTIMATE_NOT_INVOICE",
        "TRANSACTION_FEES_DEPEND_ON_FUTURE_TRADES", "PREDECESSOR_INPUTS_ARE_TEMPLATES"]
    if profile.dataset_kind == "synthetic":
        warnings.append("SYNTHETIC_DATA_NOT_HISTORICAL_RESEARCH")
    if profile.provider == "kuaiapi":
        warnings.append("GATEWAY_LABEL_NOT_UPSTREAM_IDENTITY_PROOF")
    if not estimate.all_attempts_fit_requested_caps:
        warnings.append("REQUESTED_CAPS_CANNOT_COVER_COMPLETE_ATTEMPT_PLAN")
    if protocol is not None:
        warnings.extend(("ACCEPTANCE_NOT_FULL_FOUR_STRATEGY_RESEARCH", "ACCEPTANCE_NOT_EQUAL_INFERENCE_COMPARISON",
                         "SYNTHETIC_ACCEPTANCE_NOT_LIVE_SPEND"))
        if not estimate.all_attempts_fit_requested_caps:
            warnings.append("ACCEPTANCE_PROTOCOL_CAPS_INSUFFICIENT")
    if request.transport_intent == "live":
        warnings.append("LIVE_APPROVAL_REQUIRED")
    if request.overall_deadline_seconds is not None:
        warnings.append("OVERALL_DEADLINE_DOES_NOT_STOP_UPSTREAM_BILLING")
    view = SafePlanView(manifest_hash=frozen.manifest_hash, profile_id=profile.profile_id,
        provider=profile.provider, model_id=profile.model_id, identity_scope=profile.identity_scope,
        dataset_id=request.dataset_id, dataset_hash=dataset.hash, dataset_kind=profile.dataset_kind,
        start_date=request.start_date, end_date=request.end_date, initial_cash=request.initial_cash,
        fee_bps=request.fee_bps, slippage_bps=request.slippage_bps, max_exposure=request.max_exposure,
        sessions=sessions, estimate=estimate, requested_budget_usd=request.requested_budget_usd,
        requested_max_attempts=request.requested_max_attempts, settings=profile.settings,
        strategy_ids=tuple(selected), study_protocol=protocol,
        overall_deadline_seconds=request.overall_deadline_seconds,
        transport_intent=None if request.transport_intent == "fixture" else request.transport_intent,
        engine_version=context.engine_version,
        feature_version=context.feature_version, prompt_version=context.prompt_version,
        planner_version=context.planner_version, attempts_per_role=profile.attempts_per_role,
        expires_at=profile.expires_at, warnings=tuple(warnings))
    return frozen, view


def acceptance_start_refusal(view: SafePlanView):
    """Refuse an acceptance start that cannot finish. Standard research is unchanged."""
    if view.study_protocol is None:
        return None
    expected = {"acceptance-stage-a-v1": (("single_agent",), 1), "acceptance-stage-b-v1": (("multi_agent",), 3)}
    roles, nominal = expected[view.study_protocol]
    if view.attempts_per_role != 1 or view.strategy_ids != roles or view.estimate.total_nominal_calls != nominal:
        return "ACCEPTANCE_PROTOCOL_GEOMETRY_INVALID"
    if view.estimate.complete_attempt_ceiling != nominal or not view.estimate.all_attempts_fit_requested_caps:
        return "ACCEPTANCE_PROTOCOL_CAPS_INSUFFICIENT"
    return None


def verify_plan(frozen: FrozenStudyPlan, request: StudyRequest, profile: ServerResearchProfile,
                registry: DatasetRegistry, *, context: SourceContext):
    """Pure drift check; never resume/start an experiment or release a hold."""
    checked = FrozenStudyPlan.model_validate(frozen.model_dump())
    current, _ = plan_study(request, profile, registry, context=context)
    if current.manifest_hash != checked.manifest_hash:
        raise PlanningError("FROZEN_PLAN_DRIFT")


class ExecutionError(ValueError):
    """Sanitized execution rejection. The message is a stable code, never a secret."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_DRIFT = {"PROVIDER_DRIFT", "MODEL_DRIFT", "ENDPOINT_DRIFT", "PRICING_UNIT_DRIFT", "MULTIPLIER_DRIFT",
          "TIER_DRIFT", "RATE_DRIFT", "BINDING_DRIFT", "GROUP_DRIFT"}
_TIMEOUT_SEMANTICS = "HTTPX phase/chunk inactivity limits; not a total wall-clock deadline"
EXECUTION_POLICY_VERSION = "execution-checkpoint-v2-billing-before-fill"


class _ExecutionRuntime:
    """Advancing execution clock and bounded injected readonly evidence checks."""
    def __init__(self, store, study_id, inputs, at, billing, usage_evidence, clock):
        import time
        self.store, self.study_id, self.inputs = store, study_id, inputs
        self.started, self.monotonic = at, time.monotonic()
        self.billing, self.usage_evidence, self.clock = billing, usage_evidence, clock
        self.last = at
        self.billing_window = None

    def now(self):
        import time
        from datetime import timedelta
        value = self.clock() if self.clock is not None else self.started + timedelta(seconds=time.monotonic()-self.monotonic)
        if value.tzinfo is None or value.utcoffset() is None or value < self.last:
            raise ExecutionError("INVALID_EXECUTION_CLOCK")
        self.last = value
        return value

    def check(self):
        at = self.now()
        try:
            self.store.check_execution_scope(self.study_id, self.inputs, at=at)
            document = self.billing(at) if callable(self.billing) else self.billing
            if isinstance(document, dict) and document.get("kind") == "production-shaped-usage-v1":
                from .live_readiness import classify_billing_evidence
                checked = classify_billing_evidence(self.inputs.profile, document, at=at, key_ref=self.store.bound_key_ref(self.study_id))
                result = {"ok": checked["ok"], "pause": checked["pause"], "codes": checked["codes"], "detail": checked["detail"]}
            else:
                result = assess_preflight(self.inputs.profile, document, at=at, key_ref=self.store.bound_key_ref(self.study_id))
            if result['ok']:
                window = (document['as_of_date'], document['timezone'])
                self.store.pin_billing_window(self.study_id, *window, at=at)
                if self.billing_window is None:
                    self.billing_window = window
                elif window != self.billing_window:
                    result = {'ok': False, 'pause': 'PAUSED_BILLING', 'codes': ['DAY_WINDOW_ROLLOVER'], 'detail': 'DAY_WINDOW_ROLLOVER'}
        except ValueError:
            result = {"ok": False, "pause": "PAUSED_DRIFT", "codes": ["EXECUTION_SCOPE_EXPIRED_OR_CHANGED"], "detail": "EXECUTION_SCOPE_EXPIRED_OR_CHANGED"}
        if not result['ok'] and not _cancelled(self.store, self.study_id):
            self.store.pause(self.study_id, result['pause'], at=at, detail=result['detail'])
        return result

    def reconcile(self, slot_id):
        from .pilot_billing import reconcile_study_usage
        if not callable(self.usage_evidence):
            return False
        try:
            evidence = self.usage_evidence(self.store, self.study_id, slot_id)
            reconcile_study_usage(self.store, self.study_id, evidence)
            return self.store.call_billing_verified(slot_id)
        except (ValueError, TypeError, KeyError):
            return False


def assess_preflight(profile: ServerResearchProfile, billing, *, at, key_ref):
    """Pure fake billing check. It does not read a key, open a socket, or retry inference."""
    profile = ServerResearchProfile.model_validate(profile.model_dump(mode="json", warnings=False))
    codes = []
    if not isinstance(billing, dict) or billing.get("kind") != "synthetic-readonly-billing-v1" or billing.get("semantics") != "token-effective-usd":
        return {"ok": False, "pause": "PAUSED_BILLING", "codes": ["BILLING_EVIDENCE_UNSUPPORTED"], "detail": "BILLING_EVIDENCE_UNSUPPORTED"}
    if billing.get("provider") != profile.provider:
        codes.append("PROVIDER_DRIFT")
    if billing.get("model_id") != profile.model_id:
        codes.append("MODEL_DRIFT")
    if billing.get("endpoint") != PROVIDER_ENDPOINTS[profile.provider]:
        codes.append("ENDPOINT_DRIFT")
    if billing.get("pricing_unit") != profile.pricing_unit:
        codes.append("PRICING_UNIT_DRIFT")
    try:
        multiplier = billing.get("multiplier")
        if isinstance(multiplier, bool) or Decimal(str(multiplier)) != 1:
            codes.append("MULTIPLIER_DRIFT")
    except Exception:
        codes.append("MULTIPLIER_DRIFT")
    tier = billing.get("service_tier")
    if tier not in ("auto", "default") or (profile.settings.service_tier is not None and tier != profile.settings.service_tier):
        codes.append("TIER_DRIFT")
    try:
        if any(Decimal(str(billing.get(field))) != getattr(profile, field) for field in (
                "input_usd_per_million", "output_usd_per_million", "cache_read_usd_per_million", "cache_write_usd_per_million")):
            codes.append("RATE_DRIFT")
    except Exception:
        codes.append("RATE_DRIFT")
    if profile.provider == "kuaiapi":
        binding = next(item for item in profile.private_evidence if item.kind == "binding")
        if billing.get("binding_ref") != binding.reference_id or billing.get("binding_sha256") != binding.sha256:
            codes.append("BINDING_DRIFT")
    if billing.get("group_ref") != key_ref:
        codes.append("GROUP_DRIFT")
    if billing.get("delayed") is not False:
        codes.append("BILLING_DELAYED")
    if type(billing.get("external_same_key_requests")) is not int or billing["external_same_key_requests"] != 0:
        codes.append("EXTERNAL_SAME_KEY_USAGE")
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        start = datetime.fromisoformat(billing["valid_from"])
        end = datetime.fromisoformat(billing["expires_at"])
        if start.tzinfo is None or end.tzinfo is None or start.utcoffset() is None or end.utcoffset() is None or not start <= at < end:
            codes.append("BILLING_EXPIRED")
        elif at.astimezone(ZoneInfo(billing["timezone"])).date().isoformat() != billing.get("as_of_date") or billing.get("day_count") != 1:
            codes.append("DAY_WINDOW_ROLLOVER")
    except Exception:
        codes.append("BILLING_SEMANTICS_UNSUPPORTED")
    # A config mismatch outranks an ambiguous bill. Neither one is guessed through.
    pause = "PAUSED_DRIFT" if any(code in _DRIFT for code in codes) else "PAUSED_BILLING" if codes else None
    detail = ",".join(sorted(set(codes)))[:200]
    return {"ok": not codes, "pause": pause, "codes": sorted(set(codes)), "detail": detail}


def poll_study(store, study_id):
    """Read-only status. No migration, provider, reservation or dispatch."""
    return store.execution_report(study_id)


def execute_study(store, study_id, inputs, *, at, service_enabled=False, client=None, authorization=None, billing=None, fault=None, secrets=(), usage_evidence=None, clock=None, live_binding=None):
    """Run one admitted study on the shared store. Disabled unless service_enabled is true."""
    if service_enabled is not True:
        raise ExecutionError("SERVICE_DISABLED")
    from .pilot import STRATEGIES
    if tuple(STRATEGY_ROLES) != STRATEGIES:
        raise ExecutionError("STRATEGY_SET_DRIFT")
    current, view = plan_study(inputs.request, inputs.profile, inputs.registry, context=inputs.context)
    refusal = acceptance_start_refusal(view)
    if refusal:
        raise ExecutionError(refusal)
    saved = store.get_study(study_id)
    if current.manifest_hash != saved["plan"]["manifest_hash"]:
        raise PlanningError("FROZEN_PLAN_DRIFT")
    if inputs.request.transport_intent == "live":
        code = "LIVE_APPROVAL_REQUIRED" if live_binding is None else live_binding.permits(
            inputs.request, inputs.profile, saved["plan"]["manifest_hash"], at)
        if code:
            raise ExecutionError(code)
    elif live_binding is not None:
        raise ExecutionError("LIVE_BINDING_REJECTED")
    if saved["state"] != "QUEUED":
        return _done(store, study_id, "BLOCKED", 0, [])
    runtime = _ExecutionRuntime(store, study_id, inputs, at, billing, usage_evidence, clock)
    preflight = runtime.check()
    if not preflight["ok"]:
        return _done(store, study_id, preflight["pause"], 0, preflight["codes"])
    slots = store.list_slots(study_id)
    unstarted = any(slot["state"] == "UNSTARTED" for slot in slots)
    if inputs.request.transport_intent == "live":
        transport = live_binding.transport() if unstarted else None
    else:
        if unstarted and (client is None or not isinstance(authorization, str) or not authorization):
            raise ExecutionError("INJECTED_CLIENT_REQUIRED")
        transport = _transport(client, inputs, authorization, secrets) if unstarted else None
    try:
        store.mark_running(study_id, inputs, at=runtime.now())
    except Exception as exc:
        from .research_store import StudyStorageError
        if isinstance(exc, StudyStorageError) and str(exc) == "RUNNING_REQUIRES_QUEUED":
            return _done(store, study_id, "BLOCKED", 0, [])
        raise
    dataset = inputs.registry.load(inputs.request.dataset_id)
    plan = store.load_plan(study_id)
    outcome = "COMPLETED"
    protocol = plan.payload["request"].get("study_protocol")
    for strategy in selected_roles(protocol):
        if _cancelled(store, study_id):
            outcome = "CANCELLED"
            break
        if _gate(store, study_id, "BEFORE_ROLE", runtime.now(), fault) == "cancel":
            outcome = "CANCELLED"
            break
        config = _config(plan, strategy, study_id)
        run_id, _, continue_run = _open_source(store, config, f"{study_id}:source:{strategy}")
        if not continue_run:
            store.link_child(study_id, strategy, run_id)
            continue
        result, halt = _walk_agent(store, study_id, plan, config, dataset, run_id, at, transport, fault, inputs, runtime)
        if halt == "cancel":
            store.update(run_id, status="FAILED", progress=100, result=result, error="CANCELLED_PARTIAL")
            store.link_child(study_id, strategy, run_id)
            outcome = "CANCELLED"
            break
        if halt in ("pause", "budget"):
            store.update(run_id, progress=99, result=result)
            store.link_child(study_id, strategy, run_id)
            if store.get_study(study_id)["state"] == "RUNNING":
                store.pause(study_id, "PAUSED_DRIFT" if halt == "pause" and result.get("pause_state") == "PAUSED_DRIFT" else "PAUSED_BILLING", at=at)
            outcome = store.get_study(study_id)["state"]
            break
        store.update(run_id, status="COMPLETED", progress=100, result=result)
        store.link_child(study_id, strategy, run_id)
    if outcome == "CANCELLED":
        if store.get_study(study_id)["state"] == "CANCEL_REQUESTED":
            store.finish(study_id, "CANCELLED", at=at)
        outcome = store.get_study(study_id)["state"]
    elif outcome == "COMPLETED" and store.get_study(study_id)["state"] == "RUNNING":
        store.finish(study_id, "COMPLETED", at=at)
    return _done(store, study_id, outcome, 0 if transport is None else transport.posts, [])


def resume_study(store, study_id, inputs, *, at, service_enabled=False, client=None, authorization=None, billing=None, fault=None, secrets=(), usage_evidence=None, clock=None, live_binding=None):
    """Continue only never-sent slots under the original freeze. Claimed slots are not resent."""
    if service_enabled is not True:
        raise ExecutionError("SERVICE_DISABLED")
    eligibility = store.resume_eligibility(study_id, inputs, at=at)
    if not eligibility["can_queue"]:
        raise ExecutionError("RESUME_NOT_ELIGIBLE")
    store.queue_verified_unstarted(study_id, inputs, at=at)
    return execute_study(store, study_id, inputs, at=at, service_enabled=True, client=client, authorization=authorization,
                         billing=billing, fault=fault, secrets=secrets, usage_evidence=usage_evidence, clock=clock,
                         live_binding=live_binding)


def replay_study(store, study_id, inputs, *, at):
    """Replay saved outcomes with new run ids. No provider, key, reservation or socket."""
    from .experiments import run_experiment
    from .pilot import semantic_result
    report = store.execution_report(study_id)
    dataset = inputs.registry.load(inputs.request.dataset_id)
    plan = store.load_plan(study_id)
    comparisons = {}
    for strategy, source in report["source_runs"].items():
        origin = store.get(source["run_id"], True)
        if not origin.get("result"):
            continue
        config = _config(plan, strategy, study_id, replay_of=source["run_id"])
        revision = canonical_hash(origin['result'])
        run_id, fresh = store.create(config, f"{study_id}:replay:{strategy}:{revision}")
        if fresh:
            store.update(run_id, status="RUNNING")
            if origin["status"] == "COMPLETED":
                result = run_experiment(config, dataset, source=origin["result"])
            else:
                result = _replay_partial(config, dataset, origin["result"])
            store.update(run_id, status="COMPLETED", progress=100, result=result)
        else:
            previous = store.get(run_id, True)
            if previous['status'] != 'COMPLETED' or previous['result'] is None:
                raise ExecutionError('REPLAY_NOT_COMPLETE')
            result = previous['result']
        comparisons[strategy] = {"run_id": run_id, "equal": semantic_result(result) == semantic_result(origin["result"])}
    return {"posts": 0, "replays": comparisons, "report": store.execution_report(study_id)}


def _done(store, study_id, status, posts, codes):
    report = store.execution_report(study_id)
    return {"status": status, "posts": posts, "preflight_codes": codes, "report": report}


def _transport(client, inputs, authorization, secrets):
    from .agents import InjectedFixtureTransport
    profile = inputs.profile
    return InjectedFixtureTransport(client, provider_id=profile.provider, endpoint=PROVIDER_ENDPOINTS[profile.provider],
        model_id=profile.model_id, settings=profile.settings.provider_settings(), timeout_policy=profile.timeout_policy,
        authorization=authorization, secrets=secrets)


def _config(plan, strategy, study_id, replay_of=None):
    from .models import ExperimentConfig
    payload = plan.payload
    request = payload["request"]
    profile = payload["server_profile"]
    roles = STRATEGY_ROLES[strategy]
    return ExperimentConfig(strategy_id=strategy, dataset_id=request["dataset_id"], dataset_hash=request["expected_dataset_hash"],
        start_date=request["start_date"], end_date=request["end_date"], initial_cash=request["initial_cash"],
        fee_bps=request["fee_bps"], slippage_bps=request["slippage_bps"], max_exposure=request["max_exposure"],
        research_group_id=str(study_id), prompt_version=payload["source"]["prompt_version"],
        max_llm_calls=request["requested_max_attempts"], max_llm_budget_usd=float(Decimal(request["requested_budget_usd"])),
        max_attempts_per_role=profile["attempts_per_role"], llm_mode="replay" if replay_of else "api" if roles else "mock",
        model_provider=profile["provider"] if roles else None, model_id=profile["model_id"] if roles else None,
        replay_source_run_id=replay_of)


def _open_source(store, config, key):
    run_id, fresh = store.create(config, key)
    current = store.get(run_id, True)
    if current["status"] == "COMPLETED":
        return run_id, current["result"], False
    if current["status"] == "FAILED":
        raise ExecutionError("SOURCE_RUN_FAILED_NOT_RETRIED")
    if current["status"] == "PENDING":
        store.update(run_id, status="RUNNING")
    return run_id, current.get("result"), True


def _cancelled(store, study_id):
    return store.get_study(study_id)["state"] in ("CANCEL_REQUESTED", "CANCELLED")


def _gate(store, study_id, name, at, fault, slot_id=None):
    store.checkpoint(study_id, name, at=at, slot_id=slot_id)
    if fault is not None:
        fault(name)
    if _cancelled(store, study_id):
        return "cancel"
    return None


def _quote(profile, input_tokens, output_tokens):
    return (Decimal(input_tokens) * (max(Decimal(profile["input_usd_per_million"]), Decimal(profile['cache_read_usd_per_million']))+Decimal(profile["cache_write_usd_per_million"])) + Decimal(output_tokens) * Decimal(profile["output_usd_per_million"])) / Decimal(1000000)


def _seal_retries(store, study_id, strategy, day, stage, at):
    for slot in store.list_slots(study_id):
        if slot["strategy"] == strategy and slot["decision_session"] == str(day) and slot["stage"] == stage and slot["attempt_no"] > 1 and slot["state"] == "UNSTARTED":
            store.close_unsent(slot["slot_id"], "NO_AUTOMATIC_RETRY", at=at)


def _find_slot(store, study_id, strategy, day, stage, attempt=1):
    for slot in store.list_slots(study_id):
        if slot["strategy"] == strategy and slot["decision_session"] == str(day) and slot["stage"] == stage and slot["attempt_no"] == attempt:
            return slot
    raise ExecutionError("SLOT_MISSING")


def _seen_response_ids(store, study_id):
    found = set()
    for slot in store.list_slots(study_id):
        if not slot["observation_json"]:
            continue
        envelope = json.loads(slot["observation_json"]).get("envelope")
        if isinstance(envelope, dict) and isinstance(envelope.get("id"), str):
            found.add(envelope["id"])
    return found


def _stage_payload(snapshot, stage, outputs):
    payload = {"features": snapshot.numeric_input()}
    if stage in ("critic", "decision") and "analysis" in outputs:
        payload["analysis"] = outputs["analysis"]
    if stage == "decision" and "critic" in outputs:
        payload["critic"] = outputs["critic"]
    return payload


def _trace_call(profile, endpoint, stage, payload, raw, validated, status, tokens, estimate, reserved, elapsed, call_id, error=None):
    from .agent_contracts import INSTRUCTIONS, SCHEMAS
    from .data import canonical_hash
    from .models import PROMPT_VERSION
    schema = SCHEMAS[stage].model_json_schema()
    attribution = {"provider": profile["provider"], "endpoint": endpoint, "group": None}
    call = {"call_id": call_id, "stage": stage, "attempt": 1, "mode": "api", "model_id": profile["model_id"], "actual_model_id": profile["model_id"],
            "model_settings": json.loads(json.dumps(profile["settings"])), "input": payload, "input_hash": canonical_hash(payload),
            "prompt_hash": canonical_hash({"version": PROMPT_VERSION, "instructions": INSTRUCTIONS[stage], "schema": schema}),
            "prompt_version": PROMPT_VERSION, "schema_hash": canonical_hash(schema), "instructions": INSTRUCTIONS[stage],
            "raw_response": raw, "validated_output": validated, "status": status, "error_code": error,
            "cost_usd": float(estimate) if estimate is not None else 0, "input_tokens": tokens[0], "output_tokens": tokens[1],
            "reserved_usd": float(reserved), "latency_ms": elapsed, "cost_status": "known" if estimate is not None else "unknown_reserved",
            "provider_attribution": attribution}
    return call, attribution


def _walk_agent(store, study_id, plan, config, dataset, run_id, at, transport, fault, inputs, runtime):
    import time
    import httpx
    from uuid import UUID, uuid5
    from .agent_contracts import SCHEMAS, AgentFailure
    from .agents import classify_stage_envelope, request_exceeds_input_bound, response_request
    from .backtest import Ledger, assess_risk, performance
    from .data import get_calendar, schedule
    from .features import snapshots
    from .http_policy import timeout_category
    from .models import ENGINE_VERSION, FEATURE_VERSION, DecisionOutput
    from .experiments import baseline_output
    is_agent = bool(STRATEGY_ROLES[config.strategy_id])
    profile = plan.payload["server_profile"]
    endpoint = plan.payload["provider_endpoint"]
    features = snapshots(dataset)
    decisions_at = schedule(dataset, config, features)
    calendar = get_calendar(config.start_date, config.end_date)
    ledger = Ledger(config.initial_cash)
    bars = [bar for bar in dataset.bars if config.start_date <= bar.session_date <= config.end_date]
    decisions, trades, equity = [], [], []
    pending = None
    halt = None
    pause_state = None
    buy_hold_decided = False
    prior_events = store.events(run_id)
    committed = {event['payload']['decision_id']: event['payload'] for event in prior_events if event['event_type'] == 'trade'}
    committed_decisions = {event['payload']['decision_id']: event['payload'] for event in prior_events
                           if event['event_type'] == 'decision' and event['payload']['decision_id'] in committed}
    record_event = lambda kind, payload: store.record_event(run_id, kind, payload)
    for index, bar in enumerate(bars):
        at = runtime.now()
        day = bar.session_date
        if pending and halt is None:
            decision, _target = pending
            already_filled = committed.get(decision['decision_id'])
            if already_filled:
                rebuilt = ledger.rebalance(decision['approved_weight'], bar.open, config.fee_bps, config.slippage_bps, day, decision['decision_id'])
                if rebuilt is None or any(rebuilt.get(k) != value for k, value in already_filled.items() if k not in ('trade_id', 'executed_at')):
                    raise ExecutionError('COMMITTED_FILL_RECONSTRUCTION_MISMATCH')
                trades.append(already_filled)
                decision['trade_id'] = already_filled['trade_id']
                pending = None
            elif _gate(store, study_id, "BEFORE_FILL", at, fault, decision["decision_id"]) == "cancel":
                decision.update(status="SKIPPED", approved_weight=None, decision_output=None, reason_codes=["cancelled"])
                record_event("decision", decision)
                pending = None
                halt = "cancel"
            elif is_agent and not runtime.check()['ok']:
                decision.update(status="SKIPPED", approved_weight=None, decision_output=None, reason_codes=["BILLING_PREFILL_CHECK_FAILED"])
                pending, halt = None, "pause"
                pause_state = store.get_study(study_id)['state']
                record_event('decision', decision)
            else:
                if str(day) != decision["execution_session"]:
                    raise ExecutionError("EXECUTION_CALENDAR_MISMATCH")
                trade = ledger.rebalance(decision["approved_weight"], bar.open, config.fee_bps, config.slippage_bps, day, decision["decision_id"])
                if trade:
                    trade['trade_id'] = str(uuid5(UUID(decision['decision_id']), 'fill'))
                    trade["executed_at"] = calendar.session_open(str(day)).isoformat()
                    trades.append(trade)
                    decision["trade_id"] = trade["trade_id"]
                    record_event("trade", trade)
                else:
                    decision["reason_codes"].append("TARGET_ALREADY_MET")
                record_event("decision", decision)
                pending = None
                if fault is not None:
                    fault("AFTER_FILL")
                if _cancelled(store, study_id):
                    halt = "cancel"
        if halt is None and day in decisions_at and not (config.strategy_id == 'buy_hold' and buy_hold_decided):
            snap = features.get(day)
            execution = decisions_at[day]
            decision_id = str(uuid5(UUID(str(study_id)), config.strategy_id + ":" + str(day)))
            record = {"decision_id": decision_id, "decision_session": str(day),
                      "execution_session": str(execution) if execution <= config.end_date else None,
                      "snapshot_id": str(snap.snapshot_id) if snap else None, "snapshot": snap.model_dump(mode="json") if snap else None,
                      "trade_id": None, "analysis_output": None, "critic_output": None, "decision_output": None, "calls": [],
                      "requested_weight": None, "approved_weight": None, "status": "SKIPPED", "reason_codes": []}
            if decision_id not in committed_decisions:
                record_event("decision", {**record, "status": "PENDING"})
            if decision_id in committed_decisions:
                # A crash after a durable fill must not resend, re-identify, or
                # erase that fill. Reconstruct its deterministic prefix first.
                record = json.loads(json.dumps(committed_decisions[decision_id]))
            elif _gate(store, study_id, "BEFORE_DECISION", at, fault, record["decision_id"]) == "cancel":
                record.update(status="SKIPPED", approved_weight=None, requested_weight=None, decision_output=None, reason_codes=["cancelled"])
                halt = "cancel"
            elif execution > config.end_date:
                record["reason_codes"] = ["NO_EXECUTION_SESSION"]
            elif snap is None:
                record["reason_codes"] = ["INVALID_FEATURES"]
            else:
                if is_agent:
                    stage_halt, outputs, calls, reasons, pause_state = _roles(store, study_id, plan, config, snap, day, at, transport, fault, profile, endpoint, inputs, time, httpx, classify_stage_envelope, request_exceeds_input_bound, response_request, timeout_category, SCHEMAS, AgentFailure, runtime)
                    if _cancelled(store, study_id):
                        stage_halt = 'cancel'
                else:
                    stage_halt, outputs, calls, reasons = None, {'decision': baseline_output(config.strategy_id, snap)}, [], []
                record["calls"] = calls
                record["analysis_output"] = outputs.get("analysis")
                record["critic_output"] = outputs.get("critic")
                if stage_halt == "cancel":
                    parsed = outputs.get("decision")
                    record.update(status="SKIPPED", approved_weight=None, decision_output=None,
                                  requested_weight=parsed["target_weight"] if isinstance(parsed, dict) and "target_weight" in parsed else None,
                                  reason_codes=["cancelled"])
                    halt = "cancel"
                elif stage_halt in ("pause", "budget"):
                    record.update(status="SKIPPED", approved_weight=None, decision_output=None, requested_weight=None, reason_codes=reasons or [stage_halt])
                    halt = stage_halt
                elif stage_halt == "skip":
                    record.update(status="SKIPPED", approved_weight=None, decision_output=None, requested_weight=None, reason_codes=reasons or ["INVALID_DECISION"])
                else:
                    record["decision_output"] = outputs.get("decision")
                    parsed = DecisionOutput.model_validate(record["decision_output"]) if record["decision_output"] else None
                    risk = assess_risk(parsed, max_exposure=config.max_exposure)
                    record["requested_weight"] = parsed.target_weight if parsed else None
                    record["approved_weight"] = risk["approved_weight"]
                    record["status"] = risk["status"]
                    if not record["reason_codes"]:
                        record["reason_codes"] = risk["reason_codes"]
            decisions.append(record)
            record_event("decision", record)
            if halt is None and record["status"] == "APPROVED":
                pending = (record, record["approved_weight"])
                buy_hold_decided = config.strategy_id == 'buy_hold'
        equity.append({"session_date": str(day), "equity": ledger.equity(bar.close), "cash": ledger.cash,
                       "shares": ledger.shares, "close": bar.close})
        record_event("equity", equity[-1])
        if index % 10 == 0 or index == len(bars) - 1:
            store.update(run_id, progress=min(99, int((index + 1) / len(bars) * 100)))
    calls = [call for record in decisions for call in record["calls"]]
    metrics = performance(equity, trades, config.initial_cash)
    metrics.update(llm_calls=sum(call["mode"] == "api" for call in calls), agent_calls=sum(call["mode"] != "replay" for call in calls),
                   mock_calls=sum(call["mode"] == "mock" for call in calls), llm_cost_usd=sum(call["cost_usd"] for call in calls),
                   uncertain_cost_reserve_usd=sum(call["reserved_usd"] for call in calls if call.get("cost_status") == "unknown_reserved"),
                   llm_input_tokens=sum(call["input_tokens"] for call in calls), llm_output_tokens=sum(call["output_tokens"] for call in calls),
                   llm_latency_ms=sum(call["latency_ms"] for call in calls),
                   reserved_or_actual_usd=sum(call["reserved_usd"] if call.get("cost_status") == "unknown_reserved" else call["cost_usd"] for call in calls),
                   approved_decisions=sum(record["status"] == "APPROVED" for record in decisions),
                   skipped_decisions=sum(record["status"] == "SKIPPED" for record in decisions),
                   rejected_decisions=sum(record["status"] == "REJECTED" for record in decisions))
    attribution = calls[0]["provider_attribution"] if calls else {"provider": profile["provider"], "endpoint": endpoint, "group": None}
    result = {"schema_version": 1, "engine_version": ENGINE_VERSION, "feature_version": FEATURE_VERSION, "return_basis": "price-only",
              "decision_origin": "api" if is_agent else "baseline", "execution_policy_version": EXECUTION_POLICY_VERSION,
              "dataset": dataset.metadata, "decisions": decisions, "trades": trades, "model_ids": [profile["model_id"]] if is_agent else [],
              "provider_settings": profile["settings"] if is_agent else None, "provider_attribution": attribution if is_agent else None, "equity": equity, "metrics": metrics,
              "coverage_status": "COMPLETE" if halt is None else "PARTIAL", "pause_state": pause_state,
              "first_execution_session": next((record["execution_session"] for record in decisions if record["status"] == "APPROVED"), None)}
    return result, halt


def _roles(store, study_id, plan, config, snap, day, at, transport, fault, profile, endpoint, inputs, time, httpx, classify, exceeds, request_of, timeout_category, schemas, agent_failure, runtime):
    outputs, calls = {}, []
    reserved = Decimal(plan.payload["estimate"]["reservation_usd_per_attempt"])
    for stage in STRATEGY_ROLES[config.strategy_id]:
        at = runtime.now()
        slot = _find_slot(store, study_id, config.strategy_id, day, stage)
        if _gate(store, study_id, "BEFORE_ROLE", at, fault, slot["slot_id"]) == "cancel":
            return "cancel", outputs, calls, ["cancelled"], None
        preflight = runtime.check()
        if not preflight['ok']:
            return 'pause', outputs, calls, preflight['codes'], preflight['pause']
        if slot["state"] == "RECORDED":
            observed = json.loads(slot["observation_json"])
            if observed.get("sent") is False or slot["outcome"] != "SUCCEEDED":
                return "skip", outputs, calls + ([observed["call"]] if "call" in observed else []), observed.get("failure_codes") or [observed.get("reason") or "INVALID_DECISION"], None
            if not store.call_billing_verified(slot['slot_id']):
                return 'pause', outputs, calls + [observed['call']], ['AWAITING_VERIFIED_RECONCILIATION'], 'PAUSED_BILLING'
            outputs[stage] = observed["call"]["validated_output"]
            calls.append(observed["call"])
            continue
        if slot["state"] != "UNSTARTED":
            raise ExecutionError("UNRESOLVED_SLOT")
        payload = _stage_payload(snap, stage, outputs)
        schema = schemas[stage].model_json_schema()
        if exceeds(request_of(transport.model_id, transport.settings, stage, payload, schema)):
            store.close_unsent(slot["slot_id"], "INPUT_BOUND", at=at)
            _seal_retries(store, study_id, config.strategy_id, day, stage, at)
            return "skip", outputs, calls, ["INPUT_BOUND"], None
        if fault is not None:
            fault("BEFORE_RESERVE")
        try:
            store.reserve_slot(slot["slot_id"], inputs, at=at)
        except agent_failure as exc:
            if str(exc) == "PILOT_BUDGET_EXHAUSTED":
                return "budget", outputs, calls, ["PILOT_BUDGET_EXHAUSTED"], "PAUSED_BILLING"
            raise
        if fault is not None:
            fault("AFTER_RESERVE")
        fresh = _find_slot(store, study_id, config.strategy_id, day, stage)
        store.mark_dispatched(fresh["slot_id"], inputs, at=runtime.now())
        if fault is not None:
            fault("AFTER_DISPATCH")
        started = time.monotonic()
        try:
            status, reply = transport.post(stage, payload, schema)
        except httpx.TimeoutException as exc:
            _retain_transport(store, fresh, profile, endpoint, stage, payload, reserved, started, time, timeout_category(exc), at)
            _seal_retries(store, study_id, config.strategy_id, day, stage, at)
            return "pause", outputs, calls, ["PROVIDER_TIMEOUT"], "PAUSED_BILLING"
        except httpx.HTTPError:
            _retain_transport(store, fresh, profile, endpoint, stage, payload, reserved, started, time, ("unspecified", "HTTPError"), at)
            _seal_retries(store, study_id, config.strategy_id, day, stage, at)
            return "pause", outputs, calls, ["PROVIDER_NETWORK_ERROR"], "PAUSED_BILLING"
        elapsed = round((time.monotonic() - started) * 1000, 3)
        if fault is not None:
            fault("AFTER_RESPONSE")
        identity = reply.envelope.get("id")
        duplicate = isinstance(identity, str) and identity in _seen_response_ids(store, study_id)
        expected = {"http_status": status, "model_id": profile["model_id"], "provider_id": profile["provider"], "endpoint": endpoint,
                    "service_tier": profile["settings"].get("service_tier"), "max_output_tokens": profile["settings"]["max_output_tokens"]}
        classified = classify(stage, reply.envelope, reply.raw, expected)
        if classified['service_tier_observation']['value'] not in (None, 'auto', 'default'):
            classified = {**classified, 'release_reservation': False, 'eligible_for_fill': False, 'pause_study': True,
                          'hard_stop_codes': [*classified['hard_stop_codes'], 'UNVERIFIED_SERVICE_TIER']}
        estimate = _quote(profile, classified["input_tokens"], classified["output_tokens"]) if classified["usage_known"] else None
        if duplicate or estimate is not None and estimate > reserved:
            classified = {**classified, "release_reservation": False, "eligible_for_fill": False, "pause_study": True,
                          "hard_stop_codes": sorted(set([*classified["hard_stop_codes"], "DUPLICATE_OR_OVER_RESERVE"]))}
            estimate = None
        call, _ = _trace_call(profile, endpoint, stage, payload, reply.raw, classified["validated_output"] if classified["eligible_for_fill"] else None,
                              "VALIDATED" if classified["eligible_for_fill"] else "FAILED",
                              (classified["input_tokens"], classified["output_tokens"]) if classified["usage_known"] else (0, 0),
                              estimate, reserved, elapsed, fresh["call_id"], None if classified["eligible_for_fill"] else (classified["failure_codes"] or classified["hard_stop_codes"] or ["REJECTED"])[0])
        observation = {"kind": "stage-observation-v1", "sent": True, "input_tokens": call["input_tokens"], "output_tokens": call["output_tokens"],
                       "usage_estimate_usd": str(estimate) if estimate is not None else None, "release_reservation": classified["release_reservation"],
                       "failure_codes": classified["failure_codes"], "hard_stop_codes": classified["hard_stop_codes"],
                       "observation_warnings": classified["observation_warnings"], "classifier_version": classified["classifier_version"],
                       "cache_read_tokens": classified['cache_read_tokens'], "cache_write_tokens": classified['cache_write_tokens'],
                       "service_tier_observation": classified["service_tier_observation"], "envelope": reply.envelope, "call": call}
        if classified["pause_study"] or not classified["release_reservation"]:
            if classified["release_reservation"] and estimate is not None:
                store.settle_known_usage(fresh["slot_id"], "FAILED", observation, estimate, at=at)
                _seal_retries(store, study_id, config.strategy_id, day, stage, at)
                state = "PAUSED_DRIFT" if any(code in classified["hard_stop_codes"] for code in ("MODEL_LABEL_DRIFT", "PROVIDER_DRIFT", "ENDPOINT_DRIFT", "UNREQUESTED_TOOL_OR_OUTPUT_TYPE")) else "PAUSED_BILLING"
                return "pause", outputs, calls + [call], classified["hard_stop_codes"] or classified["failure_codes"], state
            state = 'PAUSED_DRIFT' if any(code in classified['hard_stop_codes'] for code in ('MODEL_LABEL_DRIFT', 'PROVIDER_DRIFT', 'ENDPOINT_DRIFT', 'UNREQUESTED_TOOL_OR_OUTPUT_TYPE', 'UNVERIFIED_SERVICE_TIER')) else 'PAUSED_BILLING'
            store.retain_liability(fresh['slot_id'], observation, at=at, pause_state=state)
            _seal_retries(store, study_id, config.strategy_id, day, stage, at)
            return "pause", outputs, calls + [call], classified["hard_stop_codes"] or classified["failure_codes"], state
        if not classified["eligible_for_fill"]:
            store.settle_known_usage(fresh["slot_id"], "FAILED", observation, estimate, at=at)
            _seal_retries(store, study_id, config.strategy_id, day, stage, at)
            if not runtime.reconcile(fresh['slot_id']):
                if not _cancelled(store, study_id):
                    store.pause(study_id, 'PAUSED_BILLING', at=runtime.now(), detail='AWAITING_VERIFIED_RECONCILIATION')
                return 'cancel' if _cancelled(store, study_id) else 'pause', outputs, calls+[call], ['AWAITING_VERIFIED_RECONCILIATION'], 'PAUSED_BILLING'
            return "skip", outputs, calls + [call], classified["failure_codes"] or ["SCHEMA_OR_JSON_INVALID"], None
        store.settle_known_usage(fresh["slot_id"], "SUCCEEDED", observation, estimate, at=runtime.now())
        if not runtime.reconcile(fresh['slot_id']):
            if not _cancelled(store, study_id):
                store.pause(study_id, 'PAUSED_BILLING', at=runtime.now(), detail='AWAITING_VERIFIED_RECONCILIATION')
            return 'cancel' if _cancelled(store, study_id) else 'pause', outputs, calls+[call], ['AWAITING_VERIFIED_RECONCILIATION'], 'PAUSED_BILLING'
        if fault is not None:
            fault("AFTER_SETTLEMENT")
        outputs[stage] = classified["validated_output"]
        calls.append(call)
        if _cancelled(store, study_id):
            return "cancel", outputs, calls, ["cancelled"], None
    return None, outputs, calls, [], None


def _provider_settings(profile):
    """Frozen JSON settings are a dict; provider_settings() is only on the model."""
    settings = {"max_output_tokens": profile["settings"]["max_output_tokens"], "store": False}
    if profile["settings"].get("reasoning_effort") is not None:
        settings["reasoning"] = {"effort": profile["settings"]["reasoning_effort"]}
    if profile["settings"].get("temperature") is not None:
        settings["temperature"] = profile["settings"]["temperature"]
    if profile["settings"].get("service_tier") is not None:
        settings["service_tier"] = profile["settings"]["service_tier"]
    return settings


def _retain_transport(store, slot, profile, endpoint, stage, payload, reserved, started, time, category, at):
    elapsed = round((time.monotonic() - started) * 1000, 3)
    phase, exception_class = category
    error = "PROVIDER_TIMEOUT" if exception_class != "HTTPError" else "PROVIDER_NETWORK_ERROR"
    call, _ = _trace_call(profile, endpoint, stage, payload, None, None, "FAILED", (0, 0), None, reserved, elapsed, slot["call_id"], error)
    call["timeout_diagnostic"] = {"phase": phase, "exception_class": exception_class,
                                  "policy": profile["timeout_policy"], "semantics": _TIMEOUT_SEMANTICS, "attempt_elapsed_ms": elapsed}
    observation = {"kind": "stage-observation-v1", "sent": True, "input_tokens": 0, "output_tokens": 0, "usage_estimate_usd": None,
                   "release_reservation": False, "failure_codes": [call["error_code"]], "hard_stop_codes": ["TRANSPORT_OR_ENDPOINT_FAILURE"],
                   "timeout_diagnostic": call["timeout_diagnostic"], "call": call}
    store.retain_liability(slot["slot_id"], observation, at=at)


def _replay_partial(config, dataset, source):
    """Replay a partial saved ledger. Non-approved rows do not become zero-weight orders."""
    from .backtest import Ledger, assess_risk, performance
    from .data import get_calendar, schedule
    from .features import snapshots
    from .models import ENGINE_VERSION, FEATURE_VERSION, DecisionOutput
    saved = {record["decision_session"]: record for record in source["decisions"]}
    last = max(saved) if saved else None
    features = snapshots(dataset)
    decisions_at = schedule(dataset, config, features)
    calendar = get_calendar(config.start_date, config.end_date)
    ledger = Ledger(config.initial_cash)
    bars = [bar for bar in dataset.bars if config.start_date <= bar.session_date <= config.end_date]
    decisions, trades, equity, pending = [], [], [], None
    for bar in bars:
        day = bar.session_date
        if pending:
            decision, target = pending
            if str(day) != decision["execution_session"]:
                raise ExecutionError("EXECUTION_CALENDAR_MISMATCH")
            trade = ledger.rebalance(target, bar.open, config.fee_bps, config.slippage_bps, day, decision["decision_id"])
            if trade:
                trade["executed_at"] = calendar.session_open(str(day)).isoformat()
                trades.append(trade)
                decision["trade_id"] = trade["trade_id"]
            else:
                decision['reason_codes'].append('TARGET_ALREADY_MET')
            pending = None
        if day in decisions_at and str(day) in saved:
            original = saved.get(str(day))
            snap = features.get(day)
            record = {"decision_id": original["decision_id"] if original else str(day), "decision_session": str(day),
                      "execution_session": str(decisions_at[day]), "snapshot_id": str(snap.snapshot_id) if snap else None,
                      "snapshot": snap.model_dump(mode="json") if snap else None, "trade_id": None,
                      "analysis_output": None, "critic_output": None, "decision_output": None, "calls": [],
                      "requested_weight": None, "approved_weight": None, "status": "SKIPPED", "reason_codes": ["REPLAY_MISSING_DECISION"]}
            if original and original["status"] == "APPROVED":
                from .agents import replay_decision
                record.update(replay_decision(snap, original, baseline=config.strategy_id not in ("single_agent", "multi_agent"),
                                             expected_provider=source.get("provider_attribution")))
                parsed = DecisionOutput.model_validate(record["decision_output"]) if record["decision_output"] else None
                risk = assess_risk(parsed, max_exposure=config.max_exposure)
                record["requested_weight"] = parsed.target_weight if parsed else None
                record["approved_weight"] = risk["approved_weight"]
                record["status"] = risk["status"]
                if not record["reason_codes"]:
                    record["reason_codes"] = risk["reason_codes"]
                if record["status"] == "APPROVED":
                    pending = (record, record["approved_weight"])
            elif original:
                record.update(status="SKIPPED", analysis_output=original.get("analysis_output"), critic_output=original.get("critic_output"),
                              decision_output=None, requested_weight=original.get("requested_weight"), approved_weight=None,
                              reason_codes=original.get("reason_codes") or ["cancelled"], calls=[])
            decisions.append(record)
        equity.append({"session_date": str(day), "equity": ledger.equity(bar.close), "cash": ledger.cash, "shares": ledger.shares, "close": bar.close})
    metrics = performance(equity, trades, config.initial_cash)
    calls = [call for record in decisions for call in record["calls"]]
    metrics.update(llm_calls=sum(call["mode"] == "api" for call in calls), agent_calls=sum(call["mode"] != "replay" for call in calls),
                   mock_calls=sum(call["mode"] == "mock" for call in calls), llm_cost_usd=sum(call["cost_usd"] for call in calls),
                   uncertain_cost_reserve_usd=sum(call["reserved_usd"] for call in calls if call.get("cost_status") == "unknown_reserved"),
                   llm_input_tokens=sum(call["input_tokens"] for call in calls), llm_output_tokens=sum(call["output_tokens"] for call in calls),
                   llm_latency_ms=sum(call["latency_ms"] for call in calls),
                   reserved_or_actual_usd=sum(call["reserved_usd"] if call.get("cost_status") == "unknown_reserved" else call["cost_usd"] for call in calls),
                   approved_decisions=sum(record["status"] == "APPROVED" for record in decisions),
                   skipped_decisions=sum(record["status"] == "SKIPPED" for record in decisions),
                   rejected_decisions=sum(record["status"] == "REJECTED" for record in decisions))
    return {"schema_version": 1, "engine_version": ENGINE_VERSION, "feature_version": FEATURE_VERSION, "return_basis": "price-only",
            "decision_origin": "replay", "dataset": dataset.metadata, "decisions": decisions, "trades": trades, "equity": equity,
            "metrics": metrics, "provider_attribution": source.get("provider_attribution"), "coverage_status": source.get("coverage_status", "PARTIAL")}
