"""Allowlist HTTP projections. Database rows are never rewritten."""
import re
from uuid import UUID

_SECRET_SPAN = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_\-]{8,}|bearer\s+[A-Za-z0-9._\-]{8,}|api[_-]?key\s*[:=]\s*\S+"
    r"|[A-Za-z]:[\\/][^\s\"']+|\\\\[^\s\"']+|/(?:Users|home|opt|var|tmp)/[^\s\"']+"
    r"|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
    r"|\b(?:synthetic-)?(?:key|cohort|group|account|binding|secret|credential|token)[-_][A-Za-z0-9\-]{3,}\b)"
)
_PRIVATE_DETAIL = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/]|\\\\|/(?:users|home|opt|var|tmp)/|sk-[A-Za-z0-9_\-]{8,}|bearer\s+|api[_-]?key|-----BEGIN|@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
)
_PUBLIC_DETAILS = {
    'configured dataset hash does not match selected dataset', 'Replay source must be completed',
    'Replay source strategy, dataset and prompt must match', 'Replay feature version mismatch',
    'Replay research group must match its source', 'comparison requires distinct run IDs',
    'all compared runs must be completed', 'runs must share dataset, period, capital, costs and engine',
    'agent comparisons must share decision origin, model version and prompt version',
    'Experiment has not completed', 'Experiment is still running', 'Unknown experiment', 'Unknown pilot',
    'No paid pilot budget has been initialized', 'unknown dataset id', 'dataset SHA-256 mismatch',
    'requested market sessions fall outside the dataset',
    'range has no weekly decision with enough warmup and a following execution session',
    'KuaiAPI application execution requires the bounded private pilot runner',
    'OpenAI application execution requires the default-disabled research service',
}
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,80}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_HEX40 = re.compile(r"^[a-f0-9]{40}$")
_BASENAME = re.compile(r"^[A-Za-z0-9._\-]{1,80}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")

FEATURE_KEYS = {"last_close", "ma20", "ma60", "rsi14", "atr14", "atr_pct14", "realized_vol20", "return_5d", "volume_ratio20"}
DATASET_KEYS = {"id", "kind", "symbol", "source", "start_date", "end_date", "rows", "sha256", "adjustment_mode", "currency", "frequency", "return_basis", "redistributable", "file", "acquired_at", "seed"}
CONFIG_KEYS = {"strategy_id", "dataset_id", "symbol", "start_date", "end_date", "initial_cash", "fee_bps", "slippage_bps", "max_exposure", "llm_mode", "model_provider", "model_id", "prompt_version", "dataset_hash", "replay_source_run_id", "max_llm_calls", "max_llm_budget_usd", "max_attempts_per_role", "research_group_id"}
RUN_KEYS = {"run_id", "status", "progress", "config", "metrics", "dataset", "created_at", "started_at", "finished_at", "code_commit", "code_dirty", "source_hash", "error_message", "decision_origin"}
METRIC_KEYS = {"total_return", "cagr", "sharpe_ratio", "sortino_ratio", "max_drawdown", "calmar_ratio", "trade_count", "turnover", "total_fees", "slippage_cost", "exposure_fraction", "final_equity", "valuation_sessions", "llm_calls", "agent_calls", "mock_calls", "llm_cost_usd", "uncertain_cost_reserve_usd", "llm_input_tokens", "llm_output_tokens", "llm_latency_ms", "reserved_or_actual_usd", "approved_decisions", "skipped_decisions", "rejected_decisions"}
TRADE_KEYS = {"trade_id", "decision_id", "execution_session", "side", "quantity", "reference_open", "execution_price", "notional", "fee", "slippage_cost", "cash_after", "shares_after", "weight_after", "executed_at"}
EQUITY_KEYS = {"session_date", "equity", "cash", "shares", "close", "drawdown"}
DECISION_KEYS = {"decision_id", "decision_session", "execution_session", "status", "reason_codes", "snapshot", "calls", "requested_weight", "approved_weight", "trade_id", "analysis_output", "critic_output", "decision_output"}
CALL_KEYS = {"call_id", "stage", "attempt", "mode", "model_id", "actual_model_id", "model_settings", "input", "input_hash", "prompt_hash", "prompt_version", "schema_hash", "instructions", "raw_response", "validated_output", "status", "error_code", "cost_usd", "input_tokens", "output_tokens", "reserved_usd", "latency_ms", "cost_status", "provider_attribution", "http_status"}
SETTING_KEYS = {"max_output_tokens", "store", "reasoning", "reasoning_effort", "temperature", "service_tier"}
COST_KEYS = {"attempts", "max_calls", "limit_usd", "usage_estimated_usd", "unknown_reserved_usd", "charged_or_reserved_usd", "confirmed_actual_usd", "actual_charge_scope", "invoiced_actual_usd"}
# Ledger strings stay unchanged. Public text is an allowlist that does not match _SECRET_SPAN.
_CHARGE_SCOPE = {
    "key-usage-aggregate-with-matching-calls": "aggregate-matched-calls",
    "individually-reconciled-calls": "individually-reconciled-calls",
}
RESULT_KEYS = {"schema_version", "engine_version", "feature_version", "return_basis", "decision_origin", "execution_policy_version", "dataset", "decisions", "trades", "model_ids", "provider_settings", "provider_attribution", "equity", "metrics", "coverage_status", "pause_state", "first_execution_session", "environment"}
PACKAGES = {"pydantic", "pandas", "numpy", "langgraph", "exchange-calendars", "fastapi", "uvicorn", "httpx"}


def public_detail(message, secrets=()) -> str:
    text = " ".join(_scrub(str(message), secrets).split())
    if text and all(_CODE.fullmatch(code) for code in text.split(',')):
        return text
    if text in _PUBLIC_DETAILS and not _PRIVATE_DETAIL.search(text):
        return text
    return "REQUEST_REJECTED"


def _scrub(value, secrets):
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4 and secret in value:
            value = value.replace(secret, "[redacted]")
    return _SECRET_SPAN.sub("[redacted]", value)


def _text(value, secrets):
    if value is None:
        return None
    return _scrub(str(value), secrets)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _codes(value, secrets):
    if not isinstance(value, (list, tuple)):
        return []
    return [_scrub(str(item), secrets) for item in value if isinstance(item, (str, int))]


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def project_dataset(value, secrets=()):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in DATASET_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "file":
            if isinstance(item, str) and _BASENAME.fullmatch(item):
                output[key] = item
            continue
        if key in {"rows", "seed"}:
            number = _number(item)
            if number is not None:
                output[key] = int(number)
            continue
        if key == "redistributable":
            if isinstance(item, bool):
                output[key] = item
            continue
        if key == "sha256" and isinstance(item, str) and _HEX64.fullmatch(item):
            output[key] = item
            continue
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            output[key] = _scrub(str(item), secrets) if isinstance(item, str) else item
    return output


def _settings(value, secrets):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in SETTING_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "reasoning" and isinstance(item, dict):
            effort = item.get("effort")
            if isinstance(effort, str):
                output[key] = {"effort": _scrub(effort, secrets)}
            continue
        if key == "store":
            if item is False:
                output[key] = False
            continue
        if isinstance(item, str):
            output[key] = _scrub(item, secrets)
        elif _number(item) is not None or item is None:
            output[key] = item
    return output


def _output_block(value, secrets):
    if not isinstance(value, dict):
        return None
    allowed = {"regime", "thesis", "evidence", "risk_level", "risk_flags", "counterarguments", "target_weight", "confidence", "rationale", "acknowledged_risks"}
    output = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if isinstance(item, str):
            output[key] = _scrub(item, secrets)
        elif isinstance(item, (list, tuple)):
            output[key] = [_scrub(str(part), secrets) for part in item if isinstance(part, str)]
        elif _number(item) is not None:
            output[key] = item
    return output


def _call_input(value, secrets):
    if not isinstance(value, dict):
        return {}
    output = {}
    features = value.get("features")
    if isinstance(features, dict):
        output["features"] = {key: _number(features[key]) for key in FEATURE_KEYS if key in features and _number(features[key]) is not None}
    for key in ("analysis", "critic"):
        if key in value:
            output[key] = _output_block(value[key], secrets)
    return output


def project_call(value, secrets=()):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in CALL_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == 'raw_response' and value.get('mode') != 'mock':
            output[key] = None  # Unvalidated paid/replayed text stays private in SQLite.
            continue
        if key == "model_settings":
            output[key] = _settings(item, secrets)
        elif key == "input":
            output[key] = _call_input(item, secrets)
        elif key == "validated_output":
            output[key] = _output_block(item, secrets)
        elif key == "provider_attribution":
            provider = item.get("provider") if isinstance(item, dict) else None
            output[key] = {"provider": _scrub(str(provider), secrets)} if isinstance(provider, str) else {"provider": None}
        elif key in {"input_hash", "prompt_hash", "schema_hash"} and isinstance(item, str) and _HEX64.fullmatch(item):
            output[key] = item
        elif key in {"cost_usd", "reserved_usd", "latency_ms", "input_tokens", "output_tokens", "attempt", "http_status"}:
            number = _number(item)
            if number is not None:
                output[key] = number
        elif isinstance(item, str):
            output[key] = _scrub(item, secrets)
    return output


def _snapshot(value, secrets):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in FEATURE_KEYS | {"dataset_hash", "feature_version", "symbol", "decision_session", "as_of"}:
        if key not in value:
            continue
        item = value[key]
        if key in FEATURE_KEYS:
            number = _number(item)
            if number is not None:
                output[key] = number
        elif key == "dataset_hash" and isinstance(item, str) and _HEX64.fullmatch(item):
            output[key] = item
        elif isinstance(item, str):
            output[key] = _scrub(item, secrets)
    return output


def project_decision(value, secrets=()):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in DECISION_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "snapshot":
            output[key] = _snapshot(item, secrets)
        elif key == "calls" and isinstance(item, list):
            output[key] = [project_call(call, secrets) for call in item if isinstance(call, dict)]
        elif key in {"analysis_output", "critic_output", "decision_output"}:
            output[key] = _output_block(item, secrets)
        elif key == "reason_codes":
            output[key] = _codes(item, secrets)
        elif key in {"requested_weight", "approved_weight"}:
            output[key] = _number(item)
        elif item is None or isinstance(item, str):
            output[key] = _text(item, secrets)
    return output


def project_trade(value, secrets=()):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in TRADE_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key in {"side", "execution_session", "trade_id", "decision_id", "executed_at"}:
            output[key] = _text(item, secrets)
        else:
            number = _number(item)
            if number is not None:
                output[key] = number
    return output


def project_equity(value, secrets=()):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in EQUITY_KEYS:
        if key not in value:
            continue
        if key == "session_date":
            output[key] = _text(value[key], secrets)
        else:
            number = _number(value[key])
            if number is not None:
                output[key] = number
    return output


def project_metrics(value, secrets=()):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in METRIC_KEYS:
        if key not in value:
            continue
        item = value[key]
        if item is None:
            output[key] = None
        else:
            number = _number(item)
            if number is not None:
                output[key] = number
    return output


def project_config(value, secrets=()):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in CONFIG_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "research_group_id":
            # Experiment isolation label, distinct from a provider account group.
            if isinstance(item, str) and _SAFE_ID.fullmatch(item) and not _PRIVATE_DETAIL.search(item):
                output[key] = _scrub(item, secrets)
            else:
                output[key] = None
            continue
        if key in {"replay_source_run_id"}:
            output[key] = _uuid(item)
            continue
        if key in {"initial_cash", "fee_bps", "slippage_bps", "max_exposure", "max_llm_calls", "max_llm_budget_usd", "max_attempts_per_role"}:
            number = _number(item)
            if number is not None:
                output[key] = number
            continue
        if key == "dataset_hash" and isinstance(item, str) and _HEX64.fullmatch(item):
            output[key] = item
            continue
        if item is None:
            output[key] = None
        elif isinstance(item, str):
            output[key] = _scrub(item, secrets)
    return output


def _environment(value, secrets):
    if not isinstance(value, dict):
        return None
    packages = value.get("packages") if isinstance(value.get("packages"), dict) else {}
    lock = value.get("uv_lock_sha256")
    return {"python": _text(value.get("python"), secrets) if isinstance(value.get("python"), str) else None,
            "packages": {name: _scrub(str(packages[name]), secrets) for name in PACKAGES if name in packages and isinstance(packages[name], str)},
            "uv_lock_sha256": lock if isinstance(lock, str) and _HEX64.fullmatch(lock) else None}


def project_result(value, secrets=()):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in RESULT_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "dataset":
            output[key] = project_dataset(item, secrets)
        elif key == "decisions" and isinstance(item, list):
            output[key] = [project_decision(row, secrets) for row in item if isinstance(row, dict)]
        elif key == "trades" and isinstance(item, list):
            output[key] = [project_trade(row, secrets) for row in item if isinstance(row, dict)]
        elif key == "equity" and isinstance(item, list):
            output[key] = [project_equity(row, secrets) for row in item if isinstance(row, dict)]
        elif key == "metrics":
            output[key] = project_metrics(item, secrets)
        elif key == "provider_settings":
            output[key] = _settings(item, secrets)
        elif key == "provider_attribution":
            provider = item.get("provider") if isinstance(item, dict) else None
            output[key] = {"provider": _scrub(provider, secrets)} if isinstance(provider, str) else None
        elif key == "environment":
            output[key] = _environment(item, secrets)
        elif key == "model_ids" and isinstance(item, list):
            output[key] = [_scrub(str(model), secrets) for model in item if isinstance(model, str)]
        elif isinstance(item, str):
            output[key] = _scrub(item, secrets)
        elif _number(item) is not None or item is None:
            output[key] = item
    return output


def project_run(value, secrets=(), include_result=False):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in RUN_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "config":
            output[key] = project_config(item, secrets)
        elif key == "metrics":
            output[key] = project_metrics(item, secrets)
        elif key == "dataset":
            output[key] = project_dataset(item, secrets)
        elif key == "code_commit":
            output[key] = item if isinstance(item, str) and _HEX40.fullmatch(item) else None
        elif key == "source_hash":
            output[key] = item if isinstance(item, str) and _HEX64.fullmatch(item) else None
        elif key == "error_message":
            output[key] = public_detail(item, secrets) if item else None
        elif key == "code_dirty":
            output[key] = bool(item)
        elif key == "progress":
            number = _number(item)
            output[key] = number if number is not None else 0
        elif isinstance(item, str):
            output[key] = _scrub(item, secrets)
        elif item is None:
            output[key] = None
    if include_result:
        output["result"] = project_result(value.get("result"), secrets)
    return output


def project_event(value, secrets=()):
    if not isinstance(value, dict):
        return {}
    kind = value.get("event_type") if isinstance(value.get("event_type"), str) else "unknown"
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
    if kind in {"decision"}:
        projected = project_decision(payload, secrets)
    elif kind in {"agent_call"}:
        projected = project_call(payload, secrets)
        if isinstance(payload.get("decision_id"), str):
            projected["decision_id"] = _scrub(payload["decision_id"], secrets)
    elif kind == "trade":
        projected = project_trade(payload, secrets)
    elif kind == "equity":
        projected = project_equity(payload, secrets)
    else:
        projected = None
    event_id = value.get("event_id")
    return {"event_type": _scrub(kind, secrets), "event_id": _text(event_id, secrets) if isinstance(event_id, str) else None,
            "sequence": _number(value.get("sequence")), "payload": projected}


def project_events(values, secrets=()):
    if not isinstance(values, list):
        return []
    return [project_event(item, secrets) for item in values if isinstance(item, dict)]


def project_comparison(value, secrets=()):
    if not isinstance(value, dict):
        return {"runs": [], "curves": [], "return_basis": "price-only"}
    runs = [project_run(row, secrets) for row in value.get("runs", []) if isinstance(row, dict)]
    curves = []
    for curve in value.get("curves", []) if isinstance(value.get("curves"), list) else []:
        if not isinstance(curve, dict):
            continue
        curves.append({"run_id": _text(curve.get("run_id"), secrets), "strategy_id": _text(curve.get("strategy_id"), secrets),
                       "equity": [project_equity(point, secrets) for point in curve.get("equity", []) if isinstance(point, dict)]})
    return {"runs": runs, "curves": curves, "return_basis": "price-only"}


def project_cost(value, secrets=()):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in COST_KEYS:
        if key not in value:
            continue
        item = value[key]
        if item is None:
            output[key] = None
        elif isinstance(item, str):
            output[key] = _CHARGE_SCOPE.get(item, "unavailable") if key == "actual_charge_scope" else None
        else:
            number = _number(item)
            if number is not None:
                output[key] = number
    output["invoiced_actual_usd"] = None
    return output


def project_bars(payload, secrets=()):
    if not isinstance(payload, dict):
        return {"dataset": None, "bars": []}
    bars = []
    for row in payload.get("bars", []) if isinstance(payload.get("bars"), list) else []:
        if not isinstance(row, dict):
            continue
        item = {}
        for key in ("session_date", "open", "high", "low", "close", "volume", "ma20", "ma60", "rsi14", "realized_vol20"):
            if key not in row:
                continue
            if key == "session_date":
                item[key] = _text(row[key], secrets)
            elif row[key] is None:
                item[key] = None
            else:
                number = _number(row[key])
                if number is not None:
                    item[key] = number
        bars.append(item)
    return {"dataset": project_dataset(payload.get("dataset"), secrets), "bars": bars}


_DENY = {"observation_json", "canonical_payload", "plan_json", "payload_json", "private_evidence", "key_ref", "cohort_ref", "group_ref", "binding_ref", "group", "endpoint", "authorization", "envelope", "raw_provider_response", "namespace", "server_profile", "debug", "api_key", "token", "secret", "password", "extras", "machine_path", "account", "account_id"}


def _sanitize(value, secrets):
    if isinstance(value, str):
        return _scrub(value, secrets)
    if isinstance(value, list):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item, secrets) for key, item in value.items() if key not in _DENY}
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return _scrub(str(value), secrets)


def project_study(report, secrets=()):
    if not isinstance(report, dict):
        return {}
    def runs(value):
        output = {}
        if not isinstance(value, dict):
            return output
        for name, item in value.items():
            if not isinstance(item, dict):
                continue
            output[_scrub(str(name), secrets)] = {"run_id": _text(item.get("run_id"), secrets), "status": _text(item.get("status"), secrets), "origin": item.get("origin") if item.get("origin") in {"source", "replay"} else None}
        return output
    evidence = []
    for item in report.get("attempt_evidence") or []:
        if isinstance(item, dict) and isinstance(item.get("call_id"), str) and isinstance(item.get("evidence_sha256"), str) and _HEX64.fullmatch(item["evidence_sha256"]):
            evidence.append({"call_id": _scrub(item["call_id"], secrets), "evidence_sha256": item["evidence_sha256"]})
    coverage = {}
    if isinstance(report.get("coverage"), dict):
        coverage = {_scrub(str(key), secrets): _scrub(str(value), secrets) for key, value in report["coverage"].items()}
    progress = report.get("progress") if isinstance(report.get("progress"), dict) else {}
    body = {"study_id": _text(report.get("study_id"), secrets), "state": _text(report.get("state"), secrets), 'worker_busy': bool(report.get('worker_busy')),
            "reason": public_detail(report["reason"], secrets) if report.get("reason") else None,
            "plan": report.get("plan") if isinstance(report.get("plan"), dict) else {},
            "cost": project_cost(report.get("cost"), secrets), "coverage": coverage,
            "source_runs": runs(report.get("source_runs")), "replay_runs": runs(report.get("replay_runs")),
            "attempt_evidence": evidence, "event_types": [_scrub(str(item), secrets) for item in report.get("event_types") or [] if isinstance(item, str)],
            "progress": {"total_slots": _number(progress.get("total_slots")) or 0, "recorded_slots": _number(progress.get("recorded_slots")) or 0,
                         "by_state": {str(key): int(value) for key, value in (progress.get("by_state") or {}).items() if isinstance(key, str) and _number(value) is not None}},
            "fixture_transport_posts": _number(report.get("fixture_transport_posts")) or 0,
            "replay_posts": _number(report.get("replay_posts")),
            "replay_equal": {str(key): bool(value) for key, value in report["replay_equal"].items()} if isinstance(report.get("replay_equal"), dict) else None,
            "synthetic_fixture": True, "real_spending_authorized": False, "execution_authorized": False,
            "labels": [item for item in report.get("labels") or [] if isinstance(item, str)]}
    return _sanitize(body, secrets)


def project_capabilities(value):
    modes = [mode for mode in (value or {}).get("modes", []) if mode in {"mock", "replay"}]
    return {"modes": modes or ["mock", "replay"], "api": {"available": False, "provider": None, "model_id": None},
            "engine_version": _text((value or {}).get("engine_version"), ()) if isinstance((value or {}).get("engine_version"), str) else None,
            "feature_version": _text((value or {}).get("feature_version"), ()) if isinstance((value or {}).get("feature_version"), str) else None,
            "research_only": True, "return_basis": "price-only", "calendar": "XNYS", "broker_enabled": False, "live_provider": False}
