"""Local research HTTP contracts. Synthetic markers only; no live provider."""
import asyncio
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data import DatasetRegistry, ROOT
from app.gateway import application_launch_block
from app.main import create_app
from app.models import ENGINE_VERSION, FEATURE_VERSION, ExperimentConfig
from app.research_runtime import build_fixture_runtime
from app.research_store import ResearchStore, StudyStorageError

MARKERS = (
    "sk-syntheticfixturesecret",
    "synthetic-fixture-token",
    "fixture-success-key",
    "fixture-delayed-key",
    "fixture-timeout-key",
    "fixture-drift-key",
    "fixture-cancel-key",
    "fixture-success-cohort",
    "fixture-delayed-cohort",
    "fixture-binding-ref",
    "fixture-price-ref",
    "https://kuaiapi.net/",
    "https://api.openai.com/",
    "account-secret-marker",
    "ops@example.com",
    "fixture-group-marker",
    "C:\\Users\\fixture\\secret",
    "bearer synthetic-token-marker",
    "grant-fixture-success",
)


def assert_clean(text):
    for marker in MARKERS:
        assert marker not in text


def request_for(profile, dataset_hash, **updates):
    body = {
        "profile_id": profile,
        "dataset_id": "synthetic-spy-v1",
        "expected_dataset_hash": dataset_hash,
        "start_date": "2023-01-01",
        "end_date": "2023-01-31",
        "requested_budget_usd": "1",
        "requested_max_attempts": 32,
    }
    body.update(updates)
    return body


def wait_for(client, study_id, states, timeout=90):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/research/studies/{study_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        assert_clean(response.text)
        if last["state"] in states and not last.get('worker_busy', False):
            return last
        time.sleep(0.05)
    raise AssertionError(last["state"] if last else "no study")


@pytest.fixture
def opened(tmp_path):
    runtime = build_fixture_runtime(tmp_path / "phase2.sqlite3")
    app = create_app(research=runtime, local_origins=("http://127.0.0.1:4311",))
    with TestClient(app) as client:
        dataset_hash = next(item["sha256"] for item in client.get("/api/v1/datasets").json() if item["id"] == "synthetic-spy-v1")
        yield runtime, client, dataset_hash


def preview(client, body):
    response = client.post("/api/v1/research/preview", json=body)
    assert response.status_code == 200, response.text
    assert_clean(response.text)
    return response.json()


def start(client, body, key):
    planned = preview(client, body)
    response = client.post("/api/v1/research/studies", json={"manifest_hash": planned["plan"]["manifest_hash"], "idempotency_key": key, "request": body})
    return planned, response


def test_default_app_stays_disabled_and_mock_still_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-syntheticfixturesecret")
    monkeypatch.setenv("LLM_MODEL", "should-not-be-read")
    app = create_app(tmp_path / "plain.sqlite3")
    with TestClient(app) as client:
        ready = client.get("/api/v1/research/readiness")
        assert ready.status_code == 200
        body = ready.json()
        assert body["service"] == "disabled" and body["live_provider"] is False and body["profiles"] == []
        assert_clean(ready.text)
        capabilities = client.get("/api/v1/system/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["api"]["available"] is False
        assert capabilities.json()["live_provider"] is False
        assert "api" not in capabilities.json()["modes"]
        assert_clean(capabilities.text)
        dataset_hash = next(item["sha256"] for item in client.get("/api/v1/datasets").json() if item["id"] == "synthetic-spy-v1")
        blocked = client.post("/api/v1/research/preview", json=request_for("fixture-success", dataset_hash))
        assert blocked.status_code == 409 and blocked.json()["detail"] == "SERVICE_DISABLED"
        created = client.post("/api/v1/experiments", json={"strategy_id": "buy_hold", "llm_mode": "mock", "dataset_id": "synthetic-spy-v1"})
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/experiments/{run_id}").json()
            if current["status"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.05)
        assert current["status"] == "COMPLETED"
        denied = client.post("/api/v1/experiments", json={"strategy_id": "single_agent", "llm_mode": "api", "model_provider": "openai", "model_id": "synthetic-model-v1"})
        assert denied.status_code == 422 and denied.json()["detail"] == application_launch_block("openai")


def test_preview_is_pure_and_rejects_client_secrets(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    import app.agents as agents
    import app.gateway as gateway
    monkeypatch.setattr(agents.OpenAIProvider, "__init__", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider")))
    monkeypatch.setattr(gateway.KuaiAPIProvider, "__init__", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider")))
    before = runtime.total_posts()
    body = request_for("fixture-success", dataset_hash)
    planned = preview(client, body)
    assert runtime.total_posts() == before == 0
    assert planned["execution_authorized"] is False and planned["preview_is_not_funding"] is True
    assert planned["plan"]["estimate"]["baseline_calls"] == 0
    assert "input_usd_per_million" not in json.dumps(planned)
    injected = client.post("/api/v1/research/preview", json={**body, "approved": True, "api_key": "sk-syntheticfixturesecret", "endpoint": "https://kuaiapi.net/v1/responses", "group_ref": "fixture-group-marker"})
    assert injected.status_code == 422 and injected.json() == {"detail": "REQUEST_BODY_REJECTED"}
    assert_clean(injected.text)
    stale = client.post("/api/v1/research/studies", json={"manifest_hash": planned["plan"]["manifest_hash"], "idempotency_key": "stale-key-1", "request": {**body, "end_date": "2023-01-20"}})
    assert stale.status_code == 409 and stale.json()["detail"] == "STALE_MANIFEST"
    assert_clean(stale.text)


def test_duplicate_and_queue_and_cancel(opened):
    runtime, client, dataset_hash = opened
    body = request_for("fixture-cancel", dataset_hash)
    planned, first = start(client, body, "cancel-once")
    assert first.status_code == 202, first.text
    study_id = first.json()["study_id"]
    second = client.post("/api/v1/research/studies", json={"manifest_hash": planned["plan"]["manifest_hash"], "idempotency_key": "cancel-once", "request": body})
    assert second.status_code == 202 and second.json()["study_id"] == study_id
    other = request_for("fixture-success", dataset_hash)
    other_plan = preview(client, other)
    queued = client.post("/api/v1/research/studies", json={"manifest_hash": other_plan["plan"]["manifest_hash"], "idempotency_key": "other-study", "request": other})
    assert queued.status_code == 409 and queued.json()["detail"] == "WORK_QUEUE_FULL"
    cancelled = client.post(f"/api/v1/research/studies/{study_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    final = wait_for(client, study_id, {"CANCELLED"})
    assert final["fixture_transport_posts"] == 0
    assert runtime.posts_for(study_id) == 0


def test_success_lease_replay_and_sources(opened):
    runtime, client, dataset_hash = opened
    body = request_for("fixture-success", dataset_hash)
    planned, first = start(client, body, "a" * 16)
    assert first.status_code == 202, first.text
    study_id = first.json()["study_id"]
    again = client.post("/api/v1/research/studies", json={"manifest_hash": planned["plan"]["manifest_hash"], "idempotency_key": "a" * 16, "request": body})
    assert again.json()["study_id"] == study_id
    final = wait_for(client, study_id, {"COMPLETED", "FAILED", "PAUSED_BILLING", "PAUSED_DRIFT"})
    assert final["state"] == "COMPLETED", final["reason"]
    nominal = planned["plan"]["estimate"]["total_nominal_calls"]
    assert nominal == 16
    assert final["fixture_transport_posts"] == 16
    assert runtime.total_posts() == 16
    assert final["coverage"] == {name: "COMPLETED" for name in ("buy_hold", "ma_crossover", "single_agent", "multi_agent")}
    assert final["execution_authorized"] is False and final["cost"]["invoiced_actual_usd"] is None
    for strategy in ("buy_hold", "ma_crossover"):
        source = client.get(f"/api/v1/research/studies/{study_id}/sources/{strategy}")
        assert source.status_code == 200
        payload = source.json()
        assert payload["complete"] is True and payload["metrics"]["llm_calls"] == 0 and payload["origin"] == "source"
        assert_clean(source.text)
    multi = client.get(f"/api/v1/research/studies/{study_id}/sources/multi_agent").json()
    assert multi["decisions"] and multi["decisions"][0]["analysis_output"]["regime"]
    missing = client.get(f"/api/v1/research/studies/{study_id}/sources/not-a-strategy")
    assert missing.status_code == 404 and missing.json()["detail"] == "UNKNOWN_SOURCE"
    before_posts = client.get(f"/api/v1/research/studies/{study_id}").json()["fixture_transport_posts"]
    source_ids = {name: item["run_id"] for name, item in final["source_runs"].items()}
    replayed = client.post(f"/api/v1/research/studies/{study_id}/replay", json={})
    assert replayed.status_code == 200, replayed.text
    replay_body = replayed.json()
    assert replay_body["replay_posts"] == 0
    assert replay_body["fixture_transport_posts"] == before_posts
    assert runtime.total_posts() == 16
    for name, run_id in source_ids.items():
        assert replay_body["source_runs"][name]["run_id"] == run_id
        assert replay_body["replay_runs"][name]["run_id"] != run_id
    exported = client.get(f"/api/v1/research/studies/{study_id}/export")
    assert exported.status_code == 200
    assert_clean(exported.text)
    for secret in runtime.secret_values():
        assert secret not in exported.text
    conflict = client.post("/api/v1/research/studies", json={"manifest_hash": planned["plan"]["manifest_hash"], "idempotency_key": "b" * 16, "request": body})
    assert conflict.status_code == 409 and conflict.json()["detail"] == "APPROVAL_OR_COHORT_ALREADY_ADMITTED"


def test_delayed_resume_and_timeout_hold(opened):
    runtime, client, dataset_hash = opened
    delayed_body = request_for("fixture-delayed", dataset_hash)
    _planned, started = start(client, delayed_body, "delayed-once")
    study_id = started.json()["study_id"]
    paused = wait_for(client, study_id, {"PAUSED_BILLING", "PAUSED_DRIFT", "COMPLETED"})
    assert paused["state"] == "PAUSED_BILLING"
    assert paused["cost"]["unknown_reserved_usd"] > 0
    early = client.post(f"/api/v1/research/studies/{study_id}/resume", json={})
    assert early.status_code == 409 and early.json()["detail"] == "RESUME_NOT_ELIGIBLE"
    reconciled = client.post(f"/api/v1/research/studies/{study_id}/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    assert_clean(reconciled.text)
    held = client.get(f"/api/v1/research/studies/{study_id}").json()
    assert held["cost"]["unknown_reserved_usd"] == 0
    assert held["cost"]["invoiced_actual_usd"] is None
    resumed = client.post(f"/api/v1/research/studies/{study_id}/resume", json={})
    assert resumed.status_code == 202, resumed.text
    finished = wait_for(client, study_id, {"COMPLETED", "PAUSED_BILLING", "PAUSED_DRIFT"})
    assert finished["state"] == "COMPLETED"
    assert finished["fixture_transport_posts"] == 16
    timeout_body = request_for("fixture-timeout", dataset_hash)
    _plan, timeout = start(client, timeout_body, "timeout-once")
    timeout_id = timeout.json()["study_id"]
    held_timeout = wait_for(client, timeout_id, {"PAUSED_BILLING", "PAUSED_DRIFT", "COMPLETED"})
    assert held_timeout["state"] == "PAUSED_BILLING"
    assert held_timeout["cost"]["unknown_reserved_usd"] > 0
    assert held_timeout["fixture_transport_posts"] == 1
    refused = client.post(f"/api/v1/research/studies/{timeout_id}/reconcile", json={})
    assert refused.status_code == 409 and refused.json()["detail"] == "NOT_ELIGIBLE"
    still = client.get(f"/api/v1/research/studies/{timeout_id}").json()
    assert still["cost"]["unknown_reserved_usd"] > 0
    resume = client.post(f"/api/v1/research/studies/{timeout_id}/resume", json={})
    assert resume.status_code == 409 and resume.json()["detail"] == "RESUME_NOT_ELIGIBLE"
    drift_body = request_for("fixture-drift", dataset_hash)
    _drift_plan, drift = start(client, drift_body, "drift-once")
    drift_id = drift.json()["study_id"]
    drifted = wait_for(client, drift_id, {"PAUSED_DRIFT", "PAUSED_BILLING", "COMPLETED"})
    assert drifted["state"] == "PAUSED_DRIFT"
    assert client.post(f"/api/v1/research/studies/{drift_id}/resume", json={}).status_code == 409
    assert client.post(f"/api/v1/research/studies/{drift_id}/reconcile", json={}).json()["detail"] == "NOT_ELIGIBLE"
    assert runtime.posts_for(drift_id) == 1


def test_concurrent_same_study_admits_once(tmp_path):
    runtime = build_fixture_runtime(tmp_path / "concurrent.sqlite3")
    app = create_app(research=runtime, local_origins=())
    dataset_hash = DatasetRegistry([ROOT / "data" / "sample"]).load("synthetic-spy-v1").hash
    body = request_for("fixture-success", dataset_hash)

    async def once():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            planned = await client.post("/api/v1/research/preview", json=body)
            assert planned.status_code == 200, planned.text
            payload = {"manifest_hash": planned.json()["plan"]["manifest_hash"], "idempotency_key": "same-concurrent", "request": body}

            async def send():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as local:
                    return await local.post("/api/v1/research/studies", json=payload)

            first, second = await asyncio.gather(send(), send())
            assert first.status_code == 202 and second.status_code == 202
            assert first.json()["study_id"] == second.json()["study_id"]
            study_id = first.json()["study_id"]
            deadline = time.time() + 90
            state = None
            while time.time() < deadline:
                state = (await client.get(f"/api/v1/research/studies/{study_id}")).json()["state"]
                if state in {"COMPLETED", "FAILED", "PAUSED_BILLING", "PAUSED_DRIFT"}:
                    break
                await asyncio.sleep(0.05)
            assert state == "COMPLETED"
            assert runtime.posts_for(study_id) == 16

    try:
        asyncio.run(once())
    finally:
        runtime.close()


def test_domain_database_survives_restart_without_mutation(tmp_path):
    path = tmp_path / "restart.sqlite3"
    runtime = build_fixture_runtime(path)
    app = create_app(research=runtime, local_origins=())
    with TestClient(app) as client:
        dataset_hash = next(item["sha256"] for item in client.get("/api/v1/datasets").json() if item["id"] == "synthetic-spy-v1")
        _planned, started = start(client, request_for("fixture-cancel", dataset_hash), "restart-cancel")
        study_id = started.json()["study_id"]
        assert client.post(f"/api/v1/research/studies/{study_id}/cancel", json={}).status_code == 200
        final = wait_for(client, study_id, {"CANCELLED"})
        with runtime.store._read() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        saved = (version, final["state"], final["study_id"])
    reopened = ResearchStore.open_existing(path)
    try:
        listed = reopened.list_studies()
        assert listed[0]["study_id"] == saved[2] and listed[0]["state"] == "CANCELLED"
        with reopened._read() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == saved[0]
        with pytest.raises(StudyStorageError, match="USE_EXPLICIT_RESEARCH_INITIALIZATION"):
            reopened.initialize()
    finally:
        reopened.close()
    with pytest.raises(StudyStorageError, match="FIXTURE_DB_EXISTS"):
        build_fixture_runtime(path)


def test_legacy_and_research_routes_hide_markers(opened):
    runtime, client, dataset_hash = opened
    poison = {
        "schema_version": 1,
        "engine_version": ENGINE_VERSION,
        "feature_version": FEATURE_VERSION,
        "return_basis": "price-only",
        "decision_origin": "mock",
        "metrics": {"total_return": 0.01, "final_equity": 100, "llm_calls": 0, "trade_count": 0},
        "dataset": {"id": "synthetic-spy-v1", "kind": "synthetic", "file": "C:\\Users\\fixture\\secret\\bars.csv", "sha256": "ab" * 32},
        "decisions": [{
            "decision_id": str(uuid4()),
            "decision_session": "2023-01-06",
            "execution_session": "2023-01-09",
            "status": "APPROVED",
            "reason_codes": ["C:\\Users\\fixture\\secret\\why.txt", "ops@example.com"],
            "requested_weight": 1,
            "approved_weight": 1,
            "calls": [{
                "call_id": "call-1",
                "stage": "decision",
                "status": "VALIDATED",
                "mode": "api",
                "model_id": "synthetic-model-v1",
                "raw_response": "prefix sk-syntheticfixturesecret bearer synthetic-token-marker",
                "raw_provider_response": {"secret": "synthetic-fixture-token", "endpoint": "https://kuaiapi.net/v1/responses", "nested": {"group": "fixture-group-marker"}},
                "provider_attribution": {"provider": "kuaiapi", "group": "fixture-group-marker", "endpoint": "https://kuaiapi.net/v1/responses"},
                "input": {"features": {"last_close": 10}, "notes": "account-secret-marker", "extras": {"token": "synthetic-fixture-token"}},
                "validated_output": {"rationale": "fixture-success-key stays server side"},
            }],
            "analysis_output": {"regime": "bullish", "thesis": "fixture-binding-ref", "evidence": ["ma20"]},
        }],
        "trades": [],
        "equity": [{"session_date": "2023-01-09", "equity": 100, "cash": 100, "shares": 0, "close": 10, "drawdown": 0}],
        "provider_attribution": {"provider": "kuaiapi", "group": "fixture-group-marker", "endpoint": "https://api.openai.com/v1/responses"},
        "extras": {"machine_path": "C:\\Users\\fixture\\secret\\db.sqlite3", "api_key": "sk-syntheticfixturesecret"},
        "environment": {"python": "C:\\Users\\fixture\\secret\\python.exe", "packages": {"httpx": "0.28.1"}},
    }
    config = ExperimentConfig(strategy_id="buy_hold", llm_mode="mock", dataset_id="synthetic-spy-v1", start_date="2023-01-01", end_date="2023-01-31")
    first, _ = runtime.store.create(config, "poison-a")
    second, _ = runtime.store.create(config, "poison-b")
    runtime.store.update(first, status="RUNNING")
    runtime.store.update(second, status="RUNNING")
    runtime.store.record_event(first, "agent_call", poison["decisions"][0]["calls"][0])
    with runtime.store.connect() as conn:
        conn.execute(
            "INSERT INTO events(run_id,event_type,event_id,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (str(first), "private_debug", "private-debug-1",
             json.dumps({"blob": "synthetic-fixture-token", "path": "C:\\Users\\fixture\\secret\\debug"}),
             "2023-01-01T00:00:00+00:00", "2023-01-01T00:00:00+00:00"),
        )
    runtime.store.update(first, status="COMPLETED", progress=100, result=poison)
    runtime.store.update(second, status="COMPLETED", progress=100, result=poison)
    failed, _ = runtime.store.create(config.model_copy(update={"fee_bps": 6.0}), "poison-failed")
    runtime.store.update(failed, status="RUNNING")
    runtime.store.update(failed, status="FAILED", error="C:\\Users\\fixture\\secret\\trace.log sk-syntheticfixturesecret")
    paths = [
        "/api/v1/experiments",
        f"/api/v1/experiments/{first}",
        f"/api/v1/experiments/{first}/decisions",
        f"/api/v1/experiments/{first}/trades",
        f"/api/v1/experiments/{first}/equity",
        f"/api/v1/experiments/{first}/metrics",
        f"/api/v1/experiments/{first}/events",
        f"/api/v1/experiments/{first}/export",
        f"/api/v1/experiments/{failed}",
        f"/api/v1/experiments/{failed}/export",
        "/api/v1/datasets",
        "/api/v1/market/bars?dataset_id=synthetic-spy-v1&start_date=2023-01-01&end_date=2023-01-31",
        "/api/v1/system/capabilities",
        "/api/v1/research/readiness",
        "/api/v1/research/studies",
    ]
    captured = []
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path + " " + response.text
        captured.append(response.text)
        assert_clean(response.text)
    compared = client.post("/api/v1/experiments/compare", json={"run_ids": [first, second]})
    assert compared.status_code == 200, compared.text
    captured.append(compared.text)
    assert_clean(compared.text)
    unknown = client.get("/api/v1/experiments/" + str(uuid4()))
    assert unknown.status_code == 404 and unknown.json()["detail"] == "Unknown experiment"
    echoed = client.get("/api/v1/experiments/sk-syntheticfixturesecret")
    assert echoed.status_code == 422 and echoed.json()["detail"] == "REQUEST_BODY_REJECTED"
    assert_clean(echoed.text)
    missing = client.get("/api/v1/research/studies/sk-syntheticfixturesecret")
    assert missing.status_code == 404 and "sk-" not in missing.text
    preview_body = request_for("fixture-success", dataset_hash)
    leaked = client.post("/api/v1/research/preview", json={**preview_body, "prices": {"input": "fixture-success-key"}})
    assert leaked.status_code == 422
    assert_clean(leaked.text)
    joined = "\n".join(captured)
    assert "bullish" in joined
    assert "kuaiapi" in joined
    for secret in runtime.secret_values():
        assert secret not in joined


def test_configured_loopback_proxy_shape_and_rebinding(tmp_path):
    app = create_app(tmp_path / "boundary.sqlite3", local_origins=("http://127.0.0.1:4311",))
    dataset_hash = DatasetRegistry([ROOT / "data" / "sample"]).load("synthetic-spy-v1").hash
    body = request_for("fixture-success", dataset_hash)
    with TestClient(app, base_url="http://127.0.0.1:4310") as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/health", headers={"Origin": "null"}).json()["detail"] == "LOCAL_ORIGIN_REJECTED"
        assert client.get("/api/v1/health", headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
        assert client.get("/api/v1/health", headers={"X-Forwarded-Host": "evil.example"}).json()["detail"] == "LOCAL_FORWARDED_REJECTED"
        assert client.get("/api/v1/health", headers={"X-Forwarded-Host": "127.0.0.1:4311, evil.example"}).status_code == 403
        assert client.get("/api/v1/health", headers={"Forwarded": "host=127.0.0.1:4310"}).json()["detail"] == "LOCAL_FORWARDED_REJECTED"
        assert client.get("/api/v1/health", headers={"X-Forwarded-Proto": "gopher"}).status_code == 403
        assert client.get("/api/v1/health", headers={"X-Forwarded-For": "203.0.113.5", "X-Forwarded-Host": "127.0.0.1:4311"}).status_code == 200
        proxied = client.post("/api/v1/research/preview", json=body, headers={
            "Origin": "http://127.0.0.1:4311",
            "X-Forwarded-Host": "127.0.0.1:4311",
            "Sec-Fetch-Site": "same-origin",
        })
        assert proxied.status_code == 409 and proxied.json()["detail"] == "SERVICE_DISABLED"
        form = client.post("/api/v1/research/preview", content=b"profile_id=fixture-success", headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:4311"})
        assert form.status_code == 403 and form.json()["detail"] == "LOCAL_CONTENT_TYPE_REJECTED"
        cross = client.post("/api/v1/research/preview", json=body, headers={"Origin": "http://127.0.0.1:4311", "Sec-Fetch-Site": "cross-site"})
        assert cross.status_code == 403 and cross.json()["detail"] == "LOCAL_FETCH_REJECTED"
        opaque = client.post("/api/v1/research/preview", json=body, headers={"Origin": "http://127.0.0.1:4311", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Site": "same-origin"})
        assert opaque.status_code == 403 and opaque.json()["detail"] == "LOCAL_FETCH_REJECTED"
    with TestClient(app, base_url="http://localhost:4310") as client:
        rejected = client.get("/api/v1/health")
        assert rejected.status_code == 403 and rejected.json()["detail"] == "LOCAL_HOST_REJECTED"
        assert "localhost" not in rejected.text
