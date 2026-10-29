"""Offline live-readiness gates. Synthetic markers and MockTransport only.

These tests count posts, refusals, and hash bytes. They do not copy planner
prices or treat a synthetic document as an invoice.
"""
import os
import socket
import time
import traceback
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.data import canonical_hash
from app.http_policy import HTTPTimeoutPolicy
from app.live_readiness import (
    LiveRefusal, ServerLiveApproval, classify_billing_evidence, send_live_attempt,
)
from app.main import create_app
from app.research_contracts import StudyRequest
from app.research_http import StartBody
from app.research_runtime import build_fixture_runtime
from app.research_service import (
    PROVIDER_ENDPOINTS, ExecutionError, execute_study, plan_study,
)
from app.research_store import PlanningInputs, ResearchStore
from test_research_acceptance import LEGACY_STANDARD_MANIFEST
from test_research_execution import TOKEN, billing_for, fixture_client
from test_research_planner import preview, setup_plan
from test_research_store import auth_for

SECRET = "synthetic-live-secret-marker"
ENDPOINT = "https://kuaiapi.net/v1/responses"
PHASE = HTTPTimeoutPolicy(connect=30, read=30, write=30, pool=30)


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


class Loader:
    def __init__(self, secret=SECRET):
        self.secret = secret
        self.calls = []

    def load(self, profile_id):
        self.calls.append(profile_id)
        return self.secret


class Trickle(httpx.SyncByteStream):
    def __init__(self, count, delay):
        self.count = count
        self.delay = delay
        self.yielded = 0

    def __iter__(self):
        for _ in range(self.count):
            if self.delay:
                time.sleep(self.delay)
            self.yielded += 1
            yield b"x"


class Lost(httpx.SyncByteStream):
    def __iter__(self):
        yield b"a"
        raise httpx.ReadError("synthetic lost response")


def _approval(profile_id="synthetic-profile", manifest="a" * 64):
    return ServerLiveApproval(holder="server", funding_approved=True, manifest_hash=manifest, profile_id=profile_id)


def _send(handler, *, approval=None, endpoint=ENDPOINT, loader=None, deadline_seconds=30, clock=time.monotonic, content=b"{}", client_type=httpx.Client, follow_redirects=True):
    seen = {"n": 0, "urls": []}

    def wrapped(request):
        seen["n"] += 1
        seen["urls"].append(str(request.url))
        seen["authorization"] = request.headers.get("authorization")
        return handler(request)

    client = client_type(transport=httpx.MockTransport(wrapped), follow_redirects=follow_redirects)
    try:
        result = send_live_attempt(
            approval=approval, profile_id="synthetic-profile", manifest_hash="a" * 64, endpoint=endpoint,
            loader=loader or Loader(), client=client, timeout_policy=PHASE, deadline_seconds=deadline_seconds,
            content=content, clock=clock)
    finally:
        client.close()
    return result, seen


def test_legacy_manifest_omits_null_deadline_and_fixture_transport(setup_plan):
    frozen, view = preview(setup_plan)
    assert frozen.manifest_hash == LEGACY_STANDARD_MANIFEST
    assert "overall_deadline_seconds" not in frozen.payload["request"]
    assert "transport_intent" not in frozen.payload["request"]
    assert "overall_deadline" not in frozen.payload["policy"]
    assert "transport_intent" not in frozen.payload["policy"]
    assert view.overall_deadline_seconds is None and view.transport_intent is None
    request = setup_plan[0].model_copy(update={"overall_deadline_seconds": 45})
    changed, changed_view = plan_study(request, setup_plan[1], setup_plan[2], context=setup_plan[3])
    assert changed.manifest_hash != frozen.manifest_hash
    assert changed.payload["policy"]["overall_deadline"]["seconds"] == 45
    assert changed.payload["policy"]["overall_deadline"]["version"] == "overall-deadline-v2"
    assert "DNS before connect is not covered" in changed.payload["policy"]["overall_deadline"]["semantics"]
    assert "OVERALL_DEADLINE_DOES_NOT_STOP_UPSTREAM_BILLING" in changed_view.warnings
    live = setup_plan[0].model_copy(update={"transport_intent": "live", "overall_deadline_seconds": 45})
    live_plan, live_view = plan_study(live, setup_plan[1], setup_plan[2], context=setup_plan[3])
    assert live_plan.payload["request"]["transport_intent"] == "live"
    assert live_plan.payload["policy"]["live_start_allowed"] is False
    assert live_view.transport_intent == "live" and "LIVE_APPROVAL_REQUIRED" in live_view.warnings


def test_request_models_reject_browser_approval_and_secrets(setup_plan):
    raw = setup_plan[0].model_dump(mode="json")
    with pytest.raises(ValidationError):
        StudyRequest.model_validate({**raw, "funding_approved": True})
    with pytest.raises(ValidationError):
        StudyRequest.model_validate({**raw, "api_key": SECRET})
    with pytest.raises(ValidationError):
        StudyRequest.model_validate({**raw, "overall_deadline_seconds": 0})
    with pytest.raises(ValidationError):
        StudyRequest.model_validate({**raw, "overall_deadline_seconds": 301})
    with pytest.raises(ValidationError):
        StudyRequest.model_validate({**raw, "transport_intent": "bypass"})
    with pytest.raises(ValidationError):
        ServerLiveApproval(holder="browser", funding_approved=True, manifest_hash="a" * 64, profile_id="synthetic-profile")
    with pytest.raises(ValidationError):
        ServerLiveApproval(holder="server", funding_approved=False, manifest_hash="a" * 64, profile_id="synthetic-profile")
    with pytest.raises(ValidationError):
        StartBody.model_validate({"manifest_hash": "a" * 64, "idempotency_key": "once", "request": raw, "api_key": SECRET})


def test_missing_approval_and_bad_endpoint_do_not_load_or_send():
    loader = Loader()

    def fail(request):
        raise AssertionError("posted")

    with pytest.raises(LiveRefusal) as missing:
        _send(fail, approval=None, loader=loader)
    assert missing.value.code == "LIVE_APPROVAL_REQUIRED" and loader.calls == []
    other = _approval(profile_id="other-profile")
    with pytest.raises(LiveRefusal) as scope:
        _send(fail, approval=other, loader=loader)
    assert scope.value.code == "LIVE_APPROVAL_SCOPE_MISMATCH" and loader.calls == []
    for endpoint in (
        "http://127.0.0.1/v1/responses",
        "https://user:pass@kuaiapi.net/v1/responses",
        "https://kuaiapi.net/v1/responses?x=1",
        "https://kuaiapi.net/v1/responses#frag",
        "http://kuaiapi.net/v1/responses",
        "https://kuaiapi.net/v1/chat/completions",
        "https://evil.example/v1/responses",
    ):
        with pytest.raises(LiveRefusal) as rejected:
            _send(fail, approval=_approval(), endpoint=endpoint, loader=loader)
        assert rejected.value.code == "LIVE_ENDPOINT_REJECTED"
    assert loader.calls == []


def test_loopback_listener_receives_nothing():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    loader = Loader()
    client = httpx.Client()
    try:
        with pytest.raises(LiveRefusal) as rejected:
            send_live_attempt(
                approval=_approval(), profile_id="synthetic-profile", manifest_hash="a" * 64,
                endpoint=f"http://127.0.0.1:{port}/v1/responses", loader=loader, client=client,
                timeout_policy=PHASE, deadline_seconds=30, content=b"{}")
        assert rejected.value.code == "LIVE_ENDPOINT_REJECTED" and loader.calls == []
        with pytest.raises(socket.timeout):
            sock.accept()
    finally:
        client.close()
        sock.close()


def test_expired_deadline_skips_loader_and_send():
    loader = Loader()
    values = iter((0.0, 1.0))
    result, seen = _send(lambda request: httpx.Response(200, content=b"late"), approval=_approval(), loader=loader, deadline_seconds=0.01, clock=lambda: next(values))
    assert result.sent is False and result.stopped == "OVERALL_DEADLINE" and result.retry_allowed is False
    assert loader.calls == [] and seen["n"] == 0


def test_subclassed_client_is_rejected_before_load():
    loader = Loader()

    class Subclassed(httpx.Client):
        pass

    with pytest.raises(LiveRefusal) as rejected:
        _send(lambda request: httpx.Response(200, content=b"no"), approval=_approval(), loader=loader, client_type=Subclassed)
    assert rejected.value.code == "LIVE_CLIENT_REJECTED" and loader.calls == []


def test_approved_mock_send_redacts_secret_and_does_not_retry():
    loader = Loader()
    result, seen = _send(lambda request: httpx.Response(200, content=f'{{"echo":"{SECRET}"}}'.encode()), approval=_approval(), loader=loader, deadline_seconds=None)
    assert seen["n"] == 1 and seen["authorization"] == "Bearer " + SECRET
    assert result.sent is True and result.retry_allowed is False and result.financial_proof is False
    assert result.status_code == 200 and result.stopped is None
    dumped = result.model_dump_json()
    assert SECRET not in dumped and "[REDACTED_API_KEY]" in result.body_text
    assert loader.calls == ["synthetic-profile"]


def test_slow_trickle_stops_inside_phase_read_timeout():
    stream = Trickle(8, 0.05)
    started = time.monotonic()
    result, seen = _send(lambda request: httpx.Response(200, stream=stream), approval=_approval(), deadline_seconds=0.12)
    elapsed = time.monotonic() - started
    assert seen["n"] == 1 and 1 <= stream.yielded < 8
    assert result.sent is True and result.stopped == "OVERALL_DEADLINE" and result.retry_allowed is False
    assert result.body_text is None and elapsed < 5


def test_clock_jump_between_chunks_discards_the_chunk_and_stops():
    values = iter((0.0, 0.0, 5.0))
    result, seen = _send(lambda request: httpx.Response(200, stream=Trickle(4, 0)), approval=_approval(), deadline_seconds=0.2, clock=lambda: next(values))
    assert seen["n"] == 1 and result.sent is True and result.stopped == "OVERALL_DEADLINE"
    assert result.body_text is None and result.retry_allowed is False


def test_phase_timeout_after_handler_keeps_liability():
    def handler(request):
        raise httpx.ReadTimeout("synthetic inactivity")

    result, seen = _send(handler, approval=_approval(), deadline_seconds=30)
    assert seen["n"] == 1 and result.sent is True and result.stopped == "PHASE_TIMEOUT" and result.retry_allowed is False


def test_connect_error_and_lost_body_stay_uncertain_without_another_post():
    result, seen = _send(lambda request: (_ for _ in ()).throw(httpx.ConnectError("synthetic connect")), approval=_approval())
    assert seen["n"] == 1 and result.sent is True and result.stopped == "TRANSPORT_UNCERTAIN" and result.retry_allowed is False
    lost, lost_seen = _send(lambda request: httpx.Response(200, stream=Lost()), approval=_approval())
    assert lost_seen["n"] == 1 and lost.sent is True and lost.stopped == "TRANSPORT_UNCERTAIN" and lost.retry_allowed is False


def test_redirect_is_not_followed_and_body_limit_stops():
    def redirect(request):
        return httpx.Response(302, headers={"location": "https://evil.example/v1/responses"})

    result, seen = _send(redirect, approval=_approval())
    assert seen["n"] == 1 and seen["urls"] == [ENDPOINT]
    assert result.sent is True and result.status_code == 302 and result.stopped == "REDIRECT_REJECTED"
    assert result.retry_allowed is False
    oversized, once = _send(lambda request: httpx.Response(200, content=b"y" * (1_048_576 + 1)), approval=_approval())
    assert once["n"] == 1 and oversized.stopped == "RESPONSE_BODY_LIMIT" and oversized.sent is True
    exact, exact_seen = _send(lambda request: httpx.Response(200, content=b"y" * 1_048_576), approval=_approval())
    assert exact_seen["n"] == 1 and exact.stopped is None and exact.status_code == 200


def test_blank_credential_is_rejected_after_approval():
    loader = Loader(secret=" secret")
    with pytest.raises(LiveRefusal) as rejected:
        _send(lambda request: httpx.Response(200, content=b"no"), approval=_approval(), loader=loader)
    assert rejected.value.code == "LIVE_CREDENTIAL_REJECTED" and loader.calls == ["synthetic-profile"]


def _operator(profile, **overrides):
    body = {
        "kind": "operator-declared-usage-v1", "production_claim": False, "tariff_binding": "not-a-live-tariff",
        "provider": profile.provider, "model_id": profile.model_id, "endpoint": PROVIDER_ENDPOINTS[profile.provider],
        "pricing_unit": profile.pricing_unit, "group_ref": "synthetic-key", "covered_call_ids": ["slot-1"],
        "delayed": False, "external_same_key_requests": 0, "cache_status": "observed-consistent",
        "input_usd_per_million": str(profile.input_usd_per_million),
        "output_usd_per_million": str(profile.output_usd_per_million),
        "cache_read_usd_per_million": str(profile.cache_read_usd_per_million),
        "cache_write_usd_per_million": str(profile.cache_write_usd_per_million),
        "valid_from": profile.valid_from.isoformat(), "expires_at": profile.expires_at.isoformat(),
        "as_of_date": "2023-02-01", "timezone": "UTC", "day_count": 1,
    }
    body.update(overrides)
    provenance = body.pop("provenance", None)
    if provenance is None:
        provenance = {"method": "operator-sha256-v1", "sha256": canonical_hash(body)}
    return {**body, "provenance": provenance}


def test_billing_documents_stay_synthetic_and_fail_closed(setup_plan):
    profile = setup_plan[1]
    at = datetime(2023, 2, 1, tzinfo=timezone.utc)
    matched = classify_billing_evidence(profile, _operator(profile), at=at, key_ref="synthetic-key")
    assert matched["ok"] is True and matched["financial_proof"] is False and matched["invoiced_actual_usd"] is None
    synthetic = classify_billing_evidence(profile, billing_for(type("Inputs", (), {"profile": profile})(), at), at=at, key_ref="synthetic-key")
    assert synthetic["ok"] is True and synthetic["financial_proof"] is False and synthetic["invoiced_actual_usd"] is None
    produced = classify_billing_evidence(profile, _operator(profile, production_claim=True), at=at, key_ref="synthetic-key")
    assert produced["codes"] == ["BILLING_TARIFF_UNKNOWN"] and produced["financial_proof"] is False
    public = classify_billing_evidence(profile, _operator(profile, tariff_binding="public-6.8"), at=at, key_ref="synthetic-key")
    assert public["codes"] == ["BILLING_TARIFF_UNKNOWN"]
    unsupported = classify_billing_evidence(profile, {"kind": "provider-invoice-v1"}, at=at, key_ref="synthetic-key")
    assert unsupported["codes"] == ["BILLING_EVIDENCE_UNSUPPORTED"] and unsupported["invoiced_actual_usd"] is None
    for name, (overrides, code) in (
        ("delayed", ({"delayed": True}, "BILLING_DELAYED")),
        ("external", ({"external_same_key_requests": 2}, "EXTERNAL_SAME_KEY_USAGE")),
        ("cache", ({"cache_status": "unknown"}, "CACHE_UNPROVEN")),
        ("rate", ({"output_usd_per_million": "999"}, "RATE_DRIFT")),
        ("provenance", ({"provenance": {"method": "operator-sha256-v1", "sha256": "b" * 64}}, "PROVENANCE_MISMATCH")),
        ("group", ({"group_ref": "other-group"}, "GROUP_DRIFT")),
    ):
        result = classify_billing_evidence(profile, _operator(profile, **overrides), at=at, key_ref="synthetic-key")
        assert result["ok"] is False and code in result["codes"] and result["financial_proof"] is False, name
        assert result["invoiced_actual_usd"] is None


def _admit(tmp_path, parts, name, **updates):
    request, profile, registry, context = parts
    request = request.model_copy(update=updates)
    inputs = PlanningInputs(request, profile, registry, context)
    plan, view = plan_study(request, profile, registry, context=context)
    store = ResearchStore.create_new(tmp_path / name)
    study = store.register(plan, inputs, idempotency_key=name)
    store.freeze(study, inputs, at=context.checked_at)
    auth = auth_for(study, plan, inputs, authorization_id=name, key=name + "-key", cohort=name + "-cohort")
    store.record_authorization(auth, at=context.checked_at)
    store.start(study, auth.authorization_id, inputs, at=context.checked_at)
    return store, study, inputs, context.checked_at, plan, view


def test_stage_a_and_stage_b_live_intents_do_not_post(tmp_path, setup_plan):
    window = {"start_date": date(2023, 1, 27), "end_date": date(2023, 1, 31), "transport_intent": "live", "overall_deadline_seconds": 45}
    store_a, study_a, inputs_a, at, plan_a, view_a = _admit(
        tmp_path, setup_plan, "stage-a", study_protocol="acceptance-stage-a-v1", requested_max_attempts=1,
        requested_budget_usd=Decimal("1"), **window)
    store_b, study_b, inputs_b, _at_b, plan_b, view_b = _admit(
        tmp_path, setup_plan, "stage-b", study_protocol="acceptance-stage-b-v1", requested_max_attempts=3,
        requested_budget_usd=Decimal("1"), **window)
    client, seen = fixture_client()
    try:
        assert view_a.strategy_ids == ("single_agent",) and view_a.estimate.total_nominal_calls == 1
        assert view_b.strategy_ids == ("multi_agent",) and view_b.estimate.total_nominal_calls == 3
        assert plan_a.manifest_hash != plan_b.manifest_hash
        assert view_a.estimate.all_attempts_fit_requested_caps is True
        assert view_b.estimate.all_attempts_fit_requested_caps is True
        for _ in range(2):
            with pytest.raises(ExecutionError, match="LIVE_APPROVAL_REQUIRED"):
                execute_study(store_a, study_a, inputs_a, at=at, service_enabled=True, client=client, authorization=TOKEN,
                              billing=billing_for(inputs_a, at), secrets=(TOKEN,), usage_evidence=lambda *args: None)
            with pytest.raises(ExecutionError, match="LIVE_APPROVAL_REQUIRED"):
                execute_study(store_b, study_b, inputs_b, at=at, service_enabled=True, client=client, authorization=TOKEN,
                              billing=billing_for(inputs_b, at), secrets=(TOKEN,), usage_evidence=lambda *args: None)
        assert seen["n"] == 0
    finally:
        client.close()
        store_a.close()
        store_b.close()


def test_http_live_preview_and_start_post_nothing(tmp_path):
    runtime = build_fixture_runtime(tmp_path / "live-http.sqlite3")
    app = create_app(research=runtime, local_origins=("http://127.0.0.1:4311",))
    with TestClient(app) as client:
        dataset_hash = next(item["sha256"] for item in client.get("/api/v1/datasets").json() if item["id"] == "synthetic-spy-v1")
        ordinary = {
            "profile_id": "fixture-success", "dataset_id": "synthetic-spy-v1", "expected_dataset_hash": dataset_hash,
            "start_date": "2023-01-01", "end_date": "2023-01-31", "requested_budget_usd": "1", "requested_max_attempts": 32,
        }
        live = {**ordinary, "transport_intent": "live", "overall_deadline_seconds": 45}
        ready = client.get("/api/v1/research/readiness")
        assert ready.status_code == 200 and ready.json()["live_provider"] is False and ready.json()["real_spending_authorized"] is False
        preview_live = client.post("/api/v1/research/preview", json=live)
        assert preview_live.status_code == 200, preview_live.text
        body = preview_live.json()
        assert body["execution_authorized"] is False and body["real_spending_authorized"] is False
        assert "LIVE_APPROVAL_REQUIRED" in body["plan"]["warnings"]
        assert "OVERALL_DEADLINE_DOES_NOT_STOP_UPSTREAM_BILLING" in body["plan"]["warnings"]
        assert body["plan"]["transport_intent"] == "live" and body["plan"]["overall_deadline_seconds"] == 45
        assert runtime.total_posts() == 0
        assert SECRET not in preview_live.text and "kuaiapi.net" not in preview_live.text
        started = client.post("/api/v1/research/studies", json={"manifest_hash": body["plan"]["manifest_hash"], "idempotency_key": "live-once", "request": live})
        assert started.status_code == 409 and started.json()["detail"] == "LIVE_APPROVAL_REQUIRED"
        assert runtime.total_posts() == 0
        rejected = client.post("/api/v1/research/preview", json={**live, "funding_approved": True, "api_key": SECRET})
        assert rejected.status_code == 422 and rejected.json()["detail"] == "REQUEST_BODY_REJECTED"
        assert SECRET not in rejected.text
        ordinary_preview = client.post("/api/v1/research/preview", json=ordinary)
        assert ordinary_preview.status_code == 200
        assert ordinary_preview.json()["plan"]["manifest_hash"] != body["plan"]["manifest_hash"]
        assert ordinary_preview.json()["plan"]["transport_intent"] is None
        assert runtime.total_posts() == 0
