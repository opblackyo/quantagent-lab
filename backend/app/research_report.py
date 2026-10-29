"""Read-only safe study report.

Projects stored decisions, fills, equity, and metrics. It does not recompute
the ledger, submit work, open a provider, or release a reservation.
"""
import json
import re
from uuid import UUID, uuid5

from .public_projection import (
    PACKAGES, _HEX40, _HEX64, _sanitize, _uuid, project_call, project_config, project_cost, project_result,
)

STRATEGIES = ("buy_hold", "ma_crossover", "single_agent", "multi_agent")
_AGENTS = {"single_agent", "multi_agent"}
_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,80}$")
_VERSION = re.compile(r"^[A-Za-z0-9.+-]{1,60}$")
LIMITATIONS = (
    "Price-only; dividends and interest are omitted.",
    "A pretrained model may contain historical knowledge.",
    "Single-agent and multi-agent roles do unequal work.",
    "Gateway identity is not independently verified.",
    "Return sign is not a success criterion.",
    "A synthetic fixture result is not a live API result.",
    "Run status, session coverage and comparability are independent. Buy and Hold records the entry decision only; later planned dates are policy holds, not recorded calls or transport failures. Missing decision sessions stay in the session denominator.",
)
_TRANSPORTS = {"synthetic-fixture-not-live-api", "unavailable"}
_REASON_ORDER = (
    "COMPARABILITY_MISMATCH", "MALFORMED_RESULT", "STRATEGY_RESULT_LINK_MISMATCH", "INPUT_HASH_MISMATCH",
    "ROLE_EVIDENCE_MISSING", "LINKAGE_UNVERIFIABLE", "PROMPT_HASH_MISMATCH", "DECISION_SESSION_MISSING",
    "RESULT_UNAVAILABLE", "SOURCE_ABSENT",
)
_BUY_HOLD_RULE = "target 1 at first common execution then hold"
_STAGES = {"analysis", "critic", "decision"}
_SLOT_STATES = {"UNSTARTED", "DISPATCHED", "RECORDED"}
_OUTCOMES = {"SUCCEEDED", "FAILED"}
_UNAVAILABLE_DIAGNOSTIC = {"max_abs_equity": None, "max_abs_shares": None, "hides_mismatch": False}


class _Malformed:
    """Stored result could not be read. The raw payload stays in the database."""


def semantic_replay_view(source_result, replay_result):
    """Exact ``semantic_result`` equality. A diagnostic never flips the flag."""
    diagnostic = _diagnostic(source_result, replay_result)
    if isinstance(source_result, _Malformed) or isinstance(replay_result, _Malformed):
        return {"equal": False, "reason": "MALFORMED_RESULT", "diagnostic": diagnostic}
    if not isinstance(source_result, dict) or not isinstance(replay_result, dict):
        return {"equal": False, "reason": "RESULT_UNAVAILABLE", "diagnostic": diagnostic}
    from .pilot import semantic_result
    try:
        equal = semantic_result(source_result) == semantic_result(replay_result)
    except ValueError as exc:
        if str(exc) != "MALFORMED_NUMERIC":
            raise
        return {"equal": False, "reason": "MALFORMED_RESULT", "diagnostic": dict(_UNAVAILABLE_DIAGNOSTIC)}
    except (KeyError, TypeError):
        return {"equal": False, "reason": "MALFORMED_RESULT", "diagnostic": dict(_UNAVAILABLE_DIAGNOSTIC)}
    return {"equal": bool(equal), "reason": None if equal else "SEMANTIC_MISMATCH", "diagnostic": diagnostic}


def build_study_report(store, study_id, secrets=(), transport_status="unavailable"):
    """Assemble the public report from the frozen plan and saved runs."""
    polling = store.execution_report(study_id)
    plan = store.load_plan(study_id)
    payload = plan.payload if isinstance(getattr(plan, "payload", None), dict) else {}
    view = polling.get("plan") if isinstance(polling.get("plan"), dict) else {}
    transport = transport_status if transport_status in _TRANSPORTS else "unavailable"
    sources = []
    observations = []
    semantics = []
    source_runs = polling.get("source_runs") if isinstance(polling.get("source_runs"), dict) else {}
    replay_runs = polling.get("replay_runs") if isinstance(polling.get("replay_runs"), dict) else {}
    for strategy in _selected_strategies(view):
        block, found = _source(store, study_id, strategy, source_runs.get(strategy), payload, view, secrets, transport)
        sources.append(block)
        observations.extend(found)
        semantics.append(_semantic(store, strategy, block.get("run_id"), replay_runs.get(strategy)))
    reasons = [item["comparability"] for item in sources if item["comparability"] != "ok"]
    body = {
        "read_only": True,
        "mutates_admission": False,
        "browser_tariff_authoritative": False,
        "browser_funding_approval": False,
        "versions": _versions(payload, view, transport),
        "sources": sources,
        "semantic_replay": semantics,
        "coverage": _coverage(sources, observations, payload),
        "cost": _cost(polling, secrets),
        "attempts": _attempts(store, study_id, sources, secrets),
        "limitations": _limitations(view),
        "comparability": {
            "ok": not reasons,
            "reason": _overall_reason(reasons),
            "mismatched_strategies": [item["strategy"] for item in sources if item["comparability"] == "COMPARABILITY_MISMATCH"],
            "affected_strategies": [{"strategy": item["strategy"], "reason": item["comparability"]} for item in sources if item["comparability"] != "ok"],
            "replay_does_not_replace_source": True,
        },
        "success_criterion": None,
        "profit_is_not_success": True,
        "labels": ["price-only", transport],
    }
    return _sanitize(body, secrets)


def _overall_reason(reasons):
    for code in _REASON_ORDER:
        if code in reasons:
            return code
    return None


def _pick(codes):
    return _overall_reason(codes) or "ok"


def _selected_strategies(view):
    listed = view.get("strategy_ids") if isinstance(view, dict) else None
    if isinstance(listed, list) and listed and all(item in STRATEGIES for item in listed):
        return tuple(listed)
    return STRATEGIES


def _limitations(view):
    lines = list(LIMITATIONS)
    protocol = view.get("study_protocol") if isinstance(view, dict) else None
    if protocol in {"acceptance-stage-a-v1", "acceptance-stage-b-v1"}:
        lines.append("Acceptance protocol evidence covers only the frozen strategy and one decision week. It is not four-strategy research and not an equal-work single-agent versus multi-agent comparison.")
    return lines


def _acceptance_complete(payload, sources):
    policy = payload.get("policy") if isinstance(payload, dict) and isinstance(payload.get("policy"), dict) else {}
    acceptance = policy.get("acceptance_protocol") if isinstance(policy.get("acceptance_protocol"), dict) else None
    if acceptance is None:
        return True
    nominal = acceptance.get("nominal_calls")
    if len(sources) != 1 or not sources[0].get("complete") or not isinstance(nominal, int):
        return False
    metrics = sources[0].get("metrics") if isinstance(sources[0].get("metrics"), dict) else {}
    return metrics.get("llm_calls") == nominal


def _coverage(sources, observations, payload=None):
    missing = sum(item["kind"] == "missing" for item in observations)
    complete = bool(observations) and missing == 0 and _acceptance_complete(payload, sources)
    return {
        "denominator": len(sources),
        "strategy_denominator": len(sources),
        "failures_remain_in_denominator": True,
        "strategies": [{"strategy": item["strategy"], "status": item["status"], "included_in_denominator": True} for item in sources],
        "session_denominator": len(observations),
        "recorded_sessions": sum(item["kind"] == "recorded_decision" for item in observations),
        "policy_hold_sessions": sum(item["kind"] == "policy_hold" for item in observations),
        "missing_sessions": missing,
        "verdict": "COMPLETE" if complete else "INCOMPLETE",
        "reason": None if complete else "COVERAGE_INCOMPLETE",
        "run_status_independent": True,
        "observations": observations,
    }


def _cost(polling, secrets):
    raw = polling.get("cost")
    projected = project_cost(raw, secrets) or {}
    plan = polling.get("plan") if isinstance(polling.get("plan"), dict) else {}
    estimate = plan.get("estimate") if isinstance(plan.get("estimate"), dict) else {}
    baseline_calls = estimate.get("baseline_calls")
    baseline_cost = estimate.get("baseline_cost_usd")

    def ledger(key):
        if raw is None:
            return None
        return projected.get(key)

    return {
        "usage_estimated_usd": ledger("usage_estimated_usd"),
        "confirmed_actual_usd": ledger("confirmed_actual_usd"),
        "unknown_reserved_usd": ledger("unknown_reserved_usd"),
        "charged_or_reserved_usd": ledger("charged_or_reserved_usd"),
        "actual_charge_scope": ledger("actual_charge_scope"),
        "invoiced_actual_usd": None,
        "per_call_actual_allocation_usd": None,
        "per_call_actual_reason": "NOT_ALLOCATED_FROM_AGGREGATE",
        "baseline_calls": baseline_calls if baseline_calls == 0 else None,
        "baseline_cost_usd": baseline_cost if baseline_cost == 0 else None,
        "baseline_scope": "planned_zero_inference" if baseline_calls == 0 and baseline_cost == 0 else "unavailable",
        "model_cost_in_trading_equity": False,
        "metric_llm_cost_is_not_invoice": True,
    }


def _versions(payload, view, transport):
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    prompts = payload.get("prompts") if isinstance(payload.get("prompts"), dict) else {}
    sessions = view.get("sessions") if isinstance(view.get("sessions"), list) else []
    schemas = {}
    prompt_hashes = {}
    for stage in ("analysis", "critic", "decision"):
        block = prompts.get(stage) if isinstance(prompts.get(stage), dict) else {}
        schemas[stage] = _hex(block.get("schema_hash"), 64)
        prompt_hashes[stage] = _hex(block.get("prompt_hash"), 64)
    inputs = []
    for session in sessions:
        digest = session.get("numeric_input_hash") if isinstance(session, dict) else None
        inputs.append(digest if isinstance(digest, str) and _HEX64.fullmatch(digest) else "unavailable")
    packages = {}
    listed = source.get("dependency_versions")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] in PACKAGES and isinstance(item[1], str) and _VERSION.fullmatch(item[1]):
                packages[item[0]] = item[1]
    provider = view.get("provider") if view.get("provider") in {"openai", "kuaiapi"} else "unavailable"
    identity = view.get("identity_scope") if view.get("identity_scope") in {"direct-provider-label", "gateway-reported-label-only"} else "unavailable"
    kind = view.get("dataset_kind") if view.get("dataset_kind") in {"synthetic", "historical"} else "unavailable"
    result = {
        "dataset_id": view.get("dataset_id") if isinstance(view.get("dataset_id"), str) and _TOKEN.fullmatch(view["dataset_id"]) else "unavailable",
        "dataset_hash": _hex(view.get("dataset_hash"), 64),
        "dataset_kind": kind,
        "source_commit": _hex(source.get("source_commit"), 40),
        "source_hash": _hex(source.get("source_hash"), 64),
        "uv_lock_hash": _hex(source.get("uv_lock_hash"), 64),
        "frontend_lock_hash": _hex(source.get("frontend_lock_hash"), 64),
        "manifest_hash": _hex(view.get("manifest_hash"), 64),
        "calendar": view.get("calendar") if view.get("calendar") == "XNYS" else "unavailable",
        "numeric_input_hashes": inputs,
        "prompt_version": _token(view.get("prompt_version")),
        "schema_hashes": schemas,
        "prompt_hashes": prompt_hashes,
        "model_id": _token(view.get("model_id")),
        "provider": provider,
        "identity_scope": identity,
        "transport": transport,
        "engine_version": _token(view.get("engine_version")),
        "feature_version": _token(view.get("feature_version")),
        "planner_version": _token(view.get("planner_version")),
        "execution_policy_version": _token(policy.get("execution_policy_version")),
        "dependency_versions": packages,
    }
    protocol = view.get("study_protocol") if isinstance(view.get("study_protocol"), str) else None
    if protocol in {"acceptance-stage-a-v1", "acceptance-stage-b-v1"}:
        result["study_protocol"] = protocol
        result["acceptance_scope"] = "selected-strategy-one-decision-week"
    return result


def _source(store, study_id, strategy, child, payload, view, secrets, transport):
    planned = _planned_dates(view, payload)
    if not isinstance(child, dict) or not child.get("run_id"):
        return _empty(strategy, "NOT_RUN", None, "SOURCE_ABSENT"), _blank_sessions(strategy, planned)
    run_id = _uuid(child.get("run_id"))
    try:
        run = store.get(child["run_id"], True)
    except (ValueError, TypeError, KeyError):
        return _empty(strategy, "MALFORMED_RESULT", run_id, "MALFORMED_RESULT"), _blank_sessions(strategy, planned)
    status = run.get("status") if isinstance(run.get("status"), str) and _STATUS.fullmatch(run["status"]) else "unavailable"
    config = run.get("config") if isinstance(run.get("config"), dict) else None
    result = run.get("result") if isinstance(run.get("result"), dict) else None
    if run.get("result") is not None and result is None:
        return _empty(strategy, status, run_id, "MALFORMED_RESULT"), _blank_sessions(strategy, planned)
    match = _matches(strategy, config, result, payload, view)
    if isinstance(result, dict):
        rows, codes = _sessions(study_id, strategy, result, planned, payload)
        codes.extend(_prompt_codes(result, payload))
        codes.extend(_input_codes(store, study_id, strategy, result, payload))
        codes.extend(_shape_codes(result))
    else:
        rows, codes = _blank_sessions(strategy, planned), []
    if not planned:
        codes.append("LINKAGE_UNVERIFIABLE")
    if match is False:
        code = "COMPARABILITY_MISMATCH"
    elif not isinstance(result, dict):
        code = "RESULT_UNAVAILABLE"
    else:
        code = _pick(codes)
    projected = project_result(result, secrets) or {}
    decisions = []
    for decision in projected.get("decisions") or []:
        item = dict(decision)
        item["calls"] = _present_calls(decision.get("calls"))
        decisions.append(item)
    metrics = projected.get("metrics")
    return {
        "strategy": strategy,
        "status": status,
        "origin": "source",
        "run_id": run_id,
        "complete": status == "COMPLETED",
        "not_complete": status != "COMPLETED",
        "zero_performance": False,
        "live_api_result": False,
        "result_label": _label(strategy, status, config, metrics, transport),
        "comparability": code,
        "config": project_config(config, secrets),
        "decisions": decisions,
        "trades": projected.get("trades") or [],
        "equity": projected.get("equity") or [],
        "metrics": metrics,
        "dataset": projected.get("dataset"),
        "engine_version": projected.get("engine_version"),
        "feature_version": projected.get("feature_version"),
        "return_basis": projected.get("return_basis"),
        "decision_origin": projected.get("decision_origin"),
        "coverage_status": projected.get("coverage_status"),
    }, rows


def _empty(strategy, status, run_id, code):
    return {
        "strategy": strategy, "status": status, "origin": "source", "run_id": run_id,
        "complete": False, "not_complete": True, "zero_performance": False, "live_api_result": False,
        "result_label": "unavailable", "comparability": code, "config": None, "decisions": [],
        "trades": [], "equity": [], "metrics": None, "dataset": None, "engine_version": None,
        "feature_version": None, "return_basis": None, "decision_origin": None, "coverage_status": None,
    }


def _matches(strategy, config, result, payload, view):
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    profile = payload.get("server_profile") if isinstance(payload.get("server_profile"), dict) else {}
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    if not isinstance(config, dict):
        return False
    agent = strategy in _AGENTS
    checks = [
        config.get("dataset_id") == request.get("dataset_id"),
        config.get("dataset_hash") == request.get("expected_dataset_hash"),
        config.get("start_date") == request.get("start_date"),
        config.get("end_date") == request.get("end_date"),
        _same_number(config.get("initial_cash"), request.get("initial_cash")),
        _same_number(config.get("fee_bps"), request.get("fee_bps")),
        _same_number(config.get("slippage_bps"), request.get("slippage_bps")),
        _same_number(config.get("max_exposure"), request.get("max_exposure")),
        config.get("prompt_version") == source.get("prompt_version"),
        config.get("llm_mode") != "replay",
        view.get("calendar") == "XNYS",
        policy.get("calendar") == "XNYS",
        policy.get("return_basis") == "price-only",
    ]
    if agent:
        checks.extend((config.get("llm_mode") == "api", config.get("model_provider") == profile.get("provider"), config.get("model_id") == profile.get("model_id")))
    else:
        checks.extend((config.get("llm_mode") == "mock", config.get("model_provider") is None, config.get("model_id") is None))
    if not all(checks):
        return False
    if result is None:
        return None
    origin = "api" if agent else "baseline"
    result_checks = [
        result.get("engine_version") == source.get("engine_version"),
        result.get("feature_version") == source.get("feature_version"),
        result.get("return_basis") == "price-only",
        result.get("decision_origin") == origin,
        result.get("decision_origin") != "replay",
    ]
    if "calendar" in result:
        result_checks.append(result.get("calendar") == "XNYS")
    dataset = result.get("dataset") if isinstance(result.get("dataset"), dict) else {}
    if dataset.get("sha256"):
        result_checks.append(dataset.get("sha256") == request.get("expected_dataset_hash"))
    if result.get("execution_policy_version") and policy.get("execution_policy_version"):
        result_checks.append(result.get("execution_policy_version") == policy.get("execution_policy_version"))
    return all(result_checks)


def _label(strategy, status, config, metrics, transport):
    calls = metrics.get("llm_calls") if isinstance(metrics, dict) else None
    if strategy not in _AGENTS and status == "COMPLETED" and calls == 0:
        return "baseline_zero_inference"
    if isinstance(config, dict) and config.get("llm_mode") == "mock":
        return "mock_not_live_api"
    if transport == "synthetic-fixture-not-live-api":
        return "fixture_transport_not_live_api"
    return "unavailable"


def _present_calls(calls):
    presented = []
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        item = dict(call)
        item["actual_usd"] = None
        item["actual_allocation"] = "NOT_ALLOCATED_FROM_AGGREGATE"
        if item.get("cost_status") == "unknown_reserved":
            item["cost_usd"] = None
        presented.append(item)
    return presented


def _parse_observation(raw):
    if raw is None:
        return None
    if not isinstance(raw, str):
        return _Malformed()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return _Malformed()
    return value if isinstance(value, dict) else _Malformed()


def _attempts(store, study_id, sources, secrets):
    """Durable slots are the liability source. Decision calls fill only ids the slots omit."""
    rows = []
    seen = set()
    for slot in store.list_slots(study_id):
        row = _slot_attempt(slot, secrets)
        if row.get("call_id"):
            seen.add(row["call_id"])
        rows.append(row)
    for source in sources:
        for decision in source["decisions"]:
            for call in decision.get("calls") or []:
                if not isinstance(call, dict) or call.get("call_id") in seen:
                    continue
                rows.append({
                    "strategy": source["strategy"], "stage": call.get("stage"), "attempt": call.get("attempt"),
                    "status": call.get("status"), "mode": call.get("mode"), "input_tokens": call.get("input_tokens"),
                    "output_tokens": call.get("output_tokens"), "latency_ms": call.get("latency_ms"),
                    "cost_status": call.get("cost_status"), "usage_estimate_usd": call.get("cost_usd"),
                    "reserved_usd": call.get("reserved_usd"), "actual_usd": None, "slot_id": None,
                    "call_id": call.get("call_id"), "decision_session": decision.get("decision_session"),
                    "execution_session": decision.get("execution_session"), "state": None, "outcome": None,
                    "usage_status": "dispatched_uncertain" if call.get("cost_status") == "unknown_reserved" else "known",
                    "sent": None,
                })
    return rows


def _slot_attempt(slot, secrets):
    observed = _parse_observation(slot.get("observation_json"))
    call = observed.get("call") if isinstance(observed, dict) else None
    projected = project_call(call, secrets) if isinstance(call, dict) else {}
    state = slot.get("state") if slot.get("state") in _SLOT_STATES else "unavailable"
    sent = observed.get("sent") if isinstance(observed, dict) and isinstance(observed.get("sent"), bool) else None
    usage = observed.get("usage_estimate_usd") if isinstance(observed, dict) else None
    if isinstance(observed, _Malformed):
        usage_status = "unavailable"
    elif state == "UNSTARTED" or sent is False:
        usage_status = "not_sent"
        sent = False
    elif state == "DISPATCHED" or (sent is True and usage is None):
        usage_status = "dispatched_uncertain"
    elif usage is not None:
        usage_status = "known"
    else:
        usage_status = "unavailable"
    unknown_usage = usage_status != "known"
    stage = slot.get("stage") if slot.get("stage") in _STAGES else None
    quiet = usage_status in {"not_sent", "unavailable"}
    return {
        "strategy": slot.get("strategy") if slot.get("strategy") in STRATEGIES else None,
        "stage": stage, "attempt": slot.get("attempt_no") if isinstance(slot.get("attempt_no"), int) else None,
        "status": None if quiet else projected.get("status"), "mode": None if quiet else projected.get("mode"),
        "input_tokens": None if unknown_usage else projected.get("input_tokens"),
        "output_tokens": None if unknown_usage else projected.get("output_tokens"),
        "latency_ms": None if quiet else projected.get("latency_ms"),
        "cost_status": None if quiet else projected.get("cost_status"),
        "usage_estimate_usd": None if unknown_usage else projected.get("cost_usd"),
        "reserved_usd": None if quiet else projected.get("reserved_usd"),
        "actual_usd": None, "slot_id": _uuid(slot.get("slot_id")), "call_id": _uuid(slot.get("call_id")),
        "decision_session": slot.get("decision_session") if isinstance(slot.get("decision_session"), str) else None,
        "execution_session": slot.get("execution_session") if isinstance(slot.get("execution_session"), str) else None,
        "state": state, "outcome": slot.get("outcome") if slot.get("outcome") in _OUTCOMES else None,
        "usage_status": usage_status, "sent": sent if isinstance(sent, bool) else None,
    }


def _semantic(store, strategy, source_run_id, replay_meta):
    replay_id = _uuid(replay_meta.get("run_id")) if isinstance(replay_meta, dict) else None
    view = semantic_replay_view(_load_result(store, source_run_id), _load_result(store, replay_id))
    view["strategy"] = strategy
    view["source_run_id"] = source_run_id
    view["replay_run_id"] = replay_id
    view["origin"] = "replay"
    return view


def _planned_dates(view, payload):
    sessions = view.get("sessions") if isinstance(view.get("sessions"), list) else None
    if not sessions and isinstance(payload.get("sessions"), list):
        sessions = payload.get("sessions")
    dates = []
    for session in sessions or []:
        day = session.get("decision") if isinstance(session, dict) else None
        if isinstance(day, str):
            dates.append(day)
    return dates


def _observation(strategy, day, kind, reason, applicability, recorded):
    return {
        "strategy": strategy, "decision_session": day, "kind": kind, "reason": reason,
        "applicability": applicability, "recorded_decision": recorded,
    }


def _blank_sessions(strategy, planned):
    return [_observation(strategy, day, "missing", "DECISION_NOT_RECORDED", "MISSING", False) for day in planned]


def _sessions(study_id, strategy, result, planned, payload):
    recorded = {}
    codes = []
    decisions = result.get("decisions") if isinstance(result.get("decisions"), list) else []
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    if not isinstance(result.get("decisions"), list):
        codes.append("MALFORMED_RESULT")
    for decision in decisions:
        if not isinstance(decision, dict):
            codes.append("MALFORMED_RESULT")
            continue
        day = decision.get("decision_session")
        if not isinstance(day, str):
            codes.append("LINKAGE_UNVERIFIABLE")
            continue
        recorded.setdefault(day, decision)
        decision_id = decision.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            codes.append("LINKAGE_UNVERIFIABLE")
        else:
            try:
                expected = str(uuid5(UUID(str(study_id)), strategy + ":" + day))
            except (ValueError, TypeError, AttributeError):
                codes.append("LINKAGE_UNVERIFIABLE")
            else:
                if decision_id != expected:
                    codes.append("STRATEGY_RESULT_LINK_MISMATCH")
        snapshot = decision.get("snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("dataset_hash"), str):
            codes.append("LINKAGE_UNVERIFIABLE")
        elif snapshot.get("dataset_hash") != request.get("expected_dataset_hash"):
            codes.append("COMPARABILITY_MISMATCH")
    definition = payload.get("strategy_definitions") if isinstance(payload.get("strategy_definitions"), dict) else {}
    rule = definition.get("buy_hold") if isinstance(definition.get("buy_hold"), dict) else {}
    entry = recorded.get(planned[0]) if planned else None
    holds = strategy == "buy_hold" and rule.get("rule") == _BUY_HOLD_RULE and isinstance(entry, dict) and entry.get("status") == "APPROVED"
    rows = []
    for index, day in enumerate(planned):
        if day in recorded:
            rows.append(_observation(strategy, day, "recorded_decision", None, "RECORDED", True))
        elif holds and index > 0:
            rows.append(_observation(strategy, day, "policy_hold", "BUY_HOLD_HOLDS_AFTER_ENTRY", "NOT_APPLICABLE", False))
        else:
            rows.append(_observation(strategy, day, "missing", "DECISION_NOT_RECORDED", "MISSING", False))
            codes.append("DECISION_SESSION_MISSING")
    return rows, codes


def _prompt_codes(result, payload):
    prompts = payload.get("prompts") if isinstance(payload.get("prompts"), dict) else {}
    profile = payload.get("server_profile") if isinstance(payload.get("server_profile"), dict) else {}
    codes = []
    for decision in result.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        for call in decision.get("calls") or []:
            if not isinstance(call, dict):
                codes.append("LINKAGE_UNVERIFIABLE")
                continue
            stage = call.get("stage")
            block = prompts.get(stage) if isinstance(stage, str) else None
            if not isinstance(block, dict) or not isinstance(call.get("prompt_hash"), str) or not isinstance(call.get("schema_hash"), str):
                codes.append("LINKAGE_UNVERIFIABLE")
            elif call.get("prompt_hash") != block.get("prompt_hash") or call.get("schema_hash") != block.get("schema_hash"):
                codes.append("PROMPT_HASH_MISMATCH")
            if profile.get("model_id") and call.get("model_id") != profile.get("model_id"):
                codes.append("COMPARABILITY_MISMATCH")
    return codes


def _input_codes(store, study_id, strategy, result, payload):
    """Compare stored inputs with the frozen numeric hash and role payload hash.

    ``numeric_input_hash`` is the canonical hash of the feature map. A role
    ``input_hash`` is the canonical hash of that role's payload, which adds
    predecessor outputs for Critic and Decision. Those digests are not interchangeable.
    """
    roles = _declared_roles(strategy, payload)
    frozen = _frozen_hashes(store, study_id)
    slots = [slot for slot in store.list_slots(study_id) if slot.get("strategy") == strategy]
    codes = []
    decisions = result.get("decisions") if isinstance(result.get("decisions"), list) else []
    for decision in decisions:
        if not isinstance(decision, dict) or not isinstance(decision.get("decision_session"), str):
            continue
        day = decision["decision_session"]
        numeric = _numeric_map(decision.get("snapshot"))
        expected = frozen.get((strategy, day))
        if numeric is None or expected is None:
            codes.append("LINKAGE_UNVERIFIABLE")
        elif _canonical(numeric) != expected:
            codes.append("INPUT_HASH_MISMATCH")
        if roles is None:
            codes.append("LINKAGE_UNVERIFIABLE")
            continue
        calls = decision.get("calls")
        if not isinstance(calls, list):
            codes.append("MALFORMED_RESULT" if "calls" in decision else "LINKAGE_UNVERIFIABLE")
            calls = []
        if not roles:
            if calls:
                codes.append("LINKAGE_UNVERIFIABLE")
            continue
        day_slots = [slot for slot in slots if slot.get("decision_session") == day]
        by_id = {slot.get("call_id"): slot for slot in day_slots if isinstance(slot.get("call_id"), str)}
        seen = set()
        outputs = {}
        for call in calls:
            codes.extend(_one_call(call, roles, by_id, day, numeric, outputs, seen, decision))
        codes.extend(_required_calls(decision, roles, calls, seen))
        for slot in day_slots:
            if slot.get("state") != "RECORDED" or slot.get("call_id") in seen:
                continue
            if slot.get("outcome") == "SUCCEEDED":
                codes.append("ROLE_EVIDENCE_MISSING")
            elif slot.get("outcome") == "FAILED":
                codes.extend(_omitted_failure(slot, roles, numeric, outputs))
            else:
                codes.append("LINKAGE_UNVERIFIABLE")
    if roles is None and not decisions:
        codes.append("LINKAGE_UNVERIFIABLE")
    return codes


def _declared_roles(strategy, payload):
    definitions = payload.get("strategy_definitions") if isinstance(payload.get("strategy_definitions"), dict) else {}
    block = definitions.get(strategy) if isinstance(definitions.get(strategy), dict) else None
    roles = block.get("roles") if isinstance(block, dict) else None
    if not isinstance(roles, list) or any(stage not in _STAGES for stage in roles):
        return None
    return tuple(roles)


def _frozen_hashes(store, study_id):
    found = {}
    for row in store.list_frozen_inputs(study_id):
        strategy, day, digest = row.get("strategy"), row.get("decision_session"), row.get("numeric_input_hash")
        if strategy in STRATEGIES and isinstance(day, str) and isinstance(digest, str) and _HEX64.fullmatch(digest):
            found[(strategy, day)] = digest
    return found


def _numeric_map(snapshot):
    from .models import FEATURE_NAMES
    if not isinstance(snapshot, dict):
        return None
    values = {}
    for key in FEATURE_NAMES:
        value = snapshot.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None
        values[key] = value
    return values


def _canonical(value):
    from .data import canonical_hash
    return canonical_hash(value)


def _role_payload(numeric, stage, outputs):
    from .research_service import _stage_payload

    class _Snapshot:
        def numeric_input(self):
            return dict(numeric)

    return _stage_payload(_Snapshot(), stage, outputs)


def _observation_call(slot):
    observed = _parse_observation(slot.get("observation_json"))
    if isinstance(observed, _Malformed):
        return observed
    if not isinstance(observed, dict) or not isinstance(observed.get("call"), dict):
        return _Malformed()
    return observed["call"]


def _one_call(call, roles, by_id, day, numeric, outputs, seen, decision):
    if not isinstance(call, dict):
        return ["LINKAGE_UNVERIFIABLE"]
    stage = call.get("stage")
    call_id = call.get("call_id")
    if stage not in roles or not isinstance(call_id, str) or call_id in seen or call_id not in by_id:
        return ["LINKAGE_UNVERIFIABLE"]
    slot = by_id[call_id]
    if slot.get("stage") != stage or slot.get("decision_session") != day:
        return ["LINKAGE_UNVERIFIABLE"]
    seen.add(call_id)
    codes = []
    ancestors = {name: outputs[name] for name in roles[:roles.index(stage)] if name in outputs}
    if numeric is None or any(name not in ancestors for name in roles[:roles.index(stage)]):
        codes.append("LINKAGE_UNVERIFIABLE")
    else:
        expected = _canonical(_role_payload(numeric, stage, ancestors))
        digest = call.get("input_hash")
        body = call.get("input")
        retained = _observation_call(slot)
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            codes.append("LINKAGE_UNVERIFIABLE")
        elif digest != expected or (isinstance(body, dict) and _canonical(body) != digest):
            codes.append("INPUT_HASH_MISMATCH")
        elif body is not None and not isinstance(body, dict):
            codes.append("LINKAGE_UNVERIFIABLE")
        elif isinstance(retained, _Malformed) or not isinstance(retained, dict):
            codes.append("LINKAGE_UNVERIFIABLE")
        elif retained.get("input_hash") != digest:
            codes.append("INPUT_HASH_MISMATCH")
    if call.get("status") == "VALIDATED":
        output = call.get("validated_output")
        key = {"analysis": "analysis_output", "critic": "critic_output", "decision": "decision_output"}[stage]
        if not isinstance(output, dict) or stage in outputs:
            codes.append("LINKAGE_UNVERIFIABLE")
        else:
            outputs[stage] = output
            recorded = decision.get(key)
            if isinstance(recorded, dict) and recorded != output:
                codes.append("LINKAGE_UNVERIFIABLE")
    return codes


def _required_calls(decision, roles, calls, seen):
    names = {"analysis": "analysis_output", "critic": "critic_output", "decision": "decision_output"}
    codes = []
    for stage in roles:
        validated = [call for call in calls if isinstance(call, dict) and call.get("stage") == stage and call.get("status") == "VALIDATED" and call.get("call_id") in seen]
        claimed = isinstance(decision.get(names[stage]), dict)
        approved = stage == "decision" and decision.get("status") == "APPROVED"
        if (claimed or approved) and len(validated) != 1:
            codes.append("ROLE_EVIDENCE_MISSING")
    return codes


def _omitted_failure(slot, roles, numeric, outputs):
    stage = slot.get("stage")
    if stage not in roles or numeric is None:
        return ["LINKAGE_UNVERIFIABLE"]
    ancestors = tuple(roles[:roles.index(stage)])
    if any(name not in outputs for name in ancestors):
        return ["LINKAGE_UNVERIFIABLE"]
    retained = _observation_call(slot)
    if isinstance(retained, _Malformed) or not isinstance(retained, dict):
        return ["LINKAGE_UNVERIFIABLE"]
    digest = retained.get("input_hash")
    expected = _canonical(_role_payload(numeric, stage, {name: outputs[name] for name in ancestors}))
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return ["LINKAGE_UNVERIFIABLE"]
    if digest != expected:
        return ["INPUT_HASH_MISMATCH"]
    return []


def _shape_codes(result):
    from .pilot import semantic_result
    try:
        semantic_result(result)
    except ValueError as exc:
        if str(exc) != "MALFORMED_NUMERIC":
            raise
        return ["MALFORMED_RESULT"]
    except (KeyError, TypeError):
        return ["MALFORMED_RESULT"]
    return []


def _load_result(store, run_id):
    if not run_id:
        return None
    try:
        run = store.get(run_id, True)
    except (ValueError, TypeError, KeyError):
        return _Malformed()
    result = run.get("result")
    if result is None:
        return None
    if not isinstance(result, dict):
        return _Malformed()
    return result


def _diagnostic(source, replay):
    found = {"max_abs_equity": None, "max_abs_shares": None, "hides_mismatch": False}
    if not isinstance(source, dict) or not isinstance(replay, dict):
        return found
    left, right = source.get("equity"), replay.get("equity")
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return found
    max_equity = 0.0
    max_shares = 0.0
    try:
        for one, other in zip(left, right):
            max_equity = max(max_equity, abs(float(one["equity"]) - float(other["equity"])))
            max_shares = max(max_shares, abs(float(one.get("shares", 0)) - float(other.get("shares", 0))))
    except (TypeError, ValueError, KeyError):
        return found
    found["max_abs_equity"] = max_equity
    found["max_abs_shares"] = max_shares
    return found


def _same_number(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    return isinstance(left, (int, float)) and isinstance(right, (int, float)) and left == right


def _hex(value, size):
    if not isinstance(value, str):
        return "unavailable"
    pattern = _HEX64 if size == 64 else _HEX40
    return value if pattern.fullmatch(value) else "unavailable"


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else "unavailable"
