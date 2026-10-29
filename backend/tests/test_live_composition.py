"""Offline production composition. Injected HTTP only; no provider and no key file.

A passing run is a synthetic server binding completing stage A once and stage B
three times through send_live_attempt. Rejection-only coverage is not this file.
"""
import json
import os
import socket
import threading
import time
import traceback
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data import canonical_hash
from app.live_composition import (
    CountingCredentialSource, HeldLiveBinding, load_server_profile, offline_handler, production_document,
)
from app.live_readiness import LiveRefusal, ServerLiveApproval, classify_billing_evidence
from app.main import create_app
from app.research_contracts import StudyRequest
from app.research_runtime import build_fixture_runtime
from app.research_service import PROVIDER_ENDPOINTS, plan_study

SECRET = "synthetic-offline-secret-marker"
ENDPOINT = "https://kuaiapi.net/v1/responses"
WINDOW = {"start_date": date(2023, 1, 27), "end_date": date(2023, 1, 31)}


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


@pytest.fixture
def production_path(monkeypatch):
    """Count the production sender and fail if a fixture shortcut or loopback sender runs."""
    from app.agents import InjectedFixtureTransport
    from app.live_composition import send_live_attempt as original

    seen = {"endpoints": []}

    def wrapped(**kwargs):
        seen["endpoints"].append(kwargs["endpoint"])
        return original(**kwargs)

    def shortcut(*args, **kwargs):
        raise AssertionError("production composition used a fixture or loopback shortcut")

    monkeypatch.setattr("app.live_composition.send_live_attempt", wrapped)
    monkeypatch.setattr(InjectedFixtureTransport, "post", shortcut)
    monkeypatch.setattr("app.live_readiness.send_loopback_attempt", shortcut)
    return seen


def _request(profile, dataset_hash, protocol, attempts, **updates):
    body = dict(
        profile_id=profile.profile_id, dataset_id=profile.dataset_id, expected_dataset_hash=dataset_hash,
        requested_budget_usd=Decimal("1"), requested_max_attempts=attempts, study_protocol=protocol,
        transport_intent="live", overall_deadline_seconds=45, **WINDOW,
    )
    body.update(updates)
    return StudyRequest(**body)


def _bind(runtime, profile, request, loader, *, attempt_budget, key_ref, cohort, tariff_id=None,
          not_before=None, expires_at=None, preflight_overrides=None, settlement_overrides=None,
          fault_factory=None, cap_usd=Decimal("1"), grant_attempts=None):
    frozen, _view = plan_study(request, profile, runtime.registry, context=runtime.context)
    pricing = next(item for item in profile.private_evidence if item.kind == "pricing")
    approval = ServerLiveApproval(
        holder="server", funding_approved=True, manifest_hash=frozen.manifest_hash, profile_id=profile.profile_id)
    holder = {}

    def handler(http_request, holder=holder):
        return offline_handler(holder["binding"])(http_request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, http2=False)
    binding = HeldLiveBinding(
        profile=profile, approval=approval, tariff_id=tariff_id or pricing.reference_id,
        not_before=not_before or profile.valid_from, expires_at=expires_at or profile.expires_at,
        attempt_budget=attempt_budget, endpoint=ENDPOINT, loader=loader, client=client,
        grant={"authorization_id": "grant-" + key_ref, "key_ref": key_ref, "cohort_ref": cohort,
               "cap_usd": cap_usd, "max_attempts": attempt_budget if grant_attempts is None else grant_attempts,
               "evidence_sha256": canonical_hash({"kind": "synthetic-offline-grant", "key_ref": key_ref})},
        deadline_seconds=45, redactions=(SECRET, ENDPOINT, key_ref, cohort),
        preflight_overrides=preflight_overrides, settlement_overrides=settlement_overrides,
        fault_factory=fault_factory, seen={"n": 0})
    holder["binding"] = binding
    runtime.install_offline_binding(binding)
    return binding, frozen


def _open(tmp_path, name):
    runtime = build_fixture_runtime(tmp_path / name)
    profile = runtime.profiles["success"].model_copy(update={"profile_id": "synthetic-live"})
    dataset_hash = runtime.registry.load(profile.dataset_id).hash
    loader = CountingCredentialSource({profile.profile_id: SECRET})
    return runtime, profile, dataset_hash, loader


def _client(runtime):
    app = create_app(research=runtime, local_origins=("http://127.0.0.1:4311",))
    return TestClient(app)


def _json(request):
    return request.model_dump(mode="json")


def _wait(client, study_id, states, timeout=60):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/research/studies/{study_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["state"] in states and not last.get("worker_busy"):
            return last
        time.sleep(0.05)
    raise AssertionError(last["state"] if last else "no study")


def _assert_public_clean(text):
    for marker in (SECRET, "synthetic-live-a-group", "synthetic-live-b-group",
                   "synthetic-live-a-cohort", "synthetic-live-b-cohort", ENDPOINT, "Bearer " + SECRET):
        assert marker not in text


def _close(runtime):
    if runtime.executor is not None:
        runtime.close()


def test_server_profile_file_rejects_embedded_secrets(tmp_path):
    runtime, profile, _dataset_hash, _loader = _open(tmp_path, "profile.sqlite3")
    try:
        path = tmp_path / "synthetic-profile.json"
        path.write_text(json.dumps(profile.model_dump(mode="json")), encoding="utf-8")
        loaded = load_server_profile(path)
        assert loaded.profile_id == "synthetic-live"
        raw = profile.model_dump(mode="json")
        raw["api_key"] = SECRET
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(LiveRefusal, match="LIVE_PROFILE_REJECTED"):
            load_server_profile(path)
    finally:
        _close(runtime)


def test_production_document_match_is_not_spend_or_a_false_claim(tmp_path):
    runtime, profile, _dataset_hash, _loader = _open(tmp_path, "billing.sqlite3")
    try:
        at = profile.valid_from
        document = production_document(profile, "synthetic-live-a-group", at)
        matched = classify_billing_evidence(profile, document, at=at, key_ref="synthetic-live-a-group")
        assert matched["ok"] is True and matched["financial_proof"] is False and matched["invoice"] is False
        assert matched["invoiced_actual_usd"] is None and matched["evidence_class"] == "synthetic-document"
        assert "production_claim" not in document
        verified = dict(document)
        verified["provenance"] = {"method": "operator-verified-server-binding-v1", "sha256": document["provenance"]["sha256"]}
        operator = classify_billing_evidence(profile, verified, at=at, key_ref="synthetic-live-a-group")
        assert operator["ok"] is True and operator["evidence_class"] == "operator-verified-binding"
        assert operator["financial_proof"] is False
        claimed = dict(document)
        claimed["production_claim"] = False
        claim = classify_billing_evidence(profile, claimed, at=at, key_ref="synthetic-live-a-group")
        assert claim["ok"] is False and "BILLING_CLAIM_FIELD_REJECTED" in claim["codes"]
        omitted = {key: value for key, value in document.items() if key != "external_same_key_requests"}
        body = {key: value for key, value in omitted.items() if key != "provenance"}
        omitted["provenance"] = {"method": "synthetic-document-sha256-v1", "sha256": canonical_hash(body)}
        missing = classify_billing_evidence(profile, omitted, at=at, key_ref="synthetic-live-a-group")
        assert missing["ok"] is False and "EXTERNAL_SAME_KEY_USAGE" in missing["codes"]
        for name, overrides, code in (
            ("unit", {"runtime_unit": "balance-units"}, "UNIT_UNSUPPORTED"),
            ("multiplier", {"multiplier": "6.8"}, "MULTIPLIER_DRIFT"),
            ("delayed", {"delayed": True}, "BILLING_DELAYED"),
            ("cache", {"cache_status": "unknown"}, "CACHE_UNPROVEN"),
            ("invoice", {"invoice": True}, "BILLING_NOT_AN_INVOICE"),
        ):
            bad = production_document(profile, "synthetic-live-a-group", at, overrides=overrides)
            result = classify_billing_evidence(profile, bad, at=at, key_ref="synthetic-live-a-group")
            assert result["ok"] is False and code in result["codes"] and result["financial_proof"] is False, name
            assert result["invoiced_actual_usd"] is None
    finally:
        _close(runtime)


def test_valid_binding_completes_stage_a_once_and_stage_b_three_times(tmp_path, production_path):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "positive.sqlite3")
    stage_a = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)
    stage_b = _request(profile, dataset_hash, "acceptance-stage-b-v1", 3)
    binding_a, _frozen_a = _bind(
        runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group", cohort="synthetic-live-a-cohort")
    binding_b, _frozen_b = _bind(
        runtime, profile, stage_b, loader, attempt_budget=3, key_ref="synthetic-live-b-group", cohort="synthetic-live-b-cohort")
    try:
        with _client(runtime) as client:
            ready = client.get("/api/v1/research/readiness")
            assert ready.status_code == 200
            _assert_public_clean(ready.text)
            body = ready.json()
            assert body["live_provider"] is False and body["real_spending_authorized"] is False
            assert "OFFLINE_COMPOSITION_NOT_SPEND_APPROVAL" in body["labels"]
            assert {item["attempt_budget"] for item in body["held_approvals"]} == {1, 3}
            preview_a = client.post("/api/v1/research/preview", json=_json(stage_a))
            assert preview_a.status_code == 200, preview_a.text
            assert preview_a.json()["server_live_ready"] is True
            assert preview_a.json()["execution_authorized"] is False and preview_a.json()["real_spending_authorized"] is False
            assert preview_a.json()["plan"]["estimate"]["total_nominal_calls"] == 1
            _assert_public_clean(preview_a.text)
            ordinary_body = {
                "profile_id": "fixture-success", "dataset_id": "synthetic-spy-v1", "expected_dataset_hash": dataset_hash,
                "start_date": "2023-01-27", "end_date": "2023-01-31", "requested_budget_usd": "1",
                "requested_max_attempts": 1, "study_protocol": "acceptance-stage-a-v1", "transport_intent": "live",
                "overall_deadline_seconds": 45}
            ordinary = client.post("/api/v1/research/preview", json=ordinary_body)
            assert ordinary.status_code == 200 and ordinary.json()["server_live_ready"] is False
            blocked = client.post("/api/v1/research/studies", json={
                "manifest_hash": ordinary.json()["plan"]["manifest_hash"], "idempotency_key": "fixture-live-blocked",
                "request": ordinary_body})
            assert blocked.status_code == 409 and blocked.json()["detail"] == "LIVE_APPROVAL_REQUIRED"
            assert loader.calls == [] and binding_a.seen["n"] == 0 and binding_b.seen["n"] == 0
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview_a.json()["plan"]["manifest_hash"],
                "idempotency_key": preview_a.json()["plan"]["manifest_hash"], "request": _json(stage_a)})
            assert started.status_code == 202, started.text
            study_a = started.json()["study_id"]
            finished_a = _wait(client, study_a, {"COMPLETED", "PAUSED_BILLING", "PAUSED_DRIFT"})
            assert finished_a["state"] == "COMPLETED", finished_a.get("reason")
            assert finished_a["fixture_transport_posts"] == 1 and finished_a["coverage"] == {"single_agent": "COMPLETED"}
            assert finished_a["real_spending_authorized"] is False
            assert finished_a["cost"]["unknown_reserved_usd"] == 0
            assert finished_a["cost"]["usage_estimated_usd"] > 0
            assert finished_a["cost"]["confirmed_actual_usd"] > 0
            assert finished_a["cost"]["actual_charge_scope"] == "aggregate-matched-calls"
            assert finished_a["cost"]["invoiced_actual_usd"] is None
            assert binding_a.seen["n"] == 1 and binding_b.seen["n"] == 0 and loader.calls == ["synthetic-live"]
            assert production_path["endpoints"] == [ENDPOINT]
            _assert_public_clean(client.get(f"/api/v1/research/studies/{study_a}").text)
            preview_b = client.post("/api/v1/research/preview", json=_json(stage_b))
            assert preview_b.status_code == 200 and preview_b.json()["server_live_ready"] is True
            assert preview_b.json()["plan"]["estimate"]["total_nominal_calls"] == 3
            assert preview_b.json()["plan"]["manifest_hash"] != preview_a.json()["plan"]["manifest_hash"]
            assert runtime.total_posts() == 1
            started_b = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview_b.json()["plan"]["manifest_hash"],
                "idempotency_key": preview_b.json()["plan"]["manifest_hash"], "request": _json(stage_b)})
            assert started_b.status_code == 202, started_b.text
            study_b = started_b.json()["study_id"]
            assert study_b != study_a
            finished_b = _wait(client, study_b, {"COMPLETED", "PAUSED_BILLING", "PAUSED_DRIFT"})
            assert finished_b["state"] == "COMPLETED", finished_b.get("reason")
            assert finished_b["fixture_transport_posts"] == 3 and finished_b["coverage"] == {"multi_agent": "COMPLETED"}
            assert finished_b["cost"]["invoiced_actual_usd"] is None and finished_b["cost"]["unknown_reserved_usd"] == 0
            assert finished_b["cost"]["confirmed_actual_usd"] > 0 and finished_b["cost"]["usage_estimated_usd"] > 0
            assert finished_b["cost"]["actual_charge_scope"] == "aggregate-matched-calls"
            assert binding_b.seen["n"] == 3 and loader.calls == ["synthetic-live"] * 4
            assert production_path["endpoints"] == [ENDPOINT] * 4
            again = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview_a.json()["plan"]["manifest_hash"],
                "idempotency_key": preview_a.json()["plan"]["manifest_hash"], "request": _json(stage_a)})
            assert again.status_code == 202 and again.json()["study_id"] == study_a
            assert binding_a.seen["n"] == 1 and runtime.posts_for(study_a) == 1
            before = list(loader.calls)
            replayed = client.post(f"/api/v1/research/studies/{study_a}/replay", json={})
            assert replayed.status_code == 200, replayed.text
            assert replayed.json()["replay_posts"] == 0 and replayed.json()["replay_equal"] == {"single_agent": True}
            exported = client.get(f"/api/v1/research/studies/{study_b}/export")
            assert exported.status_code == 200
            assert exported.json()["report"]["sources"][0]["strategy"] == "multi_agent"
            reconciled = client.post(f"/api/v1/research/studies/{study_a}/reconcile", json={})
            assert reconciled.status_code == 409 and reconciled.json()["detail"] == "NOT_ELIGIBLE"
            for response in (replayed, exported, reconciled, client.get("/api/v1/research/studies")):
                _assert_public_clean(response.text)
            assert loader.calls == before and runtime.total_posts() == 4
    finally:
        _close(runtime)


def test_missing_stale_and_unfunded_bindings_do_not_load(tmp_path, production_path):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "guards.sqlite3")
    stage_a = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)
    try:
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_a))
            assert preview.status_code == 404 and preview.json()["detail"] == "PROFILE_NOT_ALLOWLISTED"
        _bind(runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group",
              cohort="synthetic-live-a-cohort", tariff_id="not-the-profile-tariff")
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_a))
            assert preview.status_code == 200 and preview.json()["server_live_ready"] is False
            refused = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview.json()["plan"]["manifest_hash"], "idempotency_key": "wrong-tariff",
                "request": _json(stage_a)})
            assert refused.status_code == 409 and refused.json()["detail"] == "LIVE_APPROVAL_SCOPE_MISMATCH"
            stale = client.post("/api/v1/research/studies", json={
                "manifest_hash": "ab" * 32, "idempotency_key": "stale-manifest", "request": _json(stage_a)})
            assert stale.status_code == 409 and stale.json()["detail"] == "STALE_MANIFEST"
            wider = _request(profile, dataset_hash, "acceptance-stage-a-v1", 2)
            wider_preview = client.post("/api/v1/research/preview", json=_json(wider))
            assert wider_preview.status_code == 200 and wider_preview.json()["server_live_ready"] is False
            over = client.post("/api/v1/research/studies", json={
                "manifest_hash": wider_preview.json()["plan"]["manifest_hash"], "idempotency_key": "over-attempts",
                "request": _json(wider)})
            assert over.status_code == 409 and over.json()["detail"] == "LIVE_APPROVAL_REQUIRED"
        assert loader.calls == [] and production_path["endpoints"] == []
    finally:
        _close(runtime)


def test_expired_binding_and_grant_cap_stop_before_load(tmp_path, production_path):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "expiry.sqlite3")
    stage_a = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)
    expired, _frozen = _bind(
        runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group",
        cohort="synthetic-live-a-cohort", not_before=profile.valid_from - timedelta(days=3),
        expires_at=profile.valid_from - timedelta(days=2))
    try:
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_a))
            assert preview.json()["server_live_ready"] is False
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview.json()["plan"]["manifest_hash"], "idempotency_key": "expired-binding",
                "request": _json(stage_a)})
            assert started.status_code == 409 and started.json()["detail"] == "LIVE_APPROVAL_SCOPE_MISMATCH"
            expired.client.close()
            runtime.live_bindings.clear()
            _bind(runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group",
                  cohort="synthetic-live-cap-cohort", cap_usd=Decimal("0"))
            preview = client.post("/api/v1/research/preview", json=_json(stage_a))
            assert preview.json()["server_live_ready"] is True
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview.json()["plan"]["manifest_hash"], "idempotency_key": "unfunded-cap",
                "request": _json(stage_a)})
            assert started.status_code == 409 and started.json()["detail"] == "AUTHORIZATION_UNAVAILABLE"
        assert loader.calls == [] and production_path["endpoints"] == []
    finally:
        _close(runtime)


def test_same_key_race_creates_one_study_and_one_post(tmp_path, production_path):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "race.sqlite3")
    stage_a = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)
    _bind(runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group", cohort="synthetic-live-a-cohort")
    try:
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_a)).json()
            payload = {"manifest_hash": preview["plan"]["manifest_hash"], "idempotency_key": preview["plan"]["manifest_hash"],
                       "request": _json(stage_a)}
            barrier = threading.Barrier(2)
            results = []

            def launch():
                barrier.wait()
                results.append(client.post("/api/v1/research/studies", json=payload))

            threads = [threading.Thread(target=launch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert [item.status_code for item in results] == [202, 202]
            assert results[0].json()["study_id"] == results[1].json()["study_id"]
            finished = _wait(client, results[0].json()["study_id"], {"COMPLETED", "PAUSED_BILLING", "PAUSED_DRIFT"})
            assert finished["state"] == "COMPLETED"
            assert loader.calls == ["synthetic-live"] and production_path["endpoints"] == [ENDPOINT]
    finally:
        _close(runtime)


def test_crash_after_dispatch_keeps_reserve_and_resume_does_not_repost(tmp_path, production_path):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "crash.sqlite3")
    stage_a = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)

    def factory(_store, _study_id):
        def fault(name):
            if name == "AFTER_DISPATCH":
                raise RuntimeError("synthetic-crash-before-post")
        return fault

    binding, _frozen = _bind(
        runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group",
        cohort="synthetic-live-a-cohort", fault_factory=factory)
    try:
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_a)).json()
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview["plan"]["manifest_hash"], "idempotency_key": "crash-before-post",
                "request": _json(stage_a)})
            assert started.status_code == 202, started.text
            study_id = started.json()["study_id"]
            paused = _wait(client, study_id, {"PAUSED_BILLING", "PAUSED_UNKNOWN", "PAUSED_DRIFT"})
            assert paused["state"] == "PAUSED_BILLING"
            assert paused["cost"]["unknown_reserved_usd"] > 0 and paused["cost"]["invoiced_actual_usd"] is None
            assert binding.seen["n"] == 0 and loader.calls == [] and production_path["endpoints"] == []
            assert runtime.store.list_slots(study_id)[0]["state"] == "DISPATCHED"
            resumed = client.post(f"/api/v1/research/studies/{study_id}/resume", json={})
            assert resumed.status_code == 409 and resumed.json()["detail"] == "RESUME_NOT_ELIGIBLE"
            assert binding.seen["n"] == 0 and loader.calls == []
            _assert_public_clean(paused and client.get(f"/api/v1/research/studies/{study_id}").text)
    finally:
        _close(runtime)


def test_cancel_during_second_role_does_not_send_the_next_call(tmp_path, production_path):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "cancel.sqlite3")
    stage_b = _request(profile, dataset_hash, "acceptance-stage-b-v1", 3)

    def factory(store, study_id):
        seen = {"n": 0}

        def fault(name):
            if name != "BEFORE_ROLE":
                return
            seen["n"] += 1
            if seen["n"] < 3:
                return
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if store.get_study(study_id)["state"] in {"CANCEL_REQUESTED", "CANCELLED"}:
                    return
                time.sleep(0.05)
            raise AssertionError("cancel was not observed")

        return fault

    binding, _frozen = _bind(
        runtime, profile, stage_b, loader, attempt_budget=3, key_ref="synthetic-live-b-group",
        cohort="synthetic-live-b-cohort", fault_factory=factory)
    try:
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_b)).json()
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview["plan"]["manifest_hash"], "idempotency_key": "cancel-second-role",
                "request": _json(stage_b)})
            study_id = started.json()["study_id"]
            deadline = time.time() + 30
            while time.time() < deadline and runtime.posts_for(study_id) < 1:
                time.sleep(0.05)
            assert runtime.posts_for(study_id) == 1
            cancelled = client.post(f"/api/v1/research/studies/{study_id}/cancel", json={})
            assert cancelled.status_code == 200, cancelled.text
            finished = _wait(client, study_id, {"CANCELLED", "PAUSED_BILLING"})
            assert finished["state"] == "CANCELLED"
            assert binding.seen["n"] == 1 and loader.calls == ["synthetic-live"]
            assert production_path["endpoints"] == [ENDPOINT]
            _assert_public_clean(cancelled.text)
    finally:
        _close(runtime)


@pytest.mark.parametrize("overrides", [
    {"delayed": True},
    {"runtime_unit": "balance-units"},
    {"external_same_key_requests": 2},
    {"cache_status": "unknown"},
])
def test_settlement_mismatch_keeps_the_full_reserve(tmp_path, production_path, overrides):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "hold.sqlite3")
    stage_a = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)
    binding, _frozen = _bind(
        runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group",
        cohort="synthetic-live-a-cohort", settlement_overrides=overrides)
    try:
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_a)).json()
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview["plan"]["manifest_hash"], "idempotency_key": "settlement-hold",
                "request": _json(stage_a)})
            finished = _wait(client, started.json()["study_id"], {"PAUSED_BILLING", "PAUSED_DRIFT", "COMPLETED"})
            assert finished["state"] == "PAUSED_BILLING", finished.get("reason")
            assert finished["cost"]["unknown_reserved_usd"] > 0
            assert finished["cost"]["invoiced_actual_usd"] is None and finished["cost"]["confirmed_actual_usd"] == 0
            assert binding.seen["n"] == 1 and loader.calls == ["synthetic-live"]
            assert production_path["endpoints"] == [ENDPOINT]
            resumed = client.post(f"/api/v1/research/studies/{started.json()['study_id']}/resume", json={})
            assert resumed.status_code == 409
            assert binding.seen["n"] == 1
    finally:
        _close(runtime)


@pytest.mark.parametrize("mounted_http", [False, True])
def test_live_profile_fixture_name_cannot_use_synthetic_reconcile(tmp_path, monkeypatch, production_path, mounted_http):
    runtime = build_fixture_runtime(tmp_path / "colliding-live-profile.sqlite3")
    profile = runtime.profiles["success"]
    dataset_hash = runtime.registry.load(profile.dataset_id).hash
    loader = CountingCredentialSource({profile.profile_id: SECRET})
    request = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)
    binding, frozen = _bind(runtime, profile, request, loader, attempt_budget=1,
        key_ref="synthetic-colliding-group", cohort="synthetic-colliding-cohort")
    binding.client.close()
    selected_http = httpx.HTTPTransport()
    if mounted_http:
        binding.client = httpx.Client(transport=httpx.MockTransport(offline_handler(binding)),
            mounts={ENDPOINT: selected_http}, follow_redirects=False)
    else:
        binding.client = httpx.Client(transport=selected_http, follow_redirects=False)
    monkeypatch.setattr(selected_http, "handle_request", offline_handler(binding))
    try:
        with _client(runtime) as client:
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": frozen.manifest_hash, "idempotency_key": "colliding-profile-once",
                "request": _json(request)})
            assert started.status_code == 202
            study_id = started.json()["study_id"]
            before = _wait(client, study_id, {"PAUSED_BILLING", "COMPLETED"})
            assert before["state"] == "PAUSED_BILLING"
            assert before["cost"]["unknown_reserved_usd"] > 0
            reconciled = client.post(f"/api/v1/research/studies/{study_id}/reconcile", json={})
            assert reconciled.status_code == 409
            assert reconciled.json()["detail"] == "NOT_ELIGIBLE"
            after = client.get(f"/api/v1/research/studies/{study_id}").json()
            assert after["cost"] == before["cost"]
            assert after["cost"]["confirmed_actual_usd"] == 0
            assert binding.seen["n"] == 1 and loader.calls == [profile.profile_id]
            assert production_path["endpoints"] == [ENDPOINT]
    finally:
        _close(runtime)


def test_preflight_cache_mismatch_pauses_before_credential_load(tmp_path, production_path):
    runtime, profile, dataset_hash, loader = _open(tmp_path, "preflight.sqlite3")
    stage_a = _request(profile, dataset_hash, "acceptance-stage-a-v1", 1)
    binding, _frozen = _bind(
        runtime, profile, stage_a, loader, attempt_budget=1, key_ref="synthetic-live-a-group",
        cohort="synthetic-live-a-cohort", preflight_overrides={"cache_status": "unknown"})
    try:
        with _client(runtime) as client:
            preview = client.post("/api/v1/research/preview", json=_json(stage_a)).json()
            assert preview["server_live_ready"] is True
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": preview["plan"]["manifest_hash"], "idempotency_key": "cache-before-load",
                "request": _json(stage_a)})
            finished = _wait(client, started.json()["study_id"], {"PAUSED_BILLING", "PAUSED_DRIFT"})
            assert finished["state"] == "PAUSED_BILLING"
            assert "CACHE_UNPROVEN" in (finished.get("reason") or "")
            assert finished["cost"] is None or finished["cost"]["unknown_reserved_usd"] == 0
            assert binding.seen["n"] == 0 and loader.calls == [] and production_path["endpoints"] == []
    finally:
        _close(runtime)


def _operator_tariff_binding(runtime, *, profile_id="operator-live-a", tariff_id="tariff-mix-0135",
                             group_ref="operator-group-a", cohort_ref="operator-cohort-a",
                             requested_budget_usd="1"):
    base = runtime.profiles["success"]
    body = {
        "kind": "production-shaped-usage-v1",
        "profile_id": profile_id,
        "provider": "kuaiapi",
        "model_id": "gpt-6.1-sol",
        "endpoint": ENDPOINT,
        "pricing_unit": "effective-usd-per-million",
        "multiplier": "1",
        "tariff_id": tariff_id,
        "group_ref": group_ref,
        "cohort_ref": cohort_ref,
        "requested_budget_usd": requested_budget_usd,
        "input_usd_per_million": "0.27",
        "output_usd_per_million": "1.35",
        "cache_read_usd_per_million": "0.0135",
        "cache_write_usd_per_million": "0.3375",
        "binding_ref": "operator-binding-ref",
        "binding_sha256": "b" * 64,
        "cache_status": "observed-consistent",
        "delayed": False,
        "external_same_key_requests": 0,
        "valid_from": base.valid_from.isoformat(),
        "expires_at": base.expires_at.isoformat(),
        "as_of_date": base.valid_from.date().isoformat(),
        "timezone": "UTC",
        "day_count": 1,
        "evidence_source": "operator-verified-binding",
    }
    clean = {k: v for k, v in body.items() if k not in {"profile_id", "cohort_ref", "requested_budget_usd", "provenance"}}
    body["provenance"] = {"method": "operator-verified-server-binding-v1", "sha256": canonical_hash(clean)}
    return body


def test_scoped_key_file_loader_is_lazy_and_enforces_private_origin_and_provider_prefix(tmp_path):
    from app.data import ROOT
    from app.live_composition import ScopedKeyFileLoader

    key_file = tmp_path / "private-kuaiapi.key"
    key_file.write_text("# comment only before start\n", encoding="utf-8")
    loader = ScopedKeyFileLoader(
        profile_id="operator-live-a", provider="kuaiapi", key_file=key_file, key_ref="operator-group-a"
    )
    assert loader.calls == [] and loader.loaded_secrets == []

    with pytest.raises(LiveRefusal, match="LIVE_KEY_ORIGIN_REJECTED"):
        ScopedKeyFileLoader(
            profile_id="operator-live-a", provider="kuaiapi", key_file=ROOT / "README.md", key_ref="operator-group-a"
        )
    with pytest.raises(LiveRefusal, match="LIVE_KEY_ORIGIN_REJECTED"):
        ScopedKeyFileLoader(
            profile_id="operator-live-a", provider="kuaiapi", key_file=tmp_path, key_ref="operator-group-a"
        )

    for bad_text in (
        "OPENAI_API_KEY=" + SECRET,
        "KUAIAPI_API_KEY=",
        f'KUAIAPI_API_KEY="{SECRET}"',
        f"KUAIAPI_API_KEY={SECRET} extra",
        f"KUAIAPI_API_KEY={SECRET}\nKUAIAPI_API_KEY={SECRET}",
    ):
        key_file.write_text(bad_text, encoding="utf-8")
        with pytest.raises(LiveRefusal, match="LIVE_CREDENTIAL_REJECTED") as exc_info:
            loader.load("operator-live-a")
        assert SECRET not in str(exc_info.value) and str(key_file) not in str(exc_info.value)

    key_file.write_text(f"# scoped operator key\nKUAIAPI_API_KEY={SECRET}\n", encoding="utf-8")
    with pytest.raises(LiveRefusal, match="LIVE_CREDENTIAL_SCOPE_MISMATCH"):
        loader.load("other-profile")
    assert loader.calls == [] and loader.loaded_secrets == []

    loaded = loader.load("operator-live-a")
    assert loaded == SECRET
    assert loader.calls == ["operator-live-a"]
    assert loader.loaded_secrets == [SECRET]


def test_operator_freeze_and_runtime_entry_complete_stage_a_and_separate_stage_b(tmp_path, monkeypatch, production_path):
    import subprocess
    import sys
    from pathlib import Path
    from app.data import ROOT
    from app.live_composition import (
        build_operator_approval_bundle, freeze_operator_manifest, load_operator_bindings,
    )
    from app.research_runtime import create_operator_live_from_environ

    runtime, _profile, _dataset_hash, _loader = _open(tmp_path, "seed.sqlite3")
    try:
        binding_a_doc = _operator_tariff_binding(
            runtime, profile_id="operator-live-a", tariff_id="tariff-mix-0135",
            group_ref="operator-group-a", cohort_ref="operator-cohort-a", requested_budget_usd="1")
        binding_b_doc = _operator_tariff_binding(
            runtime, profile_id="operator-live-b", tariff_id="tariff-mix-0135",
            group_ref="operator-group-b", cohort_ref="operator-cohort-b", requested_budget_usd="1")
    finally:
        _close(runtime)

    binding_a_path = tmp_path / "tariff-binding-a.json"
    binding_b_path = tmp_path / "tariff-binding-b.json"
    binding_a_path.write_text(json.dumps(binding_a_doc, indent=2), encoding="utf-8")
    binding_b_path.write_text(json.dumps(binding_b_doc, indent=2), encoding="utf-8")

    freeze_a_path = tmp_path / "stage-a-freeze.json"
    cli_freeze = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "run_live_operator.py"), "freeze",
         "--binding", str(binding_a_path), "--output", str(freeze_a_path), "--protocol", "acceptance-stage-a-v1"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    freeze_a = json.loads(freeze_a_path.read_text(encoding="utf-8"))
    assert freeze_a["kind"] == "operator-live-freeze-v1"
    assert freeze_a["approval_status"] == "PROPOSED_NOT_APPROVED"
    assert freeze_a["funding_approved"] is False
    assert freeze_a["study_protocol"] == "acceptance-stage-a-v1"
    assert freeze_a["view"]["estimate"]["total_nominal_calls"] == 1
    assert freeze_a["view"]["estimate"]["reservation_usd_per_attempt"] == "0.02128896"
    assert freeze_a["manifest_hash"] in cli_freeze.stdout

    freeze_b = freeze_operator_manifest(binding_b_path, protocol="acceptance-stage-b-v1")
    assert freeze_b["approval_status"] == "PROPOSED_NOT_APPROVED" and freeze_b["funding_approved"] is False
    assert freeze_b["study_protocol"] == "acceptance-stage-b-v1"
    assert freeze_b["view"]["estimate"]["total_nominal_calls"] == 3
    assert freeze_b["manifest_hash"] != freeze_a["manifest_hash"]

    key_file = tmp_path / "operator-kuaiapi.key"
    key_file.write_text("# not yet populated before start\n", encoding="utf-8")

    unapproved_path = tmp_path / "unapproved-bundle.json"
    unapproved_path.write_text(json.dumps({"kind": "operator-live-bundle-v1", "bindings": [{"freeze": freeze_a}]}), encoding="utf-8")
    check_runtime = build_fixture_runtime(tmp_path / "unapproved.sqlite3")
    try:
        with pytest.raises(LiveRefusal, match="LIVE_APPROVAL_REQUIRED"):
            load_operator_bindings(unapproved_path, runtime=check_runtime)
        embedded_secret = build_operator_approval_bundle(
            [(freeze_a, key_file)],
        )
        embedded_secret["bindings"][0]["freeze"]["profile"]["api_key"] = SECRET
        bad_secret_path = tmp_path / "embedded-secret.json"
        bad_secret_path.write_text(json.dumps(embedded_secret), encoding="utf-8")
        with pytest.raises(LiveRefusal, match="LIVE_PROFILE_REJECTED"):
            load_operator_bindings(bad_secret_path, runtime=check_runtime)

        browser_bundle = build_operator_approval_bundle([(freeze_a, key_file)])
        browser_bundle["bindings"][0]["approval"]["holder"] = "browser"
        browser_path = tmp_path / "browser-bundle.json"
        browser_path.write_text(json.dumps(browser_bundle), encoding="utf-8")
        with pytest.raises(LiveRefusal, match="LIVE_APPROVAL_REQUIRED"):
            load_operator_bindings(browser_path, runtime=check_runtime)
    finally:
        _close(check_runtime)

    bundle = build_operator_approval_bundle([(freeze_a, key_file), (freeze_b, key_file)])
    bundle_path = tmp_path / "approved-operator-bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    real_client_runtime = build_fixture_runtime(tmp_path / "real-client-check.sqlite3")
    try:
        real_bindings = load_operator_bindings(bundle_path, runtime=real_client_runtime)
        assert len(real_bindings) == 2
        for rb in real_bindings:
            assert type(rb.client) is httpx.Client
            assert isinstance(rb.client._transport, httpx.HTTPTransport)
            assert rb.client.follow_redirects is False
            assert rb.loader.calls == [] and rb.loader.loaded_secrets == []
            # A production client has no verified gateway charge yet. It must
            # never promote a token estimate into an aggregate actual charge.
            with pytest.raises(ValueError, match="VERIFIED_GATEWAY_USAGE_REQUIRED"):
                rb.usage(real_client_runtime.store, "not-sent", "not-reserved")
            assert rb.loader.calls == [] and rb.loader.loaded_secrets == []

        # Exercise the ordinary HTTPTransport path with its socket boundary
        # stubbed locally. A successful model response must still pause before
        # risk/fill when no independent gateway deduction has been supplied.
        real_binding = real_bindings[0]
        monkeypatch.setattr(real_binding.client._transport, "handle_request", offline_handler(real_binding))
        key_file.write_text(f"KUAIAPI_API_KEY={SECRET}\n", encoding="utf-8")
        with _client(real_client_runtime) as client:
            started = client.post("/api/v1/research/studies", json={
                "manifest_hash": freeze_a["manifest_hash"],
                "idempotency_key": "real-client-held-accounting", "request": freeze_a["request"]})
            assert started.status_code == 202
            study_id = started.json()["study_id"]
            held = _wait(client, study_id, {"COMPLETED", "PAUSED_BILLING", "PAUSED_DRIFT"})
            assert held["state"] == "PAUSED_BILLING"
            assert held["cost"]["attempts"] == 1
            assert held["cost"]["unknown_reserved_usd"] == float(Decimal("0.02128896"))
            assert held["cost"]["confirmed_actual_usd"] == 0
            assert real_binding.seen["n"] == 1 and real_binding.loader.calls == ["operator-live-a"]
            assert held["cost"]["invoiced_actual_usd"] is None
            assert not real_client_runtime.store.call_billing_verified(real_client_runtime.store.list_slots(study_id)[0]["slot_id"])
            assert production_path["endpoints"] == [ENDPOINT]
        production_path["endpoints"].clear()
        key_file.write_text("# not yet populated before start\n", encoding="utf-8")
    finally:
        _close(real_client_runtime)

    for bad_port in ("3002", "8002"):
        refused_serve = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "run_live_operator.py"), "serve",
             "--db", str(tmp_path / "unused.sqlite3"), "--bundle", str(bundle_path), "--port", bad_port],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert refused_serve.returncode != 0 and "REFUSED_OPERATOR_PORT" in refused_serve.stderr

    cli_check = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "run_live_operator.py"), "check-bundle",
         "--bundle", str(bundle_path)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    assert freeze_a["manifest_hash"] in cli_check.stdout and SECRET not in cli_check.stdout

    db_path = tmp_path / "operator-runtime.sqlite3"
    monkeypatch.setenv("QUANTAGENT_FIXTURE_DB", str(db_path))
    monkeypatch.setenv("QUANTAGENT_LIVE_OPERATOR_FILE", str(bundle_path))
    monkeypatch.setenv("QUANTAGENT_LOCAL_ORIGINS", "http://127.0.0.1:4311")

    seen_posts = {"a": 0, "b": 0}

    def injected_transport_factory(holder):
        def handler(http_request):
            b = holder["binding"]
            key = "a" if b.profile.profile_id == "operator-live-a" else "b"
            seen_posts[key] += 1
            return offline_handler(b)(http_request)
        return httpx.MockTransport(handler)

    app = create_operator_live_from_environ(transport_factory=injected_transport_factory)
    op_runtime = app.state.research
    loaders = [b.loader for b in op_runtime.live_bindings.values()]
    assert all(loader.calls == [] and loader.loaded_secrets == [] for loader in loaders)

    try:
        with TestClient(app) as client:
            ready = client.get("/api/v1/research/readiness")
            assert ready.status_code == 200
            assert all(loader.calls == [] for loader in loaders)

            req_a = freeze_a["request"]
            req_b = freeze_b["request"]
            prev_a = client.post("/api/v1/research/preview", json=req_a)
            assert prev_a.status_code == 200 and prev_a.json()["server_live_ready"] is True
            assert prev_a.json()["plan"]["manifest_hash"] == freeze_a["manifest_hash"]
            assert all(loader.calls == [] for loader in loaders) and seen_posts == {"a": 0, "b": 0}

            exceeds_profile = {**req_a, "requested_budget_usd": "2"}
            prev_exceeds = client.post("/api/v1/research/preview", json=exceeds_profile)
            assert prev_exceeds.status_code == 422 and prev_exceeds.json()["detail"] == "REQUESTED_CAP_EXCEEDS_PROFILE"

            wrong_cap = {**req_a, "requested_budget_usd": "0.5"}
            prev_wrong = client.post("/api/v1/research/preview", json=wrong_cap)
            assert prev_wrong.status_code == 200 and prev_wrong.json()["server_live_ready"] is False
            refused = client.post("/api/v1/research/studies", json={
                "manifest_hash": prev_wrong.json()["plan"]["manifest_hash"],
                "idempotency_key": "operator-wrong-cap", "request": wrong_cap})
            assert refused.status_code == 409 and refused.json()["detail"] == "LIVE_APPROVAL_REQUIRED"
            assert all(loader.calls == [] for loader in loaders) and seen_posts == {"a": 0, "b": 0}

            key_file.write_text(f"KUAIAPI_API_KEY={SECRET}\n", encoding="utf-8")

            started_a = client.post("/api/v1/research/studies", json={
                "manifest_hash": freeze_a["manifest_hash"],
                "idempotency_key": "operator-stage-a-once", "request": req_a})
            assert started_a.status_code == 202, started_a.text
            study_a = started_a.json()["study_id"]
            done_a = _wait(client, study_a, {"COMPLETED", "PAUSED_BILLING", "PAUSED_DRIFT"})
            assert done_a["state"] == "COMPLETED", done_a.get("reason")
            assert seen_posts == {"a": 1, "b": 0}
            assert production_path["endpoints"] == [ENDPOINT]

            loader_a = next(b.loader for b in op_runtime.live_bindings.values() if b.profile.profile_id == "operator-live-a")
            loader_b = next(b.loader for b in op_runtime.live_bindings.values() if b.profile.profile_id == "operator-live-b")
            assert loader_a.calls == ["operator-live-a"] and loader_b.calls == []

            key_file.write_text("# deleted secret after stage A; replay/export must not read key file\n", encoding="utf-8")
            exported_a = client.get(f"/api/v1/research/studies/{study_a}/export")
            assert exported_a.status_code == 200
            replayed_a = client.post(f"/api/v1/research/studies/{study_a}/replay", json={})
            assert replayed_a.status_code == 200 and replayed_a.json()["replay_posts"] == 0
            assert replayed_a.json()["replay_equal"] == {"single_agent": True}
            assert loader_a.calls == ["operator-live-a"] and seen_posts == {"a": 1, "b": 0}

            for text in (ready.text, prev_a.text, json.dumps(done_a), exported_a.text, replayed_a.text):
                for marker in (SECRET, str(key_file), str(bundle_path), "operator-group-a", "operator-cohort-a", "tariff-mix-0135", ENDPOINT):
                    assert marker not in text

            key_file.write_text(f"KUAIAPI_API_KEY={SECRET}\n", encoding="utf-8")
            prev_b = client.post("/api/v1/research/preview", json=req_b)
            assert prev_b.status_code == 200 and prev_b.json()["server_live_ready"] is True
            started_b = client.post("/api/v1/research/studies", json={
                "manifest_hash": freeze_b["manifest_hash"],
                "idempotency_key": "operator-stage-b-once", "request": req_b})
            assert started_b.status_code == 202, started_b.text
            study_b = started_b.json()["study_id"]
            done_b = _wait(client, study_b, {"COMPLETED", "PAUSED_BILLING", "PAUSED_DRIFT"})
            assert done_b["state"] == "COMPLETED", done_b.get("reason")
            assert seen_posts == {"a": 1, "b": 3}
            assert loader_b.calls == ["operator-live-b"] * 3
            assert production_path["endpoints"] == [ENDPOINT] * 4
    finally:
        _close(op_runtime)
