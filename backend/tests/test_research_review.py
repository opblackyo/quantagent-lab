"""Independent P2-03 regression cases; synthetic inputs and no application IO."""
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.agents import InjectedFixtureTransport, classify_stage_envelope
from app.research_service import execute_study, replay_study, resume_study
from test_research_execution import (
    TOKEN, OUTPUTS, billing_for, fixture_client, fixture_usage, no_live_io, open_study, run,
)
from app.pilot_billing import reconcile_study_usage
from test_research_planner import setup_plan


def test_estimate_without_receipt_keeps_hold_and_cannot_fill(tmp_path, setup_plan):
    store, study, inputs, at, plan = open_study(tmp_path, setup_plan, budget="0.0178")
    client, seen = fixture_client()
    try:
        result = execute_study(store, study, inputs, at=at, service_enabled=True, client=client,
                               authorization=TOKEN, billing=billing_for(inputs, at))
        assert result["status"] == "PAUSED_BILLING" and seen["n"] == 1
        assert result["report"]["cost"]["unknown_reserved_usd"] == pytest.approx(float(plan.payload["estimate"]["reservation_usd_per_attempt"]))
        single = store.get(result["report"]["source_runs"]["single_agent"]["run_id"], True)["result"]
        assert single["trades"] == []
    finally:
        client.close(); store.close()


@pytest.mark.parametrize("strategy", ["buy_hold", "ma_crossover"])
def test_baseline_cancel_before_fill_retains_cash(tmp_path, setup_plan, strategy):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    original = store.record_event
    def cancel(run_id, kind, payload):
        original(run_id, kind, payload)
        if kind == "decision" and payload.get("status") == "APPROVED" and store.get(run_id)["config"]["strategy_id"] == strategy:
            store.request_cancel(study, at=at)
    store.record_event = cancel
    client, seen = fixture_client()
    try:
        result = run(store, study, inputs, at, client)
        source = store.get(result["report"]["source_runs"][strategy]["run_id"], True)["result"]
        assert result["status"] == "CANCELLED" and seen["n"] == 0
        assert source["trades"] == [] and source["equity"][-1]["shares"] == 0
        assert source["decisions"][0]["status"] == "SKIPPED"
        assert replay_study(store, study, inputs, at=at)["replays"][strategy]["equal"]
    finally:
        client.close(); store.close()


def test_nested_cached_usage_outside_input_is_rejected():
    raw = json.dumps(OUTPUTS["decision_output"])
    envelope = {"model": "synthetic-model-v1", "status": "completed", "output": [
        {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": raw}]}],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                  "input_tokens_details": {"cached_tokens": 999999}}}
    result = classify_stage_envelope("decision", envelope, raw, {"model_id": "synthetic-model-v1",
        "provider_id": "kuaiapi", "http_status": 200, "max_output_tokens": 1024})
    assert not result["eligible_for_fill"] and not result["release_reservation"]


def test_null_output_is_persisted_as_failure(tmp_path, setup_plan):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "status": "completed", "model": "synthetic-model-v1", "output": None})))
    try:
        result = run(store, study, inputs, at, client)
        assert result["status"] == "PAUSED_BILLING"
        assert any(slot["state"] == "RECORDED" for slot in store.list_slots(study))
    finally:
        client.close(); store.close()


def test_fixture_transport_refuses_mounts_and_real_subclasses(setup_plan):
    class DisguisedHTTPTransport(httpx.HTTPTransport):
        pass
    profile = setup_plan[1]
    for client in (httpx.Client(transport=DisguisedHTTPTransport()),
                   httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                                mounts={"https://": httpx.HTTPTransport()})):
        try:
            with pytest.raises(ValueError, match="fixture"):
                InjectedFixtureTransport(client, provider_id=profile.provider, endpoint="https://kuaiapi.net/v1/responses",
                    model_id=profile.model_id, settings=profile.settings.provider_settings(),
                    timeout_policy=profile.timeout_policy, authorization=TOKEN)
        finally:
            client.close()


@pytest.mark.parametrize('change', ['expiry', 'external-usage', 'day-rollover'])
def test_execution_refreshes_clock_and_billing_before_fill(tmp_path, setup_plan, change):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    current = {'at': at, 'external': 0}
    def after_post(request, seen):
        if change == 'expiry':
            current['at'] = at + timedelta(days=2)
        elif change == 'day-rollover':
            current['at'] = at + timedelta(days=1)
        else:
            current['external'] = 1
    client, seen = fixture_client(on_post=after_post)
    def billing(t):
        return billing_for(inputs, t, external_same_key_requests=current['external'])
    try:
        result = execute_study(store, study, inputs, at=at, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing, clock=lambda: current['at'], usage_evidence=fixture_usage)
        assert result['status'] in ('PAUSED_DRIFT', 'PAUSED_BILLING') and seen['n'] == 1
        source = store.get(result['report']['source_runs']['single_agent']['run_id'], True)['result']
        assert source['trades'] == []
    finally:
        client.close(); store.close()


def test_replay_is_bound_to_each_source_revision(tmp_path, setup_plan):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    client, seen = fixture_client()
    try:
        paused = execute_study(store, study, inputs, at=at, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing_for(inputs, at))
        assert paused['status'] == 'PAUSED_BILLING' and seen['n'] == 1
        first = replay_study(store, study, inputs, at=at)['replays']['single_agent']
        assert first['equal']
        old = store.get(first['run_id'], True)['result']
        slot = next(s for s in store.list_slots(study) if s['state'] == 'RECORDED')
        reconcile_study_usage(store, study, fixture_usage(store, study, slot['slot_id']))
        resumed = resume_study(store, study, inputs, at=at, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert resumed['status'] == 'COMPLETED' and seen['n'] == 16
        second = replay_study(store, study, inputs, at=at)['replays']['single_agent']
        assert second['equal'] and first['run_id'] != second['run_id']
        assert store.get(first['run_id'], True)['result'] == old
    finally:
        client.close(); store.close()


def test_mismatched_charge_preserves_full_hold(tmp_path, setup_plan):
    store, study, inputs, at, plan = open_study(tmp_path, setup_plan)
    client, seen = fixture_client()
    def wrong(store, study, slot):
        return {**fixture_usage(store, study, slot), 'actual_charge_usd': '0'}
    try:
        result = execute_study(store, study, inputs, at=at, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=wrong)
        assert result['status'] == 'PAUSED_BILLING' and seen['n'] == 1
        assert result['report']['cost']['unknown_reserved_usd'] == pytest.approx(float(plan.payload['estimate']['reservation_usd_per_attempt']))
        assert result['report']['cost']['confirmed_actual_usd'] == 0
    finally:
        client.close(); store.close()


def test_timeout_during_cancel_is_terminal_and_keeps_hold(tmp_path, setup_plan):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    client, seen = fixture_client('timeout', on_post=lambda *_: store.request_cancel(study, at=at))
    try:
        result = run(store, study, inputs, at, client)
        assert result['status'] == 'CANCELLED' and seen['n'] == 1
        assert result['report']['cost']['unknown_reserved_usd'] > 0
    finally:
        client.close(); store.close()


def test_crash_after_committed_fill_does_not_duplicate_trade_events(tmp_path, setup_plan):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    client, seen = fixture_client()
    fired = {'done': False}
    def crash(name):
        if name == 'AFTER_FILL' and not fired['done'] and store.execution_report(study)['coverage']['ma_crossover'] == 'COMPLETED':
            fired['done'] = True
            raise RuntimeError('synthetic after-fill crash')
    try:
        with pytest.raises(RuntimeError, match='after-fill'):
            run(store, study, inputs, at, client, fault=crash)
        with store._read() as conn:
            before = [dict(r) for r in conn.execute("SELECT event_id,payload_json FROM events WHERE event_type='trade'")]
        store.recover_interrupted(at=at)
        result = resume_study(store, study, inputs, at=at, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert result['status'] == 'COMPLETED' and seen['n'] == 16
        with store._read() as conn:
            after = {r['event_id']: r['payload_json'] for r in conn.execute("SELECT event_id,payload_json FROM events WHERE event_type='trade'")}
        assert all(after[r['event_id']] == r['payload_json'] for r in before)
        single = store.get(result['report']['source_runs']['single_agent']['run_id'], True)['result']
        assert len(single['trades']) == 1
        assert replay_study(store, study, inputs, at=at)['replays']['single_agent']['equal']
    finally:
        client.close(); store.close()


def test_partial_replay_preserves_target_already_met(tmp_path, setup_plan):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    def stop(request, seen):
        if seen['n'] == 3:
            raise httpx.ReadTimeout('synthetic third-call timeout')
    client, seen = fixture_client(on_post=stop)
    try:
        result = run(store, study, inputs, at, client)
        assert result['status'] == 'PAUSED_BILLING' and seen['n'] == 3
        assert replay_study(store, study, inputs, at=at)['replays']['single_agent']['equal']
    finally:
        client.close(); store.close()


@pytest.mark.parametrize('location', ['output', 'content', 'tool'])
def test_explicit_invalid_envelope_members_fail_closed(location):
    raw = json.dumps(OUTPUTS['decision_output'])
    message = {'type': 'message', 'status': 'completed', 'content': [{'type': 'output_text', 'text': raw}]}
    envelope = {'model': 'synthetic-model-v1', 'status': 'completed', 'output': [message],
                'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}
    if location == 'output':
        envelope['output'].append(42)
    elif location == 'content':
        message['content'].append(42)
    else:
        message['content'].append({'type': 'tool_call'})
    result = classify_stage_envelope('decision', envelope, raw, {'model_id': 'synthetic-model-v1',
        'provider_id': 'kuaiapi', 'http_status': 200, 'max_output_tokens': 1024})
    assert not result['eligible_for_fill'] and not result['release_reservation'] and result['pause_study']


@pytest.mark.parametrize('point', ['AFTER_SETTLEMENT', 'AFTER_FILL', 'BEFORE_FINISH'])
def test_final_deterministic_tail_can_complete_without_resending(tmp_path, setup_plan, point):
    store, study, inputs, at, _ = open_study(tmp_path, setup_plan)
    client, seen = fixture_client()
    def crash(name):
        if name == point and seen['n'] == 16:
            raise RuntimeError('synthetic final-tail crash')
    finish = store.finish
    if point == 'BEFORE_FINISH':
        def interrupted_finish(*args, **kwargs):
            crash('BEFORE_FINISH')
            return finish(*args, **kwargs)
        store.finish = interrupted_finish
    try:
        with pytest.raises(RuntimeError, match='final-tail'):
            run(store, study, inputs, at, client, fault=crash)
        store.finish = finish
        store.recover_interrupted(at=at)
        eligibility = store.resume_eligibility(study, inputs, at=at)
        assert eligibility['can_queue'] and eligibility['deterministic_completion_only']
        result = resume_study(store, study, inputs, at=at, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing_for(inputs, at), usage_evidence=fixture_usage)
        assert result['status'] == 'COMPLETED' and result['posts'] == 0 and seen['n'] == 16
        assert all(r['equal'] for r in replay_study(store, study, inputs, at=at)['replays'].values())
    finally:
        client.close(); store.close()


def test_canonical_cache_alias_does_not_release_underbilled_hold(tmp_path, setup_plan):
    request, profile, registry, context = setup_plan
    profile = profile.model_copy(update={'cache_read_usd_per_million': Decimal('0.4')})
    store, study, inputs, at, _ = open_study(tmp_path, (request, profile, registry, context))
    def cached(request):
        raw = json.dumps(OUTPUTS['decision_output'])
        return httpx.Response(200, json={'id': 'synthetic-cache', 'model': profile.model_id, 'status': 'completed',
            'output': [{'type': 'message', 'status': 'completed', 'content': [{'type': 'output_text', 'text': raw}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                      'input_tokens_details': {'cache_read_tokens': 100, 'cache_write_tokens': 0}}})
    client = httpx.Client(transport=httpx.MockTransport(cached))
    try:
        result = run(store, study, inputs, at, client)  # fake bill is uncached 0.000036, not 0.000056
        assert result['status'] == 'PAUSED_BILLING' and result['posts'] == 1
        assert result['report']['cost']['unknown_reserved_usd'] > 0
        assert result['report']['cost']['confirmed_actual_usd'] == 0
    finally:
        client.close(); store.close()


def test_resume_cannot_reset_persisted_billing_day(tmp_path, setup_plan):
    request, profile, registry, context = setup_plan
    profile = profile.model_copy(update={'expires_at': profile.expires_at + timedelta(days=2)})
    store, study, inputs, at, _ = open_study(tmp_path, (request, profile, registry, context))
    client, seen = fixture_client()
    try:
        execute_study(store, study, inputs, at=at, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing_for(inputs, at))
        slot = next(s for s in store.list_slots(study) if s['state'] == 'RECORDED')
        reconcile_study_usage(store, study, fixture_usage(store, study, slot['slot_id']))
        later = at + timedelta(days=1)
        result = resume_study(store, study, inputs, at=later, service_enabled=True, client=client,
            authorization=TOKEN, billing=billing_for(inputs, later), usage_evidence=fixture_usage)
        assert result['status'] in ('PAUSED_DRIFT', 'PAUSED_BILLING') and seen['n'] == 1
    finally:
        client.close(); store.close()
