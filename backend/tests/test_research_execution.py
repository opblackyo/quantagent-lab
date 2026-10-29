"""Synthetic fixture execution for one admitted study. No live provider or key."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import traceback
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.data import canonical_hash
from app.gateway import application_launch_block
from app.pilot import STRATEGIES, semantic_result
from app.pilot_billing import reconcile_study_usage
from app.research_service import (
    ExecutionError, assess_preflight, execute_study, plan_study, poll_study, replay_study, resume_study,
)
from app.research_store import PlanningInputs, ResearchStore
from test_research_planner import setup_plan
from test_research_store import admitted, auth_for, bundle, state_digest

TOKEN = "synthetic-fixture-token"
OUTPUTS = {
    "analysis_output": {"regime": "bullish", "thesis": "Synthetic fixture compares trailing averages.", "evidence": ["ma20", "ma60"]},
    "critic_output": {"risk_level": "low", "risk_flags": ["NO_OBJECTION"], "counterarguments": []},
    "decision_output": {"target_weight": 1, "confidence": 0.6, "rationale": "Synthetic fixture target.", "evidence": ["ma20"], "acknowledged_risks": []},
}


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("live provider or network is forbidden")
    from app.agents import OpenAIProvider
    from app.gateway import KuaiAPIProvider
    monkeypatch.setattr(OpenAIProvider, "__init__", forbidden)
    monkeypatch.setattr(KuaiAPIProvider, "__init__", forbidden)
    def stdlib_self_pipe():
        return any(frame.name in {"_fallback_socketpair", "_make_self_pipe"} for frame in traceback.extract_stack())
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def guarded_connect(self, address, *args, **kwargs):
        if stdlib_self_pipe():
            return original_connect(self, address, *args, **kwargs)
        return forbidden()
    def guarded_connect_ex(self, address, *args, **kwargs):
        if stdlib_self_pipe():
            return original_connect_ex(self, address, *args, **kwargs)
        return forbidden()
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    original = os._Environ.__getitem__
    def guarded(env, name):
        if str(name).startswith(("OPENAI_", "KUAIAPI_", "LLM_")):
            return forbidden()
        return original(env, name)
    monkeypatch.setattr(os._Environ, "__getitem__", guarded)


def billing_for(inputs, at, **overrides):
    profile = inputs.profile
    binding = next(item for item in profile.private_evidence if item.kind == "binding")
    value = {"kind": "synthetic-readonly-billing-v1", "semantics": "token-effective-usd", "provider": profile.provider,
             "model_id": profile.model_id, "endpoint": "https://kuaiapi.net/v1/responses", "pricing_unit": profile.pricing_unit,
             "multiplier": "1", "service_tier": "auto", "input_usd_per_million": str(profile.input_usd_per_million),
             "output_usd_per_million": str(profile.output_usd_per_million), "cache_read_usd_per_million": str(profile.cache_read_usd_per_million),
             "cache_write_usd_per_million": str(profile.cache_write_usd_per_million), "binding_ref": binding.reference_id,
             "binding_sha256": binding.sha256, "group_ref": "synthetic-key", "valid_from": profile.valid_from.isoformat(),
             "expires_at": profile.expires_at.isoformat(), "as_of_date": at.astimezone(timezone.utc).date().isoformat(),
             "timezone": "UTC", "day_count": 1, "external_same_key_requests": 0, "delayed": False}
    value.update(overrides)
    return value


def fixture_client(mode="ok", on_post=None):
    seen = {"n": 0}
    def handler(request):
        seen["n"] += 1
        if on_post:
            on_post(request, seen)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic inactivity")
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://elsewhere.invalid/next"})
        if mode == "malformed":
            return httpx.Response(200, content=b"not-json")
        if mode == "http429":
            return httpx.Response(429, json={"error": {"message": "synthetic busy"}, "status": "failed"})
        name = json.loads(request.content)["text"]["format"]["name"]
        body_output = dict(OUTPUTS[name])
        raw = json.dumps(body_output)
        if mode == "schema" and seen["n"] == 1:
            raw = '{"target_weight":"no"}'
        if mode == "zero" and name == "decision_output" and seen["n"] == 2:
            raw = json.dumps({**body_output, "target_weight": 0})
        if mode == "zero" and name == "decision_output" and seen["n"] == 3:
            raw = '{"target_weight":"no"}'
        if mode == "refusal" and seen["n"] == 1:
            content = [{"type": "refusal", "text": "synthetic refusal"}]
        else:
            content = [{"type": "output_text", "text": raw}]
        message_status = "incomplete" if mode == "contradiction" and seen["n"] == 1 else "completed"
        usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        if mode == "missing-usage" and seen["n"] == 1:
            usage = None
        elif mode == "inconsistent" and seen["n"] == 1:
            usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 1}
        elif mode == "excess" and seen["n"] == 1:
            usage = {"input_tokens": 100, "output_tokens": 5000, "total_tokens": 5100}
        envelope = {"id": "same-id" if mode == "duplicate" else f"synthetic-response-{seen['n']}", "model": "other-model" if mode == "drift" and seen["n"] == 1 else "synthetic-model-v1",
                    "status": "completed", "service_tier": "auto", "output": [{"type": "message", "status": message_status, "content": content}],
                    "debug": request.headers.get("authorization")}
        if usage is not None:
            envelope["usage"] = usage
        if mode == "tier-fast" and seen["n"] == 1:
            envelope["service_tier"] = "fast"
        return httpx.Response(200, json=envelope)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def open_study(tmp_path, parts, budget=None, attempts=None, name="execution.sqlite3"):
    request, profile, registry, context = parts
    if budget is not None:
        request = request.model_copy(update={"requested_budget_usd": Decimal(budget)})
    if attempts is not None:
        profile = profile.model_copy(update={"attempts_per_role": attempts})
    inputs = PlanningInputs(request, profile, registry, context)
    plan, _ = plan_study(request, profile, registry, context=context)
    store = ResearchStore.create_new(tmp_path / name)
    study = store.register(plan, inputs, idempotency_key="synthetic-execution")
    store.freeze(study, inputs, at=context.checked_at)
    auth = auth_for(study, plan, inputs)
    store.record_authorization(auth, at=context.checked_at)
    store.start(study, auth.authorization_id, inputs, at=context.checked_at)
    return store, study, inputs, context.checked_at, plan


def run(store, study, inputs, at, client, mode_billing=None, fault=None):
    return execute_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN,
                         billing=mode_billing or billing_for(inputs, at), fault=fault, secrets=(TOKEN,),
                         usage_evidence=fixture_usage)


def fixture_usage(store, study, slot_id):
    """Explicit fake readonly billing; real execution must never invent this evidence."""
    plan = store.load_plan(study)
    return _evidence(store, study, plan, None, only_pending=True)


def test_service_disabled_and_app_launch_stay_closed(bundle):
    store, _plan, inputs, at = bundle
    study, _auth = admitted(bundle)
    client, seen = fixture_client()
    before = state_digest(store)
    with pytest.raises(ExecutionError, match="SERVICE_DISABLED"):
        execute_study(store, study, inputs, at=at, service_enabled=False, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
    assert seen["n"] == 0 and state_digest(store) == before and store.get_study(study)["state"] == "QUEUED"
    assert application_launch_block("kuaiapi") == "KuaiAPI application execution requires the bounded private pilot runner"
    assert application_launch_block("openai") == "OpenAI application execution requires the default-disabled research service"
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(store.path.parent / "app.sqlite3")) as api:
        response = api.post("/api/v1/experiments", json={"strategy_id": "single_agent", "llm_mode": "api", "model_provider": "openai", "model_id": "synthetic-model-v1"})
    assert response.status_code == 422 and response.json()["detail"] == application_launch_block("openai")


def test_four_strategies_replay_redaction_and_aggregate(tmp_path, setup_plan):
    assert STRATEGIES == ("buy_hold", "ma_crossover", "single_agent", "multi_agent")
    store, study, inputs, at, plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client)
        assert result["status"] == "COMPLETED" and result["posts"] == seen["n"] == 16
        report = result["report"]
        assert report["coverage"] == {strategy: "COMPLETED" for strategy in STRATEGIES}
        assert "execution-checkpoint-v1:BEFORE_ROLE" in report["event_types"]
        assert "execution-checkpoint-v1:BEFORE_DECISION" in report["event_types"]
        assert "execution-checkpoint-v1:BEFORE_FILL" in report["event_types"]
        for strategy in ("buy_hold", "ma_crossover"):
            origin = store.get(report["source_runs"][strategy]["run_id"], True)
            assert origin["result"]["metrics"]["llm_calls"] == 0 and origin["config"]["llm_mode"] == "mock"
        single = store.get(report["source_runs"]["single_agent"]["run_id"], True)["result"]
        assert single["metrics"]["llm_calls"] == 4 and [trade["side"] for trade in single["trades"]] == ["BUY"]
        assert any(row["reason_codes"] == ["TARGET_ALREADY_MET"] for row in single["decisions"])
        text = json.dumps(report)
        assert TOKEN not in text and "synthetic-key" not in text and "private-binding-marker" not in text
        assert "observation_json" not in text and "canonical_payload" not in text
        stored = json.dumps([slot["observation_json"] for slot in store.list_slots(study)])
        assert TOKEN not in stored and "[REDACTED_API_KEY]" in stored
        observed = json.loads(store.list_slots(study)[0]["observation_json"])
        assert observed["service_tier_observation"]["proves_fast_price_or_model_change"] is False
        assert "FORMAT_ECHO_NOT_REPORTED" in observed["observation_warnings"]
        replay = replay_study(store, study, inputs, at=at)
        assert seen["n"] == 16 and all(item["equal"] for item in replay["replays"].values())
        assert all(item["run_id"] != report["source_runs"][strategy]["run_id"] for strategy, item in replay["replays"].items())
        again = execute_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert again["status"] == "BLOCKED" and again["posts"] == 0 and seen["n"] == 16
        evidence = _evidence(store, study, plan, inputs)
        first = reconcile_study_usage(store, study, evidence)
        second = reconcile_study_usage(store, study, evidence)
        assert first == second and first["invoiced_actual_usd"] is None and first["unknown_reservations_released"] is False
        cost = store.get_study(study)["cost"]
        assert cost["unknown_reserved_usd"] == 0 and cost["confirmed_actual_usd"] == pytest.approx(0.000576)
        assert cost["usage_estimated_usd"] == pytest.approx(0.001056) and cost["invoiced_actual_usd"] is None
        with store._read() as conn:
            assert all(row["actual_charge"] is None for row in conn.execute("SELECT actual_charge FROM pilot_attempts"))
            assert conn.execute("SELECT COUNT(*) FROM pilot_usage_reconciliations").fetchone()[0] == 1
        changed = {**evidence, "actual_charge_usd": "9"}
        with pytest.raises(ValueError, match="immutable|tariff"):
            reconcile_study_usage(store, study, changed)
        with store._read() as conn:
            assert json.loads(conn.execute("SELECT receipt_json FROM pilot_usage_reconciliations").fetchone()[0])["actual_charge_usd"] == "0.000576"
        before = state_digest(store)
        assert poll_study(store, study)["state"] == "COMPLETED"
        assert state_digest(store) == before
    finally:
        client.close()
        store.close()


def test_zero_liquidates_and_schema_skip_retains(tmp_path, setup_plan):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client("zero")
    try:
        result = run(store, study, inputs, at, client)
        single = store.get(result["report"]["source_runs"]["single_agent"]["run_id"], True)["result"]
        assert [trade["side"] for trade in single["trades"]] == ["BUY", "SELL", "BUY"]
        skipped = single["decisions"][2]
        assert skipped["status"] == "SKIPPED" and skipped["approved_weight"] is None and skipped["decision_output"] is None
        assert "SCHEMA_OR_JSON_INVALID" in skipped["reason_codes"]
        assert semantic_result(store.get(replay_study(store, study, inputs, at=at)["replays"]["single_agent"]["run_id"], True)["result"]) == semantic_result(single)
    finally:
        client.close(); store.close()


@pytest.mark.parametrize("mode,code", [
    ("refusal", "REFUSAL_NOT_DECISION"), ("schema", "SCHEMA_OR_JSON_INVALID"),
])
def test_refusal_and_schema_skip_without_pause(tmp_path, setup_plan, mode, code):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, _seen = fixture_client(mode)
    try:
        result = run(store, study, inputs, at, client)
        assert result["status"] == "COMPLETED"
        decision = store.get(result["report"]["source_runs"]["single_agent"]["run_id"], True)["result"]["decisions"][0]
        assert decision["status"] == "SKIPPED" and code in decision["reason_codes"] and decision["approved_weight"] is None
    finally:
        client.close(); store.close()


@pytest.mark.parametrize("mode", ["missing-usage", "inconsistent", "excess", "contradiction", "malformed", "redirect"])
def test_bad_envelope_preserves_liability_and_stops(tmp_path, setup_plan, mode):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client(mode)
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 1
        assert result["status"] == "PAUSED_BILLING"
        assert result["report"]["coverage"]["multi_agent"] == "NOT_RUN"
        assert store.get_study(study)["cost"]["unknown_reserved_usd"] == pytest.approx(0.0172032)
        with pytest.raises(ExecutionError, match="RESUME_NOT_ELIGIBLE"):
            resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert seen["n"] == 1
    finally:
        client.close(); store.close()


def test_model_drift_keeps_hold_and_resume_does_not_resend(tmp_path, setup_plan):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client("drift")
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and result["status"] == "PAUSED_DRIFT"
        assert result["report"]["coverage"]["multi_agent"] == "NOT_RUN"
        cost = store.get_study(study)["cost"]
        assert cost["unknown_reserved_usd"] == pytest.approx(0.0172032)
        assert cost["usage_estimated_usd"] == 0
        drifted = next(slot for slot in store.list_slots(study) if slot["outcome"] == "FAILED")
        with pytest.raises(ExecutionError, match='RESUME_NOT_ELIGIBLE'):
            resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert seen['n'] == 1
        again = [slot for slot in store.list_slots(study) if slot["call_id"] == drifted["call_id"]]
        assert len(again) == 1 and again[0]["outcome"] == "FAILED"
        assert sum(slot["outcome"] == "SUCCEEDED" for slot in store.list_slots(study)) == 0
    finally:
        client.close(); store.close()


def test_timeout_after_claim_is_not_a_deadline(tmp_path, setup_plan):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client("timeout")
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and result["status"] == "PAUSED_BILLING"
        observation = json.loads(next(slot["observation_json"] for slot in store.list_slots(study) if slot["observation_json"]))
        diagnostic = observation["timeout_diagnostic"]
        assert diagnostic["semantics"] == "HTTPX phase/chunk inactivity limits; not a total wall-clock deadline"
        assert diagnostic["phase"] == "read" and diagnostic["exception_class"] == "ReadTimeout"
        assert "deadline" not in diagnostic["policy"]
        assert store.get_study(study)["cost"]["unknown_reserved_usd"] == pytest.approx(0.0172032)
    finally:
        client.close(); store.close()


def test_global_cap_pauses_after_one_send(tmp_path, setup_plan):
    # One full reservation fits. After it settles to the known estimate, the same frozen cap cannot reserve the next call.
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan, budget="0.01722")
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and result["status"] == "PAUSED_BILLING" and result["report"]["state"] != "COMPLETED"
        single = store.get(result["report"]["source_runs"]["single_agent"]["run_id"], True)["result"]
        assert [trade["side"] for trade in single["trades"]] == ["BUY"] and single["equity"][-1]["shares"] > 0
        assert single["decisions"][-1]["reason_codes"] == ["PILOT_BUDGET_EXHAUSTED"]
        assert single["decisions"][-1]["approved_weight"] is None and len(single["decisions"]) < 4
        resumed = resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert resumed["status"] == "PAUSED_BILLING" and seen["n"] == 1
    finally:
        client.close(); store.close()


def test_preflight_drift_outranks_billing_and_sends_nothing(tmp_path, setup_plan):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client()
    try:
        drifted = run(store, study, inputs, at, client, billing_for(inputs, at, model_id="other-model", delayed=True, external_same_key_requests=4))
        assert seen["n"] == 0 and drifted["status"] == "PAUSED_DRIFT" and "MODEL_DRIFT" in drifted["preflight_codes"]
        assert "MODEL_DRIFT" in drifted["report"]["reason"] and "synthetic-key" not in drifted["report"]["reason"]
        rolled = assess_preflight(inputs.profile, billing_for(inputs, at, as_of_date="2023-01-01"), at=at, key_ref="synthetic-key")
        assert rolled["pause"] == "PAUSED_BILLING" and "DAY_WINDOW_ROLLOVER" in rolled["codes"]
        assert assess_preflight(inputs.profile, {"kind": "other"}, at=at, key_ref="synthetic-key")["codes"] == ["BILLING_EVIDENCE_UNSUPPORTED"]
    finally:
        client.close(); store.close()


def test_duplicate_admit_send_and_reconcile(tmp_path, setup_plan):
    store, study, inputs, at, plan = open_study(tmp_path, setup_plan)
    auth = auth_for(study, plan, inputs)
    store.start(study, auth.authorization_id, inputs, at=at)
    client, seen = fixture_client()
    barrier = threading.Barrier(2)
    results, errors = [], []
    def launch():
        try:
            barrier.wait()
            results.append(run(store, study, inputs, at, client))
        except BaseException as exc:
            errors.append(type(exc).__name__)
    threads = [threading.Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert not errors and seen["n"] == 16
        assert sorted(item["status"] for item in results) == ["BLOCKED", "COMPLETED"]
        evidence = _evidence(store, study, plan, inputs)
        receipts, failures = [], []
        def reconcile():
            try:
                receipts.append(reconcile_study_usage(store, study, evidence))
            except BaseException as exc:
                failures.append(str(exc))
        threads = [threading.Thread(target=reconcile) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not failures and receipts[0] == receipts[1]
        with store._read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM pilot_usage_reconciliations").fetchone()[0] == 1
    finally:
        client.close(); store.close()


def test_duplicate_response_id_keeps_the_second_reservation(tmp_path, setup_plan):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client("duplicate")
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 2 and result["status"] == "PAUSED_BILLING"
        recorded = [slot for slot in store.list_slots(study) if slot["state"] == "RECORDED"]
        assert [slot["outcome"] for slot in recorded] == ["SUCCEEDED", "FAILED"]
        assert "DUPLICATE_OR_OVER_RESERVE" in recorded[1]["observation_json"]
        assert store.get_study(study)["cost"]["unknown_reserved_usd"] == pytest.approx(0.0172032)
        with pytest.raises(ExecutionError, match="RESUME_NOT_ELIGIBLE"):
            resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert seen["n"] == 2
    finally:
        client.close(); store.close()


@pytest.mark.parametrize("point,resend", [
    ("BEFORE_RESERVE", True), ("AFTER_RESERVE", False), ("AFTER_DISPATCH", False), ("AFTER_RESPONSE", False), ("AFTER_SETTLEMENT", True),
])
def test_crash_resume_never_repeats_a_claim(tmp_path, setup_plan, point, resend):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    fired = {"n": 0}
    def fault(name):
        if name == point and fired["n"] == 0:
            fired["n"] += 1
            raise RuntimeError("synthetic crash")
    client, seen = fixture_client()
    try:
        with pytest.raises(RuntimeError, match="synthetic crash"):
            run(store, study, inputs, at, client, fault=fault)
        sent = seen["n"]
        store.recover_interrupted(at=at)
        if resend:
            resumed = resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
            assert resumed["status"] == "COMPLETED" and seen["n"] == 16
            assert len({slot["call_id"] for slot in store.list_slots(study) if slot["outcome"] == "SUCCEEDED"}) == 16
        else:
            with pytest.raises(ExecutionError, match="RESUME_NOT_ELIGIBLE"):
                resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
            assert seen["n"] == sent
            assert any(slot["state"] == "UNCERTAIN" for slot in store.list_slots(study))
    finally:
        client.close(); store.close()


def test_cancel_checkpoints_do_not_undo_or_resend(tmp_path, setup_plan):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client()
    def cancel_first(name):
        if name == "BEFORE_ROLE":
            store.request_cancel(study, at=at)
    try:
        stopped = run(store, study, inputs, at, client, fault=cancel_first)
        assert seen["n"] == 0 and stopped["status"] == "CANCELLED"
        assert stopped["report"]["coverage"] == {strategy: "NOT_RUN" for strategy in STRATEGIES}
    finally:
        client.close(); store.close()
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan, name="execution-inflight.sqlite3")
    client, seen = fixture_client(on_post=lambda _request, count: store.request_cancel(study, at=at) if count["n"] == 1 else None)
    try:
        inflight = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and inflight["status"] == "CANCELLED"
        single = store.get(inflight["report"]["source_runs"]["single_agent"]["run_id"], True)["result"]
        assert single["trades"] == [] and single["decisions"][0]["status"] == "SKIPPED"
        assert single["decisions"][0]["approved_weight"] is None and single["decisions"][0]["requested_weight"] not in (0, 0.0)
        assert inflight["report"]["coverage"]["multi_agent"] == "NOT_RUN"
        replay = replay_study(store, study, inputs, at=at)
        assert seen["n"] == 1 and semantic_result(store.get(replay["replays"]["single_agent"]["run_id"], True)["result"]) == semantic_result(single)
    finally:
        client.close(); store.close()


def test_prefill_and_postfill_cancellation(tmp_path, setup_plan):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    def prefill(name):
        if name == "BEFORE_FILL" and store.execution_report(study)['coverage']['ma_crossover'] == 'COMPLETED':
            store.request_cancel(study, at=at)
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client, fault=prefill)
        single = store.get(result["report"]["source_runs"]["single_agent"]["run_id"], True)["result"]
        assert single["trades"] == [] and single["equity"][-1]["shares"] == 0
        assert single["decisions"][0]["status"] == "SKIPPED" and single["decisions"][0]["approved_weight"] is None
        assert single["decisions"][0]["requested_weight"] not in (0, 0.0, None)
    finally:
        client.close(); store.close()
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan, name="execution-postfill.sqlite3")
    def postfill(name):
        if name == "AFTER_FILL" and store.execution_report(study)['coverage']['ma_crossover'] == 'COMPLETED':
            store.request_cancel(study, at=at)
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client, fault=postfill)
        single = store.get(result["report"]["source_runs"]["single_agent"]["run_id"], True)["result"]
        assert [trade["side"] for trade in single["trades"]] == ["BUY"] and single["equity"][-1]["shares"] > 0
        assert len(single["decisions"]) == 1 and result["report"]["coverage"]["multi_agent"] == "NOT_RUN"
        assert result["status"] == "CANCELLED"
        replay = replay_study(store, study, inputs, at=at)
        assert seen["n"] == 1 and semantic_result(store.get(replay["replays"]["single_agent"]["run_id"], True)["result"]) == semantic_result(single)
    finally:
        client.close(); store.close()


def test_fast_tier_label_does_not_prove_price(tmp_path, setup_plan):
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "check_provider_contract.py"
    spec = importlib.util.spec_from_file_location("check_provider_contract_p203", tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inspect_stage = module.inspect_stage
    raw = json.dumps(OUTPUTS["decision_output"])
    envelope = {"model": "synthetic-model-v1", "status": "completed", "service_tier": "auto",
                "output": [{"type": "message", "status": "completed", "content": [{"type": "output_text", "text": raw}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    auto = inspect_stage("decision", envelope, raw, {"model_id": "synthetic-model-v1", "provider_id": "kuaiapi", "http_status": 200, "max_output_tokens": 1024})
    assert auto["eligible_for_fill"] and auto["service_tier_observation"]["proves_fast_price_or_model_change"] is False
    missing = inspect_stage("decision", {k: v for k, v in envelope.items() if k != "service_tier"}, raw, {"model_id": "synthetic-model-v1", "provider_id": "kuaiapi", "http_status": 200, "max_output_tokens": 1024})
    assert missing["eligible_for_fill"] and "TIER_MAPPING_NOT_ESTABLISHED" in missing["observation_warnings"]
    fast = inspect_stage("decision", {**envelope, "service_tier": "fast"}, raw, {"model_id": "synthetic-model-v1", "provider_id": "kuaiapi", "http_status": 200, "max_output_tokens": 1024, "service_tier": "default"})
    assert not fast["eligible_for_fill"] and "SERVICE_TIER_CONTRADICTS_REQUEST" in fast["failure_codes"]
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client("tier-fast")
    try:
        result = run(store, study, inputs, at, client)
        assert result["status"] == "PAUSED_DRIFT" and seen["n"] == 1
        observation = json.loads(store.list_slots(study)[0]["observation_json"])
        assert observation["service_tier_observation"]["value"] == "fast"
        assert observation["service_tier_observation"]["proves_fast_price_or_model_change"] is False
        assert "TIER_MAPPING_NOT_ESTABLISHED" in observation["observation_warnings"]
    finally:
        client.close(); store.close()


def test_no_automatic_retry_and_input_bound_before_reserve(tmp_path, setup_plan, monkeypatch):
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan, attempts=2)
    client, seen = fixture_client("http429")
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and result["status"] == "PAUSED_BILLING"
        stages = [slot for slot in store.list_slots(study) if slot["stage"] == "decision" and slot["decision_session"].endswith("01-06")]
        assert any(slot["attempt_no"] == 2 and slot["state"] == "RECORDED" and '"sent":false' in slot["observation_json"] for slot in stages)
        with pytest.raises(ExecutionError, match="RESUME_NOT_ELIGIBLE"):
            resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert seen["n"] == 1
    finally:
        client.close(); store.close()
    monkeypatch.setattr("app.agents.request_exceeds_input_bound", lambda _request: True)
    store, study, inputs, at, _plan = open_study(tmp_path, setup_plan, name="execution-bound.sqlite3")
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 0 and result["status"] == "COMPLETED"
        with store._read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM pilot_attempts").fetchone()[0] == 0
    finally:
        client.close(); store.close()


def test_import_and_poll_allocate_nothing(bundle):
    store, _plan, _inputs, _at = bundle
    study, _auth = admitted(bundle)
    before = state_digest(store)
    assert poll_study(store, study)["state"] == "QUEUED"
    assert state_digest(store) == before
    completed = subprocess.run([sys.executable, "-B", "-c", """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'backend'))
from app.research_service import execute_study, plan_study
assert 'app.agents' not in sys.modules
assert 'app.pilot' not in sys.modules
assert 'app.experiments' not in sys.modules
print('WORKER_IMPORT_PURE')
"""], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "WORKER_IMPORT_PURE"


def _evidence(store, study, plan, inputs, only_pending=False):
    slots = [slot for slot in store.list_slots(study) if slot["outcome"] in ("SUCCEEDED", "FAILED") and slot["observation_json"] and '"sent":true' in slot["observation_json"]]
    covered = []
    hashes = {}
    incoming = outgoing = 0
    with store._read() as conn:
        held = {row['call_id'] for row in conn.execute("SELECT call_id FROM pilot_attempts WHERE status='usage_estimated_held'")}
    for slot in slots:
        if only_pending and slot['call_id'] not in held:
            continue
        observed = json.loads(slot["observation_json"])
        if observed.get("usage_estimate_usd") is None:
            continue
        covered.append(slot["call_id"])
        hashes[slot["call_id"]] = canonical_hash(observed)
        incoming += observed["input_tokens"]
        outgoing += observed["output_tokens"]
    return {"kind": "synthetic-aggregate-usage-v1", "unit": "USD", "delayed": False, "external_requests": 0, "day_count": 1,
            "covered_call_ids": covered, "attempt_evidence_sha256": hashes, "input_tokens": incoming, "output_tokens": outgoing,
            "requests": len(covered), "actual_charge_usd": str(Decimal(incoming) * Decimal("0.2") / Decimal(1000000) + Decimal(outgoing) * Decimal("0.8") / Decimal(1000000)),
            "manifest_hash": plan.manifest_hash, "dataset_hash": plan.payload['request']['expected_dataset_hash'], "source_hash": plan.payload['source']['source_hash']}
