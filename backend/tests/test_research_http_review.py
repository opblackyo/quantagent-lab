"""Independent P2-04 regressions: temporary DBs and synthetic HTTP only."""
import json
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ExperimentConfig
from app.research_contracts import StudyRequest
from app.research_service import plan_study
from test_research_http import opened, request_for, start, wait_for


def idle(runtime):
    deadline = time.monotonic()+15
    while runtime._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not runtime._inflight


def test_known_private_error_is_hidden_on_all_legacy_routes(opened):
    runtime, client, _ = opened
    secret = runtime.profiles['success'].private_evidence[1].reference_id
    run, _ = runtime.store.create(ExperimentConfig(strategy_id='buy_hold'), 'private-error')
    runtime.store.update(run, status='FAILED', error=secret)
    for path in ('/api/v1/experiments', f'/api/v1/experiments/{run}', f'/api/v1/experiments/{run}/export'):
        response = client.get(path)
        assert response.status_code == 200
        assert secret not in response.text
    assert runtime.store.get(run)['error_message'] == secret


def test_unvalidated_paid_raw_json_is_never_a_public_field(tmp_path):
    app = create_app(tmp_path/'legacy-private.sqlite3')
    raw = json.dumps({'account_id': '739281', 'api_key': 'opaque-fixture-value'})
    with TestClient(app) as client:
        store = app.state.store
        run, _ = store.create(ExperimentConfig(strategy_id='single_agent', llm_mode='api',
            model_provider='kuaiapi', model_id='synthetic-model-v1'), 'legacy-paid')
        store.update(run, status='RUNNING')
        call = {'call_id': str(uuid4()), 'mode': 'api', 'stage': 'decision', 'status': 'FAILED',
                'raw_response': raw, 'validated_output': None}
        decision = {'decision_id': str(uuid4()), 'status': 'SKIPPED', 'calls': [call], 'decision_output': None}
        store.record_event(run, 'agent_call', call)
        store.update(run, status='COMPLETED', result={'decisions': [decision], 'trades': [], 'equity': [], 'metrics': {}})
        for suffix in ('decisions', 'events', 'export'):
            response = client.get(f'/api/v1/experiments/{run}/{suffix}')
            assert response.status_code == 200
            assert 'opaque-fixture-value' not in response.text and '739281' not in response.text
        assert store.get(run, True)['result']['decisions'][0]['calls'][0]['raw_response'] == raw


def test_paused_cancel_finishes_without_releasing_unknown_hold(opened):
    runtime, client, dataset_hash = opened
    _, created = start(client, request_for('fixture-delayed', dataset_hash), 'paused-cancel')
    study = created.json()['study_id']
    paused = wait_for(client, study, {'PAUSED_BILLING'})
    idle(runtime)
    result = client.post(f'/api/v1/research/studies/{study}/cancel', json={})
    assert result.status_code == 200 and result.json()['state'] == 'CANCELLED'
    assert result.json()['cost']['unknown_reserved_usd'] == paused['cost']['unknown_reserved_usd'] > 0
    assert runtime.posts_for(study) == 1


def test_resume_during_worker_teardown_is_explicitly_busy(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    import app.research_runtime as module
    original = module.execute_study
    done, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        if result['status'] == 'PAUSED_BILLING':
            done.set()
            assert release.wait(20)
        return result
    monkeypatch.setattr(module, 'execute_study', delayed)
    try:
        _, created = start(client, request_for('fixture-delayed', dataset_hash), 'resume-teardown')
        study = created.json()['study_id']
        assert done.wait(30)
        assert client.post(f'/api/v1/research/studies/{study}/reconcile', json={}).status_code == 200
        busy = client.post(f'/api/v1/research/studies/{study}/resume', json={})
        assert busy.status_code == 409 and busy.json()['detail'] == 'WORKER_BUSY'
        release.set()
        idle(runtime)
        accepted = client.post(f'/api/v1/research/studies/{study}/resume', json={})
        assert accepted.status_code == 202
        assert wait_for(client, study, {'COMPLETED', 'PAUSED_BILLING'})['state'] == 'COMPLETED'
        assert runtime.posts_for(study) == 16
    finally:
        release.set()


def test_all_shared_executor_submissions_are_bounded_and_idempotent(opened):
    runtime, client, dataset_hash = opened
    release = threading.Event()
    blocker = runtime.executor.submit(release.wait)
    accepted, rejected = [], []
    body = {'strategy_id': 'buy_hold', 'dataset_id': 'synthetic-spy-v1',
            'start_date': '2023-01-01', 'end_date': '2023-01-31', 'llm_mode': 'mock'}
    try:
        for i in range(12):
            response = client.post('/api/v1/experiments', json=body, headers={'X-Idempotency-Key': f'bounded-mock-{i}'})
            (accepted if response.status_code == 202 else rejected).append(response)
        assert accepted and rejected
        assert all(r.status_code == 409 and r.json()['detail'] == 'WORK_QUEUE_FULL' for r in rejected)
        duplicate = client.post('/api/v1/experiments', json=body, headers={'X-Idempotency-Key': 'bounded-mock-0'})
        assert duplicate.status_code == 202 and duplicate.json()['run_id'] == accepted[0].json()['run_id']
        assert len(runtime.store.list()) == len(accepted)  # no orphan PENDING rows on refusal
        _, queued = start(client, request_for('fixture-success', dataset_hash), 'full-shared-queue')
        assert queued.status_code == 409 and queued.json()['detail'] == 'WORK_QUEUE_FULL'
        assert runtime.store.list_studies() == []
    finally:
        release.set()
        blocker.result(timeout=5)


def test_drift_has_no_intermediate_billing_pause(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    transitions = []
    original = runtime.store._state
    def observed(conn, row, target, at, reason=None):
        transitions.append(target)
        return original(conn, row, target, at, reason)
    monkeypatch.setattr(runtime.store, '_state', observed)
    _, created = start(client, request_for('fixture-drift', dataset_hash), 'atomic-drift')
    study = created.json()['study_id']
    result = wait_for(client, study, {'PAUSED_DRIFT', 'PAUSED_BILLING'})
    assert result['state'] == 'PAUSED_DRIFT'
    assert 'PAUSED_BILLING' not in transitions
    assert result['cost']['unknown_reserved_usd'] > 0 and runtime.posts_for(study) == 1


def test_arbitrary_private_failure_text_is_not_an_http_error(tmp_path):
    app = create_app(tmp_path/'opaque-error.sqlite3')
    private = 'A synthetic private billing account belongs to Example Person'
    with TestClient(app) as client:
        run, _ = app.state.store.create(ExperimentConfig(), 'opaque-error')
        app.state.store.update(run, status='FAILED', error=private)
        assert client.get(f'/api/v1/experiments/{run}').json()['error_message'] == 'REQUEST_REJECTED'
        assert app.state.store.get(run)['error_message'] == private


def test_failed_admission_does_not_become_accepted_on_retry(opened):
    runtime, client, dataset_hash = opened
    body = request_for('fixture-success', dataset_hash)
    inputs, profile = runtime._inputs(StudyRequest.model_validate(body))
    frozen, _ = plan_study(inputs.request, profile, runtime.registry, context=runtime.context)
    runtime._admit(inputs, profile, frozen, 'reserved-original')
    payload = {'manifest_hash': frozen.manifest_hash, 'idempotency_key': 'rejected-repeat', 'request': body}
    first = client.post('/api/v1/research/studies', json=payload)
    repeated = client.post('/api/v1/research/studies', json=payload)
    assert first.status_code == repeated.status_code == 409
    assert first.json()['detail'] == repeated.json()['detail'] == 'APPROVAL_OR_COHORT_ALREADY_ADMITTED'
    assert runtime.total_posts() == 0 and not runtime._inflight


def test_concurrent_replay_shares_a_bounded_worker_and_result(opened, monkeypatch):
    runtime, client, dataset_hash = opened
    _, created = start(client, request_for('fixture-success', dataset_hash), 'parallel-replay')
    study = created.json()['study_id']
    wait_for(client, study, {'COMPLETED'})
    original = runtime.store.create
    ready, release = threading.Event(), threading.Event()
    def held(config, key=None):
        result = original(config, key)
        if config.llm_mode == 'replay' and result[1] and not ready.is_set():
            ready.set()
            assert release.wait(20)
        return result
    monkeypatch.setattr(runtime.store, 'create', held)
    responses, errors = [], []
    def send():
        try:
            responses.append(client.post(f'/api/v1/research/studies/{study}/replay', json={}))
        except Exception as exc:
            errors.append(type(exc).__name__)
    first, second = threading.Thread(target=send), threading.Thread(target=send)
    first.start()
    try:
        assert ready.wait(30)
        second.start()
        second.join(timeout=0.2)
    finally:
        release.set()
        first.join(timeout=30)
        if second.ident is not None:
            second.join(timeout=30)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and len(responses) == 2
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()['replay_runs'] == responses[1].json()['replay_runs']
    assert runtime.posts_for(study) == 16
