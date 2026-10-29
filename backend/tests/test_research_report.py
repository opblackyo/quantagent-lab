"""Safe study report oracles. Synthetic markers only; no live provider."""
import json
import math
import socket
from copy import deepcopy

from test_research_http import MARKERS, assert_clean, opened, request_for, start, wait_for

from app.research_report import LIMITATIONS, semantic_replay_view


def _decision(**updates):
    row = {
        "decision_session": "2023-01-06", "execution_session": "2023-01-09",
        "analysis_output": None, "critic_output": None, "decision_output": None,
        "requested_weight": 1.0, "approved_weight": 1.0, "status": "APPROVED", "reason_codes": [],
    }
    row.update(updates)
    return row


def _result(equity=100.0, status="APPROVED"):
    return {
        "decisions": [_decision(status=status)],
        "trades": [{"execution_session": "2023-01-09", "side": "BUY", "quantity": 1.0, "reference_open": 10.0,
                    "execution_price": 10.0, "notional": 10.0, "fee": 0.0, "slippage_cost": 0.0,
                    "cash_after": 90.0, "shares_after": 1.0, "weight_after": 1.0, "executed_at": "2023-01-09T14:30:00+00:00",
                    "trade_id": "trade-a", "decision_id": "decision-a"}],
        "equity": [{"session_date": "2023-01-09", "equity": equity, "cash": 90.0, "shares": 1.0, "close": 10.0}],
        "metrics": {"total_return": 0.0, "final_equity": equity, "trade_count": 1, "llm_calls": 4, "llm_cost_usd": 2.0},
    }


def test_semantic_equality_ignores_ids_and_diagnostics_cannot_hide_a_mismatch():
    original = _result()
    renamed = deepcopy(original)
    renamed["trades"][0]["trade_id"] = "trade-b"
    renamed["trades"][0]["decision_id"] = "decision-b"
    same = semantic_replay_view(original, renamed)
    assert same["equal"] is True and same["reason"] is None and same["diagnostic"]["hides_mismatch"] is False

    shifted = deepcopy(original)
    shifted["equity"][0]["equity"] = math.nextafter(float(original["equity"][0]["equity"]), math.inf)
    dusty = semantic_replay_view(original, shifted)
    assert dusty["equal"] is False and dusty["reason"] == "SEMANTIC_MISMATCH"
    assert 0 < dusty["diagnostic"]["max_abs_equity"] < 1e-9
    assert dusty["diagnostic"]["hides_mismatch"] is False

    skipped = deepcopy(original)
    skipped["decisions"][0]["status"] = "SKIPPED"
    skipped["decisions"][0]["reason_codes"] = ["cancelled"]
    assert semantic_replay_view(original, skipped)["equal"] is False
    assert semantic_replay_view(original, None)["reason"] == "RESULT_UNAVAILABLE"


def _snapshot(store):
    with store._read() as conn:
        studies = [tuple(row) for row in conn.execute("SELECT study_id,state,reason FROM research_studies ORDER BY study_id")]
        attempts = [tuple(row) for row in conn.execute(
            "SELECT call_id,status,usage_estimate,reserved,actual_charge FROM pilot_attempts ORDER BY call_id")]
        counts = tuple(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in (
            "research_events", "runs", "research_slots", "research_children"))
    return studies, attempts, counts


def _export(client, study_id):
    response = client.get(f"/api/v1/research/studies/{study_id}/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == 'attachment; filename="research-study.json"'
    assert_clean(response.text)
    assert "input_usd_per_million" not in response.text
    assert "https://kuaiapi.net/" not in response.text
    assert "https://api.openai.com/" not in response.text
    return response


def _finish(client, dataset_hash, profile, key, states):
    planned, started = start(client, request_for(profile, dataset_hash), key)
    assert started.status_code == 202, started.text
    study_id = started.json()["study_id"]
    final = wait_for(client, study_id, states)
    return study_id, planned, final


def test_success_export_is_read_only_and_replay_stays_exact(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    study_id, _planned, final = _finish(client, dataset_hash, "fixture-success", "report-success", {"COMPLETED"})
    assert final["state"] == "COMPLETED"
    before = _snapshot(runtime.store)
    posts = runtime.posts_for(study_id)

    def denied(*_args, **_kwargs):
        raise AssertionError("export mutated or replayed")

    monkeypatch.setattr(runtime, "replay", denied)
    monkeypatch.setattr(runtime, "resume", denied)
    monkeypatch.setattr(runtime, "reconcile", denied)
    monkeypatch.setattr(runtime, "cancel", denied)
    monkeypatch.setattr("app.research_service.replay_study", denied)
    monkeypatch.setattr("app.research_runtime.replay_study", denied)
    exported = _export(client, study_id)
    body = exported.json()
    report = body["report"]
    assert "report" not in client.get(f"/api/v1/research/studies/{study_id}").json()
    assert _snapshot(runtime.store) == before and runtime.posts_for(study_id) == posts
    assert report["read_only"] is True and report["mutates_admission"] is False
    assert report["browser_tariff_authoritative"] is False and report["browser_funding_approval"] is False
    assert report["coverage"]["denominator"] == 4 and report["coverage"]["failures_remain_in_denominator"] is True
    assert [item["strategy"] for item in report["sources"]] == ["buy_hold", "ma_crossover", "single_agent", "multi_agent"]
    assert [item["status"] for item in report["coverage"]["strategies"]] == ["COMPLETED"] * 4
    assert report["comparability"]["ok"] is True and report["comparability"]["reason"] is None
    assert report["cost"]["invoiced_actual_usd"] is None
    assert report["cost"]["baseline_calls"] == 0 and report["cost"]["baseline_cost_usd"] == 0
    assert report["cost"]["baseline_scope"] == "planned_zero_inference"
    assert report["cost"]["per_call_actual_allocation_usd"] is None
    assert report["cost"]["actual_charge_scope"] == "aggregate-matched-calls"
    assert "key-usage-aggregate-with-matching-calls" not in exported.text
    assert report["cost"]["per_call_actual_reason"] == "NOT_ALLOCATED_FROM_AGGREGATE"
    assert report["coverage"]["strategy_denominator"] == 4 and report["coverage"]["verdict"] == "COMPLETE"
    assert report["coverage"]["session_denominator"] > 4 and report["coverage"]["missing_sessions"] == 0
    holds = [item for item in report["coverage"]["observations"] if item["strategy"] == "buy_hold" and item["kind"] == "policy_hold"]
    assert holds and all(item["reason"] == "BUY_HOLD_HOLDS_AFTER_ENTRY" and item["applicability"] == "NOT_APPLICABLE" and item["recorded_decision"] is False for item in holds)
    assert len(next(item["decisions"] for item in report["sources"] if item["strategy"] == "buy_hold")) == 1
    assert report["cost"]["model_cost_in_trading_equity"] is False
    assert {"usage_estimated_usd", "confirmed_actual_usd", "unknown_reserved_usd"} <= set(report["cost"])
    assert report["success_criterion"] is None and report["profit_is_not_success"] is True
    for sentence in LIMITATIONS:
        assert sentence in report["limitations"]
    versions = report["versions"]
    assert versions["transport"] == "synthetic-fixture-not-live-api"
    assert versions["calendar"] == "XNYS" and len(versions["dataset_hash"]) == 64
    assert versions["manifest_hash"] != "unavailable" and versions["prompt_version"] != "unavailable"
    assert versions["schema_hashes"]["decision"] != "unavailable"
    assert versions["provider"] == "kuaiapi" and versions["identity_scope"] == "gateway-reported-label-only"
    for source in report["sources"]:
        assert source["origin"] == "source" and source["live_api_result"] is False and source["zero_performance"] is False
        assert source["decisions"] and source["equity"] and source["metrics"]["final_equity"] == source["equity"][-1]["equity"]
        assert "total_return" in source["metrics"] and "exposure_fraction" in source["metrics"]
    baselines = [item for item in report["sources"] if item["strategy"] in {"buy_hold", "ma_crossover"}]
    assert all(item["result_label"] == "baseline_zero_inference" and item["metrics"]["llm_calls"] == 0 for item in baselines)
    multi = next(item for item in report["sources"] if item["strategy"] == "multi_agent")
    stages = {call["stage"] for decision in multi["decisions"] for call in decision["calls"]}
    assert {"analysis", "critic", "decision"} <= stages
    assert report["attempts"] and all(row["actual_usd"] is None for row in report["attempts"])
    assert any(row["latency_ms"] is not None and row["input_tokens"] is not None for row in report["attempts"])
    monkeypatch.undo()

    import app.agents as agents
    import app.budget_ledger as budget_ledger
    import app.gateway as gateway
    import app.pilot as pilot

    def blocked(*_args, **_kwargs):
        raise AssertionError("replay opened a provider, budget, or socket")

    monkeypatch.setattr(agents.OpenAIProvider, "__init__", blocked)
    monkeypatch.setattr(gateway.KuaiAPIProvider, "__init__", blocked)
    monkeypatch.setattr(pilot.PilotBudget, "__init__", blocked)
    monkeypatch.setattr(budget_ledger, "reserve", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    replayed = client.post(f"/api/v1/research/studies/{study_id}/replay", json={})
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replay_posts"] == 0 and runtime.posts_for(study_id) == posts
    assert_clean(replayed.text)
    replay_report = _export(client, study_id).json()["report"]
    assert _snapshot(runtime.store)[1] == before[1]
    assert all(item["equal"] is True and item["reason"] is None for item in replay_report["semantic_replay"])
    assert all(item["replay_run_id"] and item["replay_run_id"] != item["source_run_id"] for item in replay_report["semantic_replay"])
    source_ids = {item["strategy"]: item["run_id"] for item in replay_report["sources"]}
    assert source_ids == {name: item["run_id"] for name, item in final["source_runs"].items()}

    buy_hold = source_ids["buy_hold"]
    single = source_ids["single_agent"]
    multi_id = source_ids["multi_agent"]
    replay_buy = next(item["replay_run_id"] for item in replay_report["semantic_replay"] if item["strategy"] == "buy_hold")
    with runtime.store.connect() as conn:
        saved = json.loads(conn.execute("SELECT result_json FROM runs WHERE run_id=?", (replay_buy,)).fetchone()[0])
        saved["decisions"][0]["reason_codes"] = list(saved["decisions"][0].get("reason_codes") or []) + ["SYNTHETIC_MISMATCH"]
        conn.execute("UPDATE runs SET result_json=? WHERE run_id=?", (json.dumps(saved), replay_buy))
    mismatched = _export(client, study_id).json()["report"]
    dusty = next(item for item in mismatched["semantic_replay"] if item["strategy"] == "buy_hold")
    assert dusty["equal"] is False and dusty["reason"] == "SEMANTIC_MISMATCH"
    assert dusty["diagnostic"]["max_abs_equity"] == 0 and dusty["diagnostic"]["hides_mismatch"] is False
    assert mismatched["comparability"]["ok"] is True

    with runtime.store.connect() as conn:
        config = json.loads(conn.execute("SELECT config_json FROM runs WHERE run_id=?", (buy_hold,)).fetchone()[0])
        config["fee_bps"] = 10
        conn.execute("UPDATE runs SET config_json=? WHERE run_id=?", (json.dumps(config), buy_hold))
        agent = json.loads(conn.execute("SELECT config_json FROM runs WHERE run_id=?", (single,)).fetchone()[0])
        agent["llm_mode"] = "replay"
        conn.execute("UPDATE runs SET config_json=? WHERE run_id=?", (json.dumps(agent), single))
        poisoned = json.loads(conn.execute("SELECT result_json FROM runs WHERE run_id=?", (multi_id,)).fetchone()[0])
        poisoned["decisions"][0]["analysis_output"]["thesis"] = "fixture-binding-ref sk-syntheticfixturesecret ops@example.com C:\\Users\\fixture\\secret"
        poisoned["extras"] = {"api_key": "sk-syntheticfixturesecret", "machine_path": "C:\\Users\\fixture\\secret\\db.sqlite3"}
        poisoned.setdefault("provider_attribution", {})["group"] = "fixture-group-marker"
        conn.execute("UPDATE runs SET result_json=? WHERE run_id=?", (json.dumps(poisoned), multi_id))
    compared = client.post("/api/v1/experiments/compare", json={"run_ids": [buy_hold, source_ids["ma_crossover"]]})
    assert compared.status_code == 422 and compared.json()["detail"] == "runs must share dataset, period, capital, costs and engine"
    assert_clean(compared.text)
    safe = _export(client, study_id)
    changed = safe.json()["report"]
    assert changed["comparability"] == {
        "ok": False, "reason": "COMPARABILITY_MISMATCH",
        "mismatched_strategies": ["buy_hold", "single_agent"],
        "affected_strategies": [
            {"strategy": "buy_hold", "reason": "COMPARABILITY_MISMATCH"},
            {"strategy": "single_agent", "reason": "COMPARABILITY_MISMATCH"},
            {"strategy": "multi_agent", "reason": "LINKAGE_UNVERIFIABLE"},
        ],
        "replay_does_not_replace_source": True,
    }
    assert {item["strategy"]: item["run_id"] for item in changed["sources"]} == source_ids
    assert next(item["replay_run_id"] for item in changed["semantic_replay"] if item["strategy"] == "single_agent") != single
    legacy = client.get(f"/api/v1/experiments/{multi_id}/export")
    assert legacy.status_code == 200
    assert_clean(legacy.text)
    for marker in MARKERS:
        assert marker not in safe.text and marker not in legacy.text
    listed = client.get("/api/v1/experiments")
    assert listed.status_code == 200 and assert_clean(listed.text) is None

    with runtime.store.connect() as conn:
        conn.execute("UPDATE runs SET result_json=? WHERE run_id=?", ("{", source_ids["ma_crossover"]))
    broken = _export(client, study_id).json()["report"]
    malformed = next(item for item in broken["sources"] if item["strategy"] == "ma_crossover")
    assert malformed["run_id"] == source_ids["ma_crossover"] and malformed["comparability"] == "MALFORMED_RESULT"
    assert malformed["metrics"] is None and malformed["zero_performance"] is False
    assert broken["comparability"]["ok"] is False and broken["comparability"]["reason"] == "COMPARABILITY_MISMATCH"
    raw = client.get(f"/api/v1/research/studies/{study_id}/export").text
    assert "Traceback" not in raw and "JSONDecode" not in raw and "Expecting value" not in raw


def test_paused_unknown_and_cancel_stay_in_the_denominator(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    study_id, _planned, paused = _finish(client, dataset_hash, "fixture-timeout", "report-timeout", {"PAUSED_BILLING"})
    assert paused["state"] == "PAUSED_BILLING"
    unknown = paused["cost"]["unknown_reserved_usd"]
    assert unknown > 0
    held = _export(client, study_id).json()
    report = held["report"]
    assert report["cost"]["unknown_reserved_usd"] == unknown
    assert report["cost"]["actual_charge_scope"] == "individually-reconciled-calls"
    assert report["cost"]["confirmed_actual_usd"] != unknown
    assert report["cost"]["invoiced_actual_usd"] is None
    assert report["cost"]["per_call_actual_allocation_usd"] is None
    assert report["cost"]["usage_estimated_usd"] is not None
    assert report["coverage"]["denominator"] == 4 and len(report["sources"]) == 4
    assert any(item["status"] != "COMPLETED" for item in report["sources"])
    assert all(item["zero_performance"] is False and item["origin"] == "source" for item in report["sources"])
    for source in report["sources"]:
        for decision in source["decisions"]:
            assert decision["status"] in {"APPROVED", "SKIPPED", "REJECTED"}
            for call in decision["calls"]:
                if call.get("cost_status") == "unknown_reserved":
                    assert call["cost_usd"] is None and call["actual_usd"] is None
    monkeypatch.setattr(runtime, "replay", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("export replay")))
    assert _export(client, study_id).status_code == 200
    cancelled = client.post(f"/api/v1/research/studies/{study_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    final = wait_for(client, study_id, {"CANCELLED"})
    assert final["cost"]["unknown_reserved_usd"] == unknown
    after = _export(client, study_id).json()["report"]
    assert after["cost"]["unknown_reserved_usd"] == unknown
    assert after["coverage"]["denominator"] == 4
    assert any(item["status"] != "COMPLETED" for item in after["sources"])
    assert runtime.posts_for(study_id) == 1


def _tripwire(monkeypatch):
    import app.agents as agents
    import app.budget_ledger as budget_ledger
    import app.gateway as gateway
    import app.pilot as pilot

    def blocked(*_args, **_kwargs):
        raise AssertionError("report or replay opened a provider, budget, or socket")

    monkeypatch.setattr(agents.OpenAIProvider, "__init__", blocked)
    monkeypatch.setattr(gateway.KuaiAPIProvider, "__init__", blocked)
    monkeypatch.setattr(pilot.PilotBudget, "__init__", blocked)
    monkeypatch.setattr(budget_ledger, "reserve", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def _guarded_export(monkeypatch, client, study_id):
    _tripwire(monkeypatch)
    try:
        return _export(client, study_id)
    finally:
        monkeypatch.undo()


def _ledger_bytes(store):
    with store._read() as conn:
        attempts = [tuple(row) for row in conn.execute(
            "SELECT call_id,pilot_id,reserved,usage_estimate,status,actual_charge FROM pilot_attempts ORDER BY call_id")]
        budgets = [tuple(row) for row in conn.execute("SELECT pilot_id,manifest_json FROM pilot_budgets ORDER BY pilot_id")]
        receipts = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_usage_reconciliations'").fetchone():
            receipts = [tuple(row) for row in conn.execute(
                "SELECT pilot_id,receipt_json FROM pilot_usage_reconciliations ORDER BY pilot_id")]
        events = conn.execute("SELECT COUNT(*) FROM research_events").fetchone()[0]
    return attempts, budgets, receipts, events


def _put_result(store, run_id, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    with store.connect() as conn:
        conn.execute("UPDATE runs SET result_json=? WHERE run_id=?", (text, run_id))


def _load_result(store, run_id):
    with store.connect() as conn:
        return json.loads(conn.execute("SELECT result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])


def test_bool_null_and_nonfinite_numbers_are_not_equal():
    original = _result()
    boolean = deepcopy(original)
    boolean["decisions"][0]["approved_weight"] = True
    boolean["decisions"][0]["requested_weight"] = True
    compared = semantic_replay_view(original, boolean)
    assert compared["equal"] is False and compared["reason"] == "MALFORMED_RESULT"
    assert compared["diagnostic"] == {"max_abs_equity": None, "max_abs_shares": None, "hides_mismatch": False}
    both = deepcopy(boolean)
    assert semantic_replay_view(boolean, both)["equal"] is False

    for path in (
        ("trades", 0, "quantity"),
        ("equity", 0, "cash"),
        ("equity", 0, "shares"),
        ("metrics", "total_return"),
    ):
        malformed = deepcopy(original)
        cursor = malformed
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = True
        assert semantic_replay_view(original, malformed)["equal"] is False

    for bad in (None, [], {"total_return": True}):
        broken = deepcopy(original)
        broken["metrics"] = bad
        view = semantic_replay_view(original, broken)
        assert view["equal"] is False and view["reason"] == "MALFORMED_RESULT"
        assert view["diagnostic"]["hides_mismatch"] is False and view["diagnostic"]["max_abs_equity"] is None

    for bad in (float("nan"), float("inf"), None):
        broken = deepcopy(original)
        broken["equity"][0]["cash"] = bad
        assert semantic_replay_view(original, broken)["reason"] == "MALFORMED_RESULT"
    shaped = deepcopy(original)
    shaped["decisions"] = {"approved_weight": 1.0}
    assert semantic_replay_view(original, shaped)["reason"] == "MALFORMED_RESULT"
    skipped = deepcopy(original)
    skipped["decisions"][0]["approved_weight"] = None
    skipped["decisions"][0]["requested_weight"] = None
    skipped["decisions"][0]["status"] = "SKIPPED"
    assert semantic_replay_view(skipped, deepcopy(skipped))["equal"] is True


def test_charge_scope_is_an_allowlisted_enum():
    from app.public_projection import project_cost

    aggregate = project_cost({
        "actual_charge_scope": "key-usage-aggregate-with-matching-calls",
        "confirmed_actual_usd": 0.25, "unknown_reserved_usd": 1.5, "invoiced_actual_usd": 9,
    }, ())
    assert aggregate["actual_charge_scope"] == "aggregate-matched-calls"
    assert aggregate["invoiced_actual_usd"] is None
    assert aggregate["confirmed_actual_usd"] == 0.25 and aggregate["unknown_reserved_usd"] == 1.5
    individual = project_cost({"actual_charge_scope": "individually-reconciled-calls"}, ())
    assert individual["actual_charge_scope"] == "individually-reconciled-calls"
    poisoned = project_cost({"actual_charge_scope": "synthetic-key-marker-secret"}, ())
    rendered = json.dumps(poisoned)
    assert poisoned["actual_charge_scope"] == "unavailable"
    assert "synthetic-key-marker-secret" not in rendered and "key-" not in rendered


def test_comparability_follows_linked_evidence_and_session_coverage(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    study_id, _planned, final = _finish(client, dataset_hash, "fixture-success", "report-evidence", {"COMPLETED"})
    source_ids = {name: item["run_id"] for name, item in final["source_runs"].items()}
    before = _snapshot(runtime.store)
    posts = runtime.posts_for(study_id)
    ledger = _ledger_bytes(runtime.store)
    healthy = _guarded_export(monkeypatch, client, study_id).json()["report"]
    gaps = []
    buy = next(item for item in healthy["sources"] if item["strategy"] == "buy_hold")
    holds = [item for item in healthy["coverage"].get("observations", []) if item.get("strategy") == "buy_hold" and item.get("kind") == "policy_hold"]
    if len(buy["decisions"]) != 1 or buy["status"] != "COMPLETED":
        gaps.append("buy-hold-run")
    if not holds or any(item.get("reason") != "BUY_HOLD_HOLDS_AFTER_ENTRY" or item.get("applicability") != "NOT_APPLICABLE" or item.get("recorded_decision") for item in holds):
        gaps.append("policy-hold")
    if healthy["coverage"].get("strategy_denominator") != 4 or healthy["coverage"].get("session_denominator", 0) <= 4:
        gaps.append("denominators")
    if healthy["coverage"].get("verdict") != "COMPLETE" or healthy["coverage"].get("missing_sessions") != 0:
        gaps.append("healthy-coverage")
    if healthy["comparability"]["ok"] is not True:
        gaps.append("healthy-comparable")
    if healthy["cost"].get("actual_charge_scope") != "aggregate-matched-calls":
        gaps.append("aggregate-scope")
    if "key-usage-aggregate-with-matching-calls" in json.dumps(healthy):
        gaps.append("raw-scope-leaked")
    originals = {name: _load_result(runtime.store, run_id) for name, run_id in source_ids.items()}

    _put_result(runtime.store, source_ids["ma_crossover"], originals["buy_hold"])
    swapped = _guarded_export(monkeypatch, client, study_id).json()["report"]
    ma = next(item for item in swapped["sources"] if item["strategy"] == "ma_crossover")
    affected = swapped["comparability"].get("affected_strategies") or []
    if ma["comparability"] != "STRATEGY_RESULT_LINK_MISMATCH" or swapped["comparability"]["ok"] or not any(
            item.get("strategy") == "ma_crossover" and item.get("reason") == "STRATEGY_RESULT_LINK_MISMATCH" for item in affected):
        gaps.append("swapped-result")
    if ma["metrics"] is None:
        gaps.append("swapped-metrics-erased")
    _put_result(runtime.store, source_ids["ma_crossover"], originals["ma_crossover"])

    hashed = deepcopy(originals["single_agent"])
    hashed["decisions"][0]["calls"][0]["prompt_hash"] = "0" * 64
    _put_result(runtime.store, source_ids["single_agent"], hashed)
    altered = _guarded_export(monkeypatch, client, study_id).json()["report"]
    single = next(item for item in altered["sources"] if item["strategy"] == "single_agent")
    if single["comparability"] != "PROMPT_HASH_MISMATCH" or altered["comparability"]["ok"]:
        gaps.append("prompt-hash")
    _put_result(runtime.store, source_ids["single_agent"], originals["single_agent"])

    shortened = deepcopy(originals["ma_crossover"])
    shortened["decisions"] = [item for item in shortened["decisions"] if item["decision_session"] != "2023-01-06"]
    _put_result(runtime.store, source_ids["ma_crossover"], shortened)
    dropped = _guarded_export(monkeypatch, client, study_id).json()["report"]
    missing = next(item for item in dropped["sources"] if item["strategy"] == "ma_crossover")
    if missing["comparability"] != "DECISION_SESSION_MISSING" or missing["status"] != "COMPLETED" or dropped["comparability"]["ok"]:
        gaps.append("missing-session")
    if dropped["coverage"].get("verdict") == "COMPLETE" or dropped["coverage"].get("missing_sessions", 0) < 1:
        gaps.append("missing-stays-in-denominator")
    _put_result(runtime.store, source_ids["ma_crossover"], originals["ma_crossover"])

    _tripwire(monkeypatch)
    replayed = client.post(f"/api/v1/research/studies/{study_id}/replay", json={})
    monkeypatch.undo()
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replay_posts"] == 0 and runtime.posts_for(study_id) == posts
    boolean = deepcopy(originals["buy_hold"])
    boolean["decisions"][0]["approved_weight"] = True
    _put_result(runtime.store, source_ids["buy_hold"], boolean)
    persisted = _guarded_export(monkeypatch, client, study_id).json()["report"]
    replay = next(item for item in persisted["semantic_replay"] if item["strategy"] == "buy_hold")
    projected = next(item for item in persisted["sources"] if item["strategy"] == "buy_hold")
    if replay["equal"] or replay["reason"] != "MALFORMED_RESULT" or projected["decisions"][0]["approved_weight"] is not None:
        gaps.append("persisted-bool")
    _put_result(runtime.store, source_ids["buy_hold"], originals["buy_hold"])
    assert runtime.posts_for(study_id) == posts and _ledger_bytes(runtime.store)[:3] == ledger[:3]

    _planned_cancel, started = start(client, request_for("fixture-cancel", dataset_hash), "report-evidence-cancel")
    assert started.status_code == 202, started.text
    cancelled_id = started.json()["study_id"]
    assert client.post(f"/api/v1/research/studies/{cancelled_id}/cancel", json={}).status_code == 200
    wait_for(client, cancelled_id, {"CANCELLED"})
    absent = _guarded_export(monkeypatch, client, cancelled_id).json()["report"]
    if absent["comparability"]["ok"] or any(item["comparability"] == "ok" or item["metrics"] is not None for item in absent["sources"]):
        gaps.append("not-run")
    if absent["coverage"].get("verdict") == "COMPLETE" or absent["coverage"].get("strategy_denominator") != 4:
        gaps.append("not-run-coverage")
    queued = [item for item in absent["attempts"] if item.get("usage_status") == "not_sent"]
    if not queued or any(item.get("sent") is not False or item.get("usage_estimate_usd") is not None or item.get("reserved_usd") is not None for item in queued):
        gaps.append("not-sent")
    if any(item.get("usage_status") == "dispatched_uncertain" for item in absent["attempts"]):
        gaps.append("cancel-dispatched")
    cancel_posts = runtime.posts_for(cancelled_id)
    cancel_ledger = _ledger_bytes(runtime.store)
    _guarded_export(monkeypatch, client, cancelled_id)
    assert runtime.posts_for(study_id) == posts and runtime.posts_for(cancelled_id) == cancel_posts
    assert _ledger_bytes(runtime.store)[:3] == cancel_ledger[:3]
    later = {row[0]: row for row in _snapshot(runtime.store)[0]}
    assert later[study_id] == before[0][0]
    assert gaps == [], gaps


def test_null_metrics_after_replay_is_a_safe_report(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    study_id, _planned, final = _finish(client, dataset_hash, "fixture-success", "report-malformed", {"COMPLETED"})
    posts = runtime.posts_for(study_id)
    _tripwire(monkeypatch)
    replayed = client.post(f"/api/v1/research/studies/{study_id}/replay", json={})
    monkeypatch.undo()
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replay_posts"] == 0 and runtime.posts_for(study_id) == posts
    buy_hold = final["source_runs"]["buy_hold"]["run_id"]
    saved = _load_result(runtime.store, buy_hold)
    saved["metrics"] = None
    saved["leak"] = "sk-syntheticfixturesecret C:\\Users\\fixture\\secret ops@example.com"
    _put_result(runtime.store, buy_hold, saved)
    before = _ledger_bytes(runtime.store)
    _tripwire(monkeypatch)
    response = client.get(f"/api/v1/research/studies/{study_id}/export")
    monkeypatch.undo()
    text = response.text
    assert "Traceback" not in text and "AttributeError" not in text and "NoneType" not in text
    for marker in MARKERS:
        assert marker not in text
    assert response.status_code == 200, text
    report = response.json()["report"]
    replay = next(item for item in report["semantic_replay"] if item["strategy"] == "buy_hold")
    source = next(item for item in report["sources"] if item["strategy"] == "buy_hold")
    assert replay["equal"] is False and replay["reason"] == "MALFORMED_RESULT"
    assert replay["diagnostic"]["hides_mismatch"] is False and replay["diagnostic"]["max_abs_equity"] is None
    assert source["comparability"] == "MALFORMED_RESULT" and source["metrics"] is None and source["zero_performance"] is False
    assert report["comparability"]["ok"] is False
    assert runtime.posts_for(study_id) == posts and _ledger_bytes(runtime.store) == before


def test_timeout_liability_links_durable_attempts_without_mutation(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    study_id, _planned, paused = _finish(client, dataset_hash, "fixture-timeout", "report-liability", {"PAUSED_BILLING"})
    unknown = paused["cost"]["unknown_reserved_usd"]
    posts = runtime.posts_for(study_id)
    ledger = _ledger_bytes(runtime.store)
    report = _guarded_export(monkeypatch, client, study_id).json()["report"]
    assert runtime.posts_for(study_id) == posts and _ledger_bytes(runtime.store) == ledger
    gaps = []
    if report["cost"].get("actual_charge_scope") != "individually-reconciled-calls":
        gaps.append("individual-scope")
    if report["cost"]["unknown_reserved_usd"] != unknown or report["cost"]["invoiced_actual_usd"] is not None:
        gaps.append("unknown-or-invoice")
    if report["cost"]["per_call_actual_allocation_usd"] is not None or report["cost"]["confirmed_actual_usd"] == unknown:
        gaps.append("allocation")
    if "key-usage-aggregate-with-matching-calls" in json.dumps(report):
        gaps.append("raw-scope")
    uncertain = [item for item in report["attempts"] if item.get("usage_status") == "dispatched_uncertain"]
    if len(uncertain) != 1:
        gaps.append("uncertain-count")
    else:
        row = uncertain[0]
        if row.get("strategy") != "single_agent" or row.get("stage") != "decision" or row.get("decision_session") != "2023-01-06":
            gaps.append("uncertain-identity")
        if row.get("sent") is not True or row.get("state") != "RECORDED" or not row.get("slot_id") or not row.get("call_id"):
            gaps.append("uncertain-linkage")
        if row.get("input_tokens") is not None or row.get("output_tokens") is not None or row.get("usage_estimate_usd") is not None or row.get("actual_usd") is not None:
            gaps.append("invented-usage")
        if row.get("reserved_usd") != unknown or row.get("latency_ms") is None:
            gaps.append("held-or-latency")
    queued = [item for item in report["attempts"] if item.get("usage_status") == "not_sent"]
    if not queued or any(item.get("sent") is not False or item.get("reserved_usd") is not None or item.get("latency_ms") is not None or item.get("usage_estimate_usd") is not None for item in queued):
        gaps.append("not-sent")
    assert gaps == [], gaps
    rendered = json.dumps(report)
    for marker in MARKERS:
        assert marker not in rendered
    assert "observation_json" not in rendered and "instructions" not in rendered
    cancelled = client.post(f"/api/v1/research/studies/{study_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    final = wait_for(client, study_id, {"CANCELLED"})
    assert final["cost"]["unknown_reserved_usd"] == unknown
    after = _guarded_export(monkeypatch, client, study_id).json()["report"]
    assert after["cost"]["unknown_reserved_usd"] == unknown
    assert runtime.posts_for(study_id) == posts and _ledger_bytes(runtime.store)[:3] == ledger[:3]


def _role_output(**updates):
    row = {
        "target_weight": 1.0, "confidence": 0.25, "rationale": "fixture",
        "evidence": ["last_close"], "acknowledged_risks": [],
    }
    row.update(updates)
    return row


def _agent_view(output=None, analysis=None, critic=None):
    body = _result()
    body["decisions"][0]["analysis_output"] = analysis
    body["decisions"][0]["critic_output"] = critic
    body["decisions"][0]["decision_output"] = _role_output() if output is None else output
    return body


def test_nested_role_numbers_reject_bool_and_keep_null_roles():
    """Schema numeric fields inside role outputs are strict. Null roles stay comparable."""
    original = _agent_view()
    assert semantic_replay_view(original, deepcopy(original))["equal"] is True
    bare = _agent_view(output=None)
    assert semantic_replay_view(bare, deepcopy(bare))["equal"] is True
    zero = _agent_view(output=_role_output(target_weight=0.0, confidence=0.0))
    assert semantic_replay_view(zero, deepcopy(zero))["equal"] is True
    skipped = deepcopy(zero)
    skipped["decisions"][0]["status"] = "SKIPPED"
    skipped["decisions"][0]["requested_weight"] = None
    skipped["decisions"][0]["approved_weight"] = None
    skipped["decisions"][0]["decision_output"] = None
    skipped["decisions"][0]["reason_codes"] = ["INVALID_DECISION"]
    assert semantic_replay_view(zero, skipped)["equal"] is False
    assert semantic_replay_view(skipped, deepcopy(skipped))["equal"] is True
    analysis = {"regime": "bullish", "thesis": "fixture", "evidence": ["ma20"]}
    critic = {"risk_level": "low", "risk_flags": ["NO_OBJECTION"], "counterarguments": []}
    paired = _agent_view(analysis=analysis, critic=critic)
    assert semantic_replay_view(paired, deepcopy(paired))["equal"] is True
    changed = deepcopy(paired)
    changed["decisions"][0]["analysis_output"]["regime"] = "bearish"
    assert semantic_replay_view(paired, changed)["reason"] == "SEMANTIC_MISMATCH"
    gaps = []
    cases = (
        ("target-true", {"target_weight": True}),
        ("target-false", {"target_weight": False}),
        ("confidence-true", {"confidence": True}),
        ("confidence-false", {"confidence": False}),
        ("confidence-null", {"confidence": None}),
        ("confidence-nan", {"confidence": float("nan")}),
        ("target-text", {"target_weight": "1.0"}),
    )
    for name, updates in cases:
        mutated = _agent_view(output=_role_output(**updates))
        view = semantic_replay_view(original, mutated)
        if view["equal"] or view["reason"] != "MALFORMED_RESULT" or view["diagnostic"]["hides_mismatch"] or view["diagnostic"]["max_abs_equity"] is not None:
            gaps.append(name)
        if semantic_replay_view(mutated, deepcopy(mutated))["equal"]:
            gaps.append(name + "-self")
    shaped = _agent_view(output=["target_weight"])
    if semantic_replay_view(original, shaped)["reason"] != "MALFORMED_RESULT":
        gaps.append("wrong-shape")
    assert gaps == [], gaps


def _source_report(report, strategy):
    return next(item for item in report["sources"] if item["strategy"] == strategy)


def _slot_blob(store):
    with store._read() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT slot_id,state,outcome,observation_json FROM research_slots ORDER BY slot_id")]


def test_input_provenance_uses_frozen_hashes_and_role_payloads(opened, monkeypatch):
    """Changed, missing, extra, or dropped role evidence must leave comparability."""
    from app.data import canonical_hash
    from app.models import FEATURE_NAMES

    runtime, client, dataset_hash = opened
    study_id, _planned, final = _finish(client, dataset_hash, "fixture-success", "report-provenance", {"COMPLETED"})
    source_ids = {name: item["run_id"] for name, item in final["source_runs"].items()}
    posts = runtime.posts_for(study_id)
    originals = {name: _load_result(runtime.store, run_id) for name, run_id in source_ids.items()}
    with runtime.store._read() as conn:
        frozen = {(row["strategy"], row["decision_session"]): row["numeric_input_hash"] for row in conn.execute(
            "SELECT strategy,decision_session,numeric_input_hash FROM research_decisions WHERE study_id=?", (study_id,))}
    healthy = _guarded_export(monkeypatch, client, study_id).json()["report"]
    assert healthy["comparability"]["ok"] is True
    single = originals["single_agent"]["decisions"][0]
    numeric = {key: single["snapshot"][key] for key in FEATURE_NAMES}
    assert canonical_hash(numeric) == frozen[("single_agent", single["decision_session"])]
    assert single["calls"][0]["input_hash"] == canonical_hash({"features": numeric})
    assert single["calls"][0]["input_hash"] != frozen[("single_agent", single["decision_session"])]
    multi = originals["multi_agent"]["decisions"][0]
    multi_numeric = {key: multi["snapshot"][key] for key in FEATURE_NAMES}
    by_stage = {call["stage"]: call for call in multi["calls"]}
    assert by_stage["analysis"]["input_hash"] == canonical_hash({"features": multi_numeric})
    assert by_stage["critic"]["input_hash"] != canonical_hash({"features": multi_numeric})
    assert "analysis" in by_stage["critic"]["input"] and "critic" in by_stage["decision"]["input"]
    buy = _source_report(healthy, "buy_hold")
    assert buy["decisions"][0]["calls"] == []
    holds = [item for item in healthy["coverage"]["observations"] if item["strategy"] == "buy_hold" and item["kind"] == "policy_hold"]
    assert holds and all(item["applicability"] == "NOT_APPLICABLE" and item["recorded_decision"] is False for item in holds)
    assert _source_report(healthy, "single_agent")["comparability"] == "ok"
    assert _source_report(healthy, "multi_agent")["comparability"] == "ok"

    def expect(strategy, code):
        report = _guarded_export(monkeypatch, client, study_id).json()["report"]
        source = _source_report(report, strategy)
        overall = report["comparability"]
        matched = source["comparability"] == code and overall["ok"] is False
        named = any(item.get("strategy") == strategy and item.get("reason") == code for item in overall.get("affected_strategies") or [])
        return matched and named, source["comparability"], overall["reason"]

    gaps = []

    def changed_hash(payload):
        payload["decisions"][0]["calls"][0]["input_hash"] = "0" * 64

    def missing_hash(payload):
        payload["decisions"][0]["calls"][0].pop("input_hash", None)

    def drop_calls(payload):
        for decision in payload["decisions"]:
            decision["calls"] = []

    def move_feature(payload):
        payload["decisions"][0]["snapshot"]["last_close"] = 123456789.0

    def duplicate_call(payload):
        extra = deepcopy(payload["decisions"][0]["calls"][0])
        extra["call_id"] = "00000000-0000-4000-8000-000000000099"
        payload["decisions"][0]["calls"].append(extra)

    def extra_stage(payload):
        extra = deepcopy(payload["decisions"][0]["calls"][0])
        extra["call_id"] = "00000000-0000-4000-8000-000000000098"
        extra["stage"] = "analysis"
        payload["decisions"][0]["calls"].append(extra)

    def critic_predecessor(payload):
        call = next(item for item in payload["decisions"][0]["calls"] if item["stage"] == "critic")
        forged = deepcopy(call["input"])
        forged["analysis"] = {"regime": "bearish", "thesis": "forged", "evidence": ["ma20"]}
        call["input"] = forged
        call["input_hash"] = canonical_hash(forged)

    def baseline_call(payload):
        sample = originals["single_agent"]["decisions"][0]["calls"][0]
        payload["decisions"][0]["calls"] = [{
            "stage": "decision", "call_id": "00000000-0000-4000-8000-000000000097",
            "prompt_hash": sample["prompt_hash"], "schema_hash": sample["schema_hash"],
            "model_id": sample.get("model_id"), "input_hash": sample["input_hash"],
            "input": deepcopy(sample["input"]),
        }]

    mutations = (
        ("single_agent", "changed_input_hash", changed_hash, "INPUT_HASH_MISMATCH"),
        ("single_agent", "missing_input_hash", missing_hash, "LINKAGE_UNVERIFIABLE"),
        ("single_agent", "agent_calls_removed", drop_calls, "ROLE_EVIDENCE_MISSING"),
        ("single_agent", "snapshot_feature_changed", move_feature, "INPUT_HASH_MISMATCH"),
        ("single_agent", "duplicated_call", duplicate_call, "LINKAGE_UNVERIFIABLE"),
        ("single_agent", "extra_stage", extra_stage, "LINKAGE_UNVERIFIABLE"),
        ("multi_agent", "critic_predecessor_forged", critic_predecessor, "INPUT_HASH_MISMATCH"),
        ("buy_hold", "baseline_call_injected", baseline_call, "LINKAGE_UNVERIFIABLE"),
    )
    for strategy, name, transform, code in mutations:
        body = deepcopy(originals[strategy])
        transform(body)
        _put_result(runtime.store, source_ids[strategy], body)
        try:
            ok, actual, overall = expect(strategy, code)
            if not ok:
                gaps.append((name, actual, overall, code))
        finally:
            _put_result(runtime.store, source_ids[strategy], originals[strategy])
    assert gaps == [], gaps

    slot = next(item for item in runtime.store.list_slots(study_id)
                if item["strategy"] == "multi_agent" and item["decision_session"] == "2023-01-27" and item["stage"] == "decision")
    saved_slot = (slot["state"], slot["outcome"], slot["observation_json"])
    partial = deepcopy(originals["multi_agent"])
    last = partial["decisions"][-1]
    last["calls"] = [call for call in last["calls"] if call["stage"] != "decision"]
    last["decision_output"] = None
    last["requested_weight"] = None
    last["approved_weight"] = None
    last["status"] = "SKIPPED"
    last["reason_codes"] = ["INVALID_DECISION"]
    before_slots = _slot_blob(runtime.store)
    with runtime.store.connect() as conn:
        conn.execute(
            "UPDATE research_slots SET state='UNSTARTED', outcome=NULL, observation_json=NULL WHERE slot_id=?",
            (slot["slot_id"],))
    _put_result(runtime.store, source_ids["multi_agent"], partial)
    try:
        report = _guarded_export(monkeypatch, client, study_id).json()["report"]
        source = _source_report(report, "multi_agent")
        assert source["comparability"] == "ok" and report["comparability"]["ok"] is True
        assert source["decisions"][-1]["calls"] and all(call["stage"] != "decision" for call in source["decisions"][-1]["calls"])
        holds = [item for item in report["coverage"]["observations"] if item["kind"] == "policy_hold"]
        assert holds and all(item["recorded_decision"] is False for item in holds)
    finally:
        with runtime.store.connect() as conn:
            conn.execute(
                "UPDATE research_slots SET state=?, outcome=?, observation_json=? WHERE slot_id=?",
                (*saved_slot, slot["slot_id"]))
        _put_result(runtime.store, source_ids["multi_agent"], originals["multi_agent"])
    assert _slot_blob(runtime.store) == before_slots

    _tripwire(monkeypatch)
    replayed = client.post(f"/api/v1/research/studies/{study_id}/replay", json={})
    monkeypatch.undo()
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replay_posts"] == 0 and runtime.posts_for(study_id) == posts
    ledger = _ledger_bytes(runtime.store)
    slots = _slot_blob(runtime.store)
    events = _snapshot(runtime.store)[2][0]
    nested_cases = (
        ("source", source_ids["single_agent"], originals["single_agent"], ("decision_output", "target_weight"), True),
        ("replay", None, None, ("decision_output", "confidence"), False),
        ("source-null", source_ids["single_agent"], originals["single_agent"], ("decision_output", "confidence"), None),
        ("source-text", source_ids["single_agent"], originals["single_agent"], ("decision_output", "target_weight"), "1.0"),
        ("source-nan", source_ids["single_agent"], originals["single_agent"], ("decision_output", "confidence"), float("nan")),
        ("source-shape", source_ids["single_agent"], originals["single_agent"], ("decision_output",), ["target_weight"]),
    )
    replay_id = next(item["replay_run_id"] for item in _guarded_export(monkeypatch, client, study_id).json()["report"]["semantic_replay"] if item["strategy"] == "single_agent")
    replay_original = _load_result(runtime.store, replay_id)
    assert semantic_replay_view(originals["single_agent"], replay_original)["equal"] is True
    for name, run_id, saved, path, value in nested_cases:
        target = run_id or replay_id
        body = deepcopy(saved or replay_original)
        cursor = body["decisions"][0]
        for key in path[:-1]:
            cursor = cursor[key]
        if len(path) == 1:
            body["decisions"][0][path[0]] = value
        else:
            cursor[path[-1]] = value
        body["leak"] = "sk-syntheticfixturesecret C:\\Users\\fixture\\secret ops@example.com fixture-group-marker"
        _put_result(runtime.store, target, body)
        try:
            response = _guarded_export(monkeypatch, client, study_id)
            text = response.text
            assert "Traceback" not in text and "AttributeError" not in text and "NoneType" not in text
            for marker in MARKERS:
                assert marker not in text
            report = response.json()["report"]
            semantic = next(item for item in report["semantic_replay"] if item["strategy"] == "single_agent")
            assert semantic["equal"] is False and semantic["reason"] == "MALFORMED_RESULT"
            assert semantic["diagnostic"]["hides_mismatch"] is False and semantic["diagnostic"]["max_abs_equity"] is None
        finally:
            _put_result(runtime.store, target, saved or replay_original)
    assert runtime.posts_for(study_id) == posts
    assert _ledger_bytes(runtime.store) == ledger
    assert _slot_blob(runtime.store) == slots
    assert _snapshot(runtime.store)[2][0] == events


def test_cancel_before_send_is_not_zero_performance(opened):
    runtime, client, dataset_hash = opened
    planned, started = start(client, request_for("fixture-cancel", dataset_hash), "report-cancel")
    assert started.status_code == 202, started.text
    study_id = started.json()["study_id"]
    assert client.post(f"/api/v1/research/studies/{study_id}/cancel", json={}).status_code == 200
    final = wait_for(client, study_id, {"CANCELLED"})
    assert planned["plan"]["estimate"]["baseline_calls"] == 0
    assert final["fixture_transport_posts"] == 0
    report = _export(client, study_id).json()["report"]
    assert report["coverage"]["denominator"] == 4
    assert [item["strategy"] for item in report["sources"]] == ["buy_hold", "ma_crossover", "single_agent", "multi_agent"]
    assert all(item["zero_performance"] is False and item["origin"] == "source" for item in report["sources"])
    assert all(item["metrics"] is None or item["status"] == "COMPLETED" or item["not_complete"] is True for item in report["sources"])
    assert all(item["replay_run_id"] is None or item["replay_run_id"] != item["source_run_id"] for item in report["semantic_replay"])
    assert runtime.posts_for(study_id) == 0
    for source in report["sources"]:
        if source["status"] == "NOT_RUN":
            assert source["metrics"] is None and source["not_complete"] is True
        for decision in source["decisions"]:
            if decision["status"] == "SKIPPED":
                assert decision["reason_codes"]
                assert decision["approved_weight"] is None
