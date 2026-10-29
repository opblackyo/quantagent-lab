"""Acceptance-protocol geometry. Standard research stays a four-strategy plan.

Oracles count frozen roles, sessions, posts and coverage. They do not copy the
planner's arithmetic or treat a stopped 16-call plan as one accepted call.
"""
import os
import socket
import threading
import time
import traceback
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.research_contracts import StudyRequest
from app.research_report import build_study_report
from app.research_service import (
    ExecutionError, PlanningError, execute_study, plan_study, replay_study, resume_study,
)
from app.research_store import PlanningInputs, ResearchStore, StudyStorageError
from test_research_execution import TOKEN, billing_for, fixture_client, fixture_usage, run
from test_research_planner import preview, setup_plan
from test_research_store import auth_for

LEGACY_STANDARD_MANIFEST = "12fdaa2cd4182f57ebc1aba64da2b46758058dab026e208275164ce70a6738b4"
STAGE_A = "acceptance-stage-a-v1"
STAGE_B = "acceptance-stage-b-v1"
ONE_WEEK = {"start_date": date(2023, 1, 27), "end_date": date(2023, 1, 31)}


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("live provider or network is forbidden")
    from app.agents import OpenAIProvider
    from app.gateway import KuaiAPIProvider
    monkeypatch.setattr(OpenAIProvider, "__init__", forbidden)
    monkeypatch.setattr(KuaiAPIProvider, "__init__", forbidden)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def local_pipe():
        return any(frame.name in {"_fallback_socketpair", "_make_self_pipe"} for frame in traceback.extract_stack())

    def guarded_connect(self, address, *args, **kwargs):
        if local_pipe():
            return original_connect(self, address, *args, **kwargs)
        return forbidden()

    def guarded_connect_ex(self, address, *args, **kwargs):
        if local_pipe():
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


def _request(parts, **updates):
    return parts[0].model_copy(update=updates)


def _plan(parts, **updates):
    request = _request(parts, **updates)
    frozen, view = plan_study(request, parts[1], parts[2], context=parts[3])
    return request, frozen, view


def _queue(tmp_path, parts, name, **updates):
    request = _request(parts, **updates)
    profile, registry, context = parts[1], parts[2], parts[3]
    inputs = PlanningInputs(request, profile, registry, context)
    plan, view = plan_study(request, profile, registry, context=context)
    store = ResearchStore.create_new(tmp_path / name)
    study = store.register(plan, inputs, idempotency_key=name)
    store.freeze(study, inputs, at=context.checked_at)
    auth = auth_for(study, plan, inputs)
    store.record_authorization(auth, at=context.checked_at)
    store.start(study, auth.authorization_id, inputs, at=context.checked_at)
    return store, study, inputs, context.checked_at, plan, view


def test_legacy_standard_manifest_and_sixteen_call_geometry_stay(setup_plan):
    frozen, view = preview(setup_plan)
    assert frozen.manifest_hash == LEGACY_STANDARD_MANIFEST
    assert "study_protocol" not in frozen.payload["request"]
    assert view.strategy_ids == ("buy_hold", "ma_crossover", "single_agent", "multi_agent")
    assert [(item.decision.day, item.execution.day) for item in view.sessions] == [(6, 9), (13, 17), (20, 23), (27, 30)]
    assert view.estimate.total_nominal_calls == view.estimate.complete_attempt_ceiling == 16
    assert view.estimate.baseline_calls == 0 and view.execution_authorized is False
    again = preview(setup_plan)[0]
    assert again.canonical_payload == frozen.canonical_payload


def test_standard_attempt_cap_remains_a_partial_sixteen_call_plan(tmp_path, setup_plan):
    store, study, inputs, at, _plan, view = _queue(
        tmp_path, setup_plan, "standard-cap.sqlite3", requested_max_attempts=1, requested_budget_usd=Decimal("1"))
    client, seen = fixture_client()
    try:
        assert view.estimate.total_nominal_calls == 16 and view.estimate.all_attempts_fit_requested_caps is False
        assert getattr(view, "study_protocol", None) is None
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and result["status"] == "PAUSED_BILLING"
        assert set(result["report"]["coverage"]) == {"buy_hold", "ma_crossover", "single_agent", "multi_agent"}
        assert result["report"]["coverage"]["multi_agent"] == "NOT_RUN"
    finally:
        client.close()
        store.close()


def test_stage_a_preview_freezes_one_single_agent_decision(setup_plan):
    _request_a, frozen, view = _plan(setup_plan, study_protocol=STAGE_A, requested_max_attempts=32, **ONE_WEEK)
    assert view.estimate.single_nominal_calls == 1 and view.estimate.multi_nominal_calls == 0
    assert view.estimate.total_nominal_calls == view.estimate.complete_attempt_ceiling == 1
    assert view.estimate.capped_attempt_ceiling == 1 and view.estimate.all_attempts_fit_requested_caps is True
    assert view.strategy_ids == ("single_agent",)
    assert list(frozen.payload["strategy_definitions"]) == ["single_agent"]
    assert frozen.payload["strategy_definitions"]["single_agent"]["roles"] == ["decision"]
    assert len(frozen.payload["sessions"]) == 1
    assert frozen.payload["request"]["study_protocol"] == STAGE_A
    assert frozen.payload["policy"]["acceptance_protocol"]["real_spending_authorized"] is False
    assert frozen.payload["policy"]["acceptance_protocol"]["baselines_included"] is False
    assert view.execution_authorized is False
    assert "ACCEPTANCE_NOT_FULL_FOUR_STRATEGY_RESEARCH" in view.warnings


def test_stage_b_preview_freezes_three_multi_agent_roles(setup_plan):
    _request_b, frozen, view = _plan(setup_plan, study_protocol=STAGE_B, requested_max_attempts=32, **ONE_WEEK)
    assert view.estimate.single_nominal_calls == 0 and view.estimate.multi_nominal_calls == 3
    assert view.estimate.total_nominal_calls == view.estimate.complete_attempt_ceiling == 3
    assert view.strategy_ids == ("multi_agent",)
    roles = [row["stage"] for row in frozen.payload["decision_inputs"][0]["role_input_templates"]["multi_agent"]]
    assert roles == ["analysis", "critic", "decision"]
    assert "single_agent" not in frozen.payload["strategy_definitions"]
    assert view.manifest_hash != _plan(setup_plan, study_protocol=STAGE_A, requested_max_attempts=32, **ONE_WEEK)[1].manifest_hash


def test_acceptance_rejects_many_weeks_retry_and_untrusted_roles(setup_plan):
    with pytest.raises(PlanningError, match="ACCEPTANCE_REQUIRES_ONE_DECISION_WEEK"):
        _plan(setup_plan, study_protocol=STAGE_A)
    with pytest.raises(PlanningError, match="ACCEPTANCE_REQUIRES_ONE_DECISION_WEEK"):
        _plan(setup_plan, study_protocol=STAGE_B, start_date=date(2023, 1, 20), end_date=date(2023, 1, 31))
    with pytest.raises(PlanningError, match="CALENDAR_OR_WARMUP_INVALID"):
        _plan(setup_plan, study_protocol=STAGE_A, start_date=date(2023, 1, 27), end_date=date(2023, 1, 27))
    profile = setup_plan[1].model_copy(update={"attempts_per_role": 2})
    with pytest.raises(PlanningError, match="ACCEPTANCE_RETRY_NOT_ALLOWED"):
        plan_study(_request(setup_plan, study_protocol=STAGE_A, **ONE_WEEK), profile, setup_plan[2], context=setup_plan[3])
    body = setup_plan[0].model_dump(mode="json")
    body.update({"study_protocol": STAGE_A, "roles": ["analysis", "decision"], "requested_max_attempts": 0})
    with pytest.raises(ValidationError):
        StudyRequest.model_validate(body)
    body.pop("roles")
    with pytest.raises(ValidationError):
        StudyRequest.model_validate(body)
    body["requested_max_attempts"] = 1
    body["study_protocol"] = "stage-c"
    with pytest.raises(ValidationError):
        StudyRequest.model_validate(body)


def test_stage_a_posts_once_and_replays_without_provider(tmp_path, setup_plan):
    store, study, inputs, at, plan, _view = _queue(
        tmp_path, setup_plan, "stage-a.sqlite3", study_protocol=STAGE_A, requested_max_attempts=32, **ONE_WEEK)
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and result["status"] == "COMPLETED"
        assert result["report"]["coverage"] == {"single_agent": "COMPLETED"}
        slots = store.list_slots(study)
        assert len(slots) == 1 and slots[0]["strategy"] == "single_agent" and slots[0]["stage"] == "decision"
        source_id = result["report"]["source_runs"]["single_agent"]["run_id"]
        payload = plan.canonical_payload
        replay = replay_study(store, study, inputs, at=at)
        assert seen["n"] == 1 and replay["posts"] == 0 and replay["replays"]["single_agent"]["equal"] is True
        assert "multi_agent" not in replay["replays"] and "buy_hold" not in replay["replays"]
        assert store.load_plan(study).canonical_payload == payload
        assert result["report"]["source_runs"]["single_agent"]["run_id"] == source_id
        report = build_study_report(store, study, transport_status="synthetic-fixture-not-live-api")
        assert report["coverage"]["denominator"] == 1 and report["coverage"]["verdict"] == "COMPLETE"
        assert [item["strategy"] for item in report["sources"]] == ["single_agent"]
        assert report["cost"]["invoiced_actual_usd"] is None
        assert any("not four-strategy research" in item for item in report["limitations"])
        assert report["versions"]["transport"] == "synthetic-fixture-not-live-api"
    finally:
        client.close()
        store.close()


def test_stage_b_posts_three_roles_and_is_not_started_by_stage_a(tmp_path, setup_plan):
    store_a, study_a, inputs_a, at_a, _plan_a, _view_a = _queue(
        tmp_path, setup_plan, "stage-a-first.sqlite3", study_protocol=STAGE_A, requested_max_attempts=1, **ONE_WEEK)
    client_a, seen_a = fixture_client()
    try:
        finished = run(store_a, study_a, inputs_a, at_a, client_a)
        assert seen_a["n"] == 1 and finished["status"] == "COMPLETED"
    finally:
        client_a.close()
        store_a.close()
    store, study, inputs, at, _plan, _view = _queue(
        tmp_path, setup_plan, "stage-b.sqlite3", study_protocol=STAGE_B, requested_max_attempts=32,
        requested_budget_usd=Decimal("1"), **ONE_WEEK)
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client)
        assert seen["n"] == 3 and result["status"] == "COMPLETED"
        assert set(result["report"]["coverage"]) == {"multi_agent"}
        assert sorted(slot["stage"] for slot in store.list_slots(study)) == ["analysis", "critic", "decision"]
        assert {slot["strategy"] for slot in store.list_slots(study)} == {"multi_agent"}
        report = build_study_report(store, study, transport_status="synthetic-fixture-not-live-api")
        assert report["coverage"]["strategy_denominator"] == 1
        assert report["coverage"]["session_denominator"] == 1 and report["coverage"]["recorded_sessions"] == 1
        assert result["report"]["plan"]["estimate"]["total_nominal_calls"] == 3
        assert [item["strategy"] for item in report["sources"]] == ["multi_agent"]
    finally:
        client.close()
        store.close()


def test_acceptance_caps_refuse_start_and_do_not_send(tmp_path, setup_plan):
    _request_b, _frozen, view = _plan(
        setup_plan, study_protocol=STAGE_B, requested_max_attempts=1, requested_budget_usd=Decimal("1"), **ONE_WEEK)
    assert view.estimate.total_nominal_calls == 3 and view.estimate.all_attempts_fit_requested_caps is False
    assert "ACCEPTANCE_PROTOCOL_CAPS_INSUFFICIENT" in view.warnings
    store, study, inputs, at, _saved, _ignored = _queue(
        tmp_path, setup_plan, "short-budget.sqlite3", study_protocol=STAGE_A,
        requested_max_attempts=32, requested_budget_usd=Decimal("0.01"), **ONE_WEEK)
    client, seen = fixture_client()
    try:
        with pytest.raises(ExecutionError, match="ACCEPTANCE_PROTOCOL_CAPS_INSUFFICIENT"):
            run(store, study, inputs, at, client)
        assert seen["n"] == 0 and store.get_study(study)["state"] == "QUEUED"
    finally:
        client.close()
        store.close()


def test_protocol_idempotency_crash_and_cancel_do_not_expand_or_resend(tmp_path, setup_plan):
    request_a, plan_a, _view_a = _plan(setup_plan, study_protocol=STAGE_A, requested_max_attempts=4, **ONE_WEEK)
    request_b, plan_b, _view_b = _plan(setup_plan, study_protocol=STAGE_B, requested_max_attempts=4, **ONE_WEEK)
    inputs_a = PlanningInputs(request_a, setup_plan[1], setup_plan[2], setup_plan[3])
    inputs_b = PlanningInputs(request_b, setup_plan[1], setup_plan[2], setup_plan[3])
    store = ResearchStore.create_new(tmp_path / "idempotency.sqlite3")
    try:
        store.register(plan_a, inputs_a, idempotency_key="same-click")
        with pytest.raises(StudyStorageError, match="IDEMPOTENCY_CONFLICT"):
            store.register(plan_b, inputs_b, idempotency_key="same-click")
    finally:
        store.close()

    store, study, inputs, at, _saved, _ignored = _queue(
        tmp_path, setup_plan, "lost-response.sqlite3", study_protocol=STAGE_A, requested_max_attempts=4, **ONE_WEEK)
    fired = {"n": 0}

    def fault(name):
        if name == "AFTER_RESPONSE" and fired["n"] == 0:
            fired["n"] += 1
            raise RuntimeError("synthetic lost response")

    client, seen = fixture_client()
    try:
        with pytest.raises(RuntimeError, match="synthetic lost response"):
            run(store, study, inputs, at, client, fault=fault)
        held = store.get_study(study)["cost"]["unknown_reserved_usd"]
        assert seen["n"] == 1 and held > 0
        store.recover_interrupted(at=at)
        with pytest.raises(ExecutionError, match="RESUME_NOT_ELIGIBLE"):
            resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN,
                         billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert seen["n"] == 1 and store.get_study(study)["cost"]["unknown_reserved_usd"] == held
        replay = replay_study(store, study, inputs, at=at)
        assert replay["posts"] == 0 and seen["n"] == 1
    finally:
        client.close()
        store.close()

    store, study, inputs, at, _saved, _ignored = _queue(
        tmp_path, setup_plan, "cancel-b.sqlite3", study_protocol=STAGE_B, requested_max_attempts=4, **ONE_WEEK)
    client, seen = fixture_client(on_post=lambda _request, count: store.request_cancel(study, at=at) if count["n"] == 1 else None)
    try:
        stopped = run(store, study, inputs, at, client)
        assert seen["n"] == 1 and stopped["status"] == "CANCELLED"
        assert set(stopped["report"]["coverage"]) == {"multi_agent"}
        with pytest.raises(ExecutionError, match="RESUME_NOT_ELIGIBLE"):
            resume_study(store, study, inputs, at=at, service_enabled=True, client=client, authorization=TOKEN,
                         billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert seen["n"] == 1
        report = build_study_report(store, study, transport_status="synthetic-fixture-not-live-api")
        assert report["coverage"]["verdict"] == "INCOMPLETE"
        assert [item["strategy"] for item in report["sources"]] == ["multi_agent"]
    finally:
        client.close()
        store.close()


def test_duplicate_stage_a_execution_sends_once(tmp_path, setup_plan):
    store, study, inputs, at, _saved, _ignored = _queue(
        tmp_path, setup_plan, "duplicate-a.sqlite3", study_protocol=STAGE_A, requested_max_attempts=4, **ONE_WEEK)
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
        assert not errors and seen["n"] == 1
        assert sorted(item["status"] for item in results) == ["BLOCKED", "COMPLETED"]
    finally:
        client.close()
        store.close()


def test_http_preview_is_silent_and_partial_acceptance_does_not_start(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.research_runtime import build_fixture_runtime
    runtime = build_fixture_runtime(tmp_path / "acceptance-http.sqlite3")
    app = create_app(research=runtime, local_origins=("http://127.0.0.1:4311",))
    with TestClient(app) as client:
        dataset_hash = next(item["sha256"] for item in client.get("/api/v1/datasets").json() if item["id"] == "synthetic-spy-v1")
        base = {"profile_id": "fixture-success", "dataset_id": "synthetic-spy-v1", "expected_dataset_hash": dataset_hash,
                "start_date": "2023-01-01", "end_date": "2023-01-31", "requested_budget_usd": "1", "requested_max_attempts": 32,
                "study_protocol": STAGE_A}
        rejected = client.post("/api/v1/research/preview", json=base)
        assert rejected.status_code == 422 and rejected.json()["detail"] == "ACCEPTANCE_REQUIRES_ONE_DECISION_WEEK"
        assert runtime.total_posts() == 0
        one_week = {**base, "start_date": "2023-01-27", "end_date": "2023-01-31", "study_protocol": STAGE_B, "requested_max_attempts": 1}
        shown = client.post("/api/v1/research/preview", json=one_week)
        assert shown.status_code == 200 and shown.json()["real_spending_authorized"] is False
        assert shown.json()["plan"]["estimate"]["total_nominal_calls"] == 3
        assert "ACCEPTANCE_PROTOCOL_CAPS_INSUFFICIENT" in shown.json()["plan"]["warnings"]
        blocked = client.post("/api/v1/research/studies", json={"manifest_hash": shown.json()["plan"]["manifest_hash"],
                                                                "idempotency_key": "stage-b-short", "request": one_week})
        assert blocked.status_code == 422 and blocked.json()["detail"] == "ACCEPTANCE_PROTOCOL_CAPS_INSUFFICIENT"
        assert runtime.total_posts() == 0
        stage_a = {**one_week, "study_protocol": STAGE_A, "requested_max_attempts": 32}
        planned = client.post("/api/v1/research/preview", json=stage_a)
        assert planned.status_code == 200 and planned.json()["plan"]["estimate"]["total_nominal_calls"] == 1
        started = client.post("/api/v1/research/studies", json={"manifest_hash": planned.json()["plan"]["manifest_hash"],
                                                                "idempotency_key": planned.json()["plan"]["manifest_hash"], "request": stage_a})
        assert started.status_code == 202
        study_id = started.json()["study_id"]
        deadline = time.time() + 60
        current = started.json()
        while time.time() < deadline and (current["state"] not in {"COMPLETED", "PAUSED_BILLING"} or current.get("worker_busy")):
            time.sleep(0.05)
            current = client.get(f"/api/v1/research/studies/{study_id}").json()
        assert current["state"] == "COMPLETED" and current["coverage"] == {"single_agent": "COMPLETED"}
        assert current["fixture_transport_posts"] == 1 and current["real_spending_authorized"] is False
        again = client.post("/api/v1/research/studies", json={"manifest_hash": planned.json()["plan"]["manifest_hash"],
                                                              "idempotency_key": planned.json()["plan"]["manifest_hash"], "request": stage_a})
        assert again.status_code == 202 and again.json()["study_id"] == study_id and again.json()["fixture_transport_posts"] == 1
        missing = client.get(f"/api/v1/research/studies/{study_id}/sources/buy_hold")
        assert missing.status_code == 404 and missing.json()["detail"] == "NOT_IN_STUDY_PROTOCOL"
        exported = client.get(f"/api/v1/research/studies/{study_id}/export")
        assert exported.status_code == 200
        assert exported.json()["report"]["coverage"]["denominator"] == 1
        assert runtime.total_posts() == 1
