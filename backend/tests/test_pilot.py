"""Offline pilot contracts; synthetic data and HTTP fixtures only, never paid calls."""
import json
from dataclasses import replace
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from app.agents import AgentWorkflow, MockProvider, OpenAIProvider
from app.data import DatasetRegistry
from app.experiments import run_experiment
from app.features import snapshots
from app.models import ExperimentConfig
from app.pilot import PilotBudget, PilotConfig, PilotProvider, execute_pilot, make_manifest, semantic_result
from app.storage import Store


@pytest.fixture(scope='module')
def fixture_data():
    data = DatasetRegistry().load('synthetic-spy-v1')
    # Exercise the historical-data gate without embedding licensed market data.
    return replace(data, metadata={**data.metadata,'kind':'historical','source':'Synthetic HTTP-test fixture only'})


def configured(data, **updates):
    return PilotConfig(dataset_id=data.metadata['id'],dataset_hash=data.hash,
        model_id='fixture-model-2026-01-01',input_usd_per_million=1,output_usd_per_million=2,
        total_budget_usd=2,price_source='https://developers.openai.com/api/docs/pricing',
        price_verified_on='2026-10-24',**updates)


@pytest.mark.parametrize('legacy_budget',[False,True])
def test_api_status_export_and_legacy_budget_poll_are_pure(tmp_path,fixture_data,monkeypatch,legacy_budget):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app=create_app(tmp_path/'readonly-poll.sqlite3')
    manifest=make_manifest(configured(fixture_data),fixture_data)
    with TestClient(app) as client:
        store=app.state.store
        run,_=store.create(ExperimentConfig(),'synthetic-read-only')
        store.update(run,status='COMPLETED',result={'synthetic':True})
        if legacy_budget:
            with store.connect() as conn:
                conn.execute('CREATE TABLE pilot_budgets (pilot_id TEXT PRIMARY KEY,manifest_json TEXT NOT NULL)')
                conn.execute('CREATE TABLE pilot_attempts (call_id TEXT PRIMARY KEY,pilot_id TEXT,reserved TEXT,usage_estimate TEXT,status TEXT)')
                conn.execute('INSERT INTO pilot_budgets VALUES (?,?)',(manifest['manifest_hash'],json.dumps(manifest)))
                conn.execute('INSERT INTO pilot_attempts VALUES (?,?,?,?,?)',('synthetic-call',manifest['manifest_hash'],'0.02',None,'unknown_reserved'))
        with store.read() as conn:
            before=[tuple(r) for r in conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
        def forbidden(*args,**kwargs):raise AssertionError('read opened a connection or initialized a budget')
        monkeypatch.setattr(store,'connect',forbidden)
        monkeypatch.setattr(PilotBudget,'__init__',forbidden)
        statements=[];store.keeper.set_trace_callback(statements.append)
        for _ in range(2):
            response=client.get('/api/v1/pilots/'+manifest['manifest_hash']+'/budget')
            assert response.status_code==(200 if legacy_budget else 404)
            if legacy_budget:
                assert response.json()['unknown_reserved_usd']==.02
                assert response.json()['invoiced_actual_usd'] is None
            assert client.get('/api/v1/experiments').status_code==200
            assert client.get('/api/v1/experiments/'+run+'/events').json()==[]
            assert client.get('/api/v1/experiments/'+run+'/export').status_code==200
        assert all(s.lstrip().upper().startswith(('SELECT','PRAGMA')) for s in statements)
        with store.read() as conn:
            assert [tuple(r) for r in conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]==before
            if legacy_budget:
                assert 'actual_charge' not in {r[1] for r in conn.execute('PRAGMA table_info(pilot_attempts)')}
        store.keeper.set_trace_callback(None)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path/'pilot.sqlite3')
    value.initialize()
    yield value
    value.close()


def provider(config, monkeypatch, handler=None):
    monkeypatch.setenv('OPENAI_API_KEY','private-http-fixture-only')
    requests = []
    def handle(request):
        body=json.loads(request.content)
        requests.append(body)
        if handler:
            return handler(body,len(requests))
        role=body['text']['format']['name'].removesuffix('_output')
        raw=MockProvider().reply(role,json.loads(body['input']),{}).raw
        return httpx.Response(200,json={'status':'completed','model':config.model_id,
            'output':[{'type':'message','content':[{'type':'output_text','text':raw}]}],
            'usage':{'input_tokens':100,'output_tokens':40}})
    return PilotProvider(config,httpx.Client(transport=httpx.MockTransport(handle))),requests


@pytest.mark.parametrize('updates',[{'model_id':'moving-alias'},{'total_budget_usd':True},
    {'total_budget_usd':float('nan')},{'max_calls':True},{'end_date':'2025-01-01'},
    {'start_date':'2023-01-01','end_date':'2023-06-01'},{'api_key':'do-not-persist'}])
def test_config_rejects_unbounded_or_secret_settings(updates):
    with pytest.raises(ValidationError):
        PilotConfig(**updates)


def test_manifest_calendar_prices_and_no_silent_defaults(fixture_data):
    missing=PilotConfig(dataset_id=fixture_data.metadata['id'],dataset_hash=fixture_data.hash)
    manifest=make_manifest(missing,fixture_data)
    assert manifest['configuration']['total_budget_usd'] is None
    assert manifest['estimate']['reservation_usd_per_attempt'] is None
    assert [(s['decision'],s['execution']) for s in manifest['sessions']]==[
        ('2023-01-06','2023-01-09'),('2023-01-13','2023-01-17'),
        ('2023-01-20','2023-01-23'),('2023-01-27','2023-01-30')]
    assert all(s['warmup_bars']>=60 for s in manifest['sessions'])
    estimate=make_manifest(configured(fixture_data),fixture_data)['estimate']
    assert estimate['total_nominal_calls']==16 and estimate['retry_attempt_ceiling']==32
    assert estimate['reservation_usd_per_attempt']==pytest.approx((32768+2048*2)/1e6)
    with pytest.raises(ValueError,match='historical dataset'):
        make_manifest(missing,replace(fixture_data,metadata={**fixture_data.metadata,'kind':'synthetic'}))
    with pytest.raises(ValueError,match='exact hash'):
        make_manifest(missing.model_copy(update={'dataset_hash':'wrong'}),fixture_data)


def test_total_budget_survives_reopen_and_atomic_competition(store,fixture_data):
    from concurrent.futures import ThreadPoolExecutor
    from app.agents import AgentFailure
    cfg=configured(fixture_data).model_copy(update={'total_budget_usd':.1,'max_calls':3})
    manifest=make_manifest(cfg,fixture_data)
    budget=PilotBudget(store,manifest)
    def reserve(i):
        try:
            budget.reserve(str(i),.06)
            return True
        except AgentFailure:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(reserve,range(2)))==1
    restarted=PilotBudget(store,manifest)
    assert restarted.summary()['unknown_reserved_usd']==.06
    assert restarted.summary()['attempts']==1
    # Settling known usage frees only the difference; invoices remain unknown.
    with store.connect() as conn:
        call=conn.execute('SELECT call_id FROM pilot_attempts').fetchone()[0]
    restarted.settle(call,.01)
    restarted.reserve('third',.06)
    assert restarted.summary()['charged_or_reserved_usd']==.07
    assert restarted.summary()['invoiced_actual_usd'] is None


def test_four_runs_replay_and_rerun_are_network_isolated(store,fixture_data,monkeypatch):
    cfg=configured(fixture_data)
    api,requests=provider(cfg,monkeypatch)
    report=execute_pilot(store,cfg,fixture_data,provider=api)
    assert len(requests)==16
    assert report['budget']['attempts']==16
    assert [r['metrics']['llm_calls'] for r in report['runs']]==[0,0,4,12]
    assert all(e['equal'] and e['provider_calls']==0 for e in report['replay_equality'])
    assert report['budget']['usage_estimated_usd']==pytest.approx(16*.00018)
    assert all(r['config']['research_group_id']==report['manifest']['manifest_hash'] for r in report['runs'])
    assert 'private-http-fixture-only' not in json.dumps(report)
    for body in requests:
        assert body['model']==cfg.model_id and body['store'] is False
        assert body['service_tier']=='default'
        assert 'SPY' not in body['input'] and 'decision_session' not in body['input']
    monkeypatch.setattr(api,'reply',lambda *a:pytest.fail('idempotent rerun called API'))
    repeated=execute_pilot(store,cfg,fixture_data,provider=api)
    assert [r['run_id'] for r in repeated['runs']]==[r['run_id'] for r in report['runs']]


def test_timeout_not_resent_unknown_charges_share_budget_and_replay(store,fixture_data,monkeypatch):
    cfg=configured(fixture_data).model_copy(update={'total_budget_usd':.075})
    def unknown(body,n):
        if n==1:
            raise httpx.ReadTimeout('synthetic timeout')
        role=body['text']['format']['name'].removesuffix('_output')
        raw=MockProvider().reply(role,json.loads(body['input']),{}).raw
        return httpx.Response(200,json={'model':cfg.model_id,'status':'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':raw}]}]})
    api,requests=provider(cfg,monkeypatch,unknown)
    report=execute_pilot(store,cfg,fixture_data,provider=api)
    assert len(requests)==2 # Different weekly decisions; timeout is not resent.
    assert report['budget']['attempts']==2
    assert report['budget']['unknown_reserved_usd']==pytest.approx(2*(32768+4096)/1e6)
    assert report['runs'][3]['metrics']['llm_calls']==0
    assert all(d['reason_codes']==['PILOT_BUDGET_EXHAUSTED'] for d in report['runs'][3]['result']['decisions'])
    single=report['runs'][2]['result']
    assert len(single['decisions'][0]['calls'])==1
    assert single['decisions'][0]['calls'][0]['timeout_diagnostic']['phase']=='read'
    assert len(single['decisions'][1]['calls'])==1
    replay=store.get(report['replay_equality'][2]['replay_run_id'],True)['result']
    assert len(replay['decisions'][0]['calls'])==1
    assert all(c['cost_status']=='replay_zero' for d in replay['decisions'] for c in d['calls'])


@pytest.mark.parametrize('problem',['invalid','model_mismatch','overrun'])
def test_invalid_model_and_overrun_never_trade(store,fixture_data,monkeypatch,problem):
    cfg=configured(fixture_data)
    def response(body,n):
        return httpx.Response(200,json={'model':'wrong-snapshot' if problem=='model_mismatch' else cfg.model_id,
            'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'invalid'}]}],
            'usage':{'input_tokens':10000000 if problem=='overrun' else 10,'output_tokens':1}})
    api,requests=provider(cfg,monkeypatch,response)
    report=execute_pilot(store,cfg,fixture_data,provider=api)
    for row in report['runs'][2:]:
        assert row['result']['trades']==[]
        assert row['metrics']['skipped_decisions']==4
    if problem=='overrun':
        assert len(requests)==1 and report['budget']['charged_or_reserved_usd']>2
    assert all(e['equal'] for e in report['replay_equality'])


def test_interruption_keeps_reservation_and_diagnostics(store,fixture_data,monkeypatch):
    cfg=configured(fixture_data)
    def interrupted(body,n):
        raise KeyboardInterrupt()
    api,requests=provider(cfg,monkeypatch,interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute_pilot(store,cfg,fixture_data,provider=api)
    failed=[r for r in store.list() if r['status']=='FAILED']
    assert len(failed)==1
    calls=[e['payload'] for e in store.events(failed[0]['run_id']) if e['event_type']=='agent_call']
    assert calls[0]['status']=='INTERRUPTED' and calls[0]['reserved_usd']>0
    assert PilotBudget(store,make_manifest(cfg,fixture_data)).summary()['attempts']==1
    with pytest.raises(ValueError,match='prior pilot run'):
        execute_pilot(store,cfg,fixture_data,provider=api)
    assert len(requests)==1


def test_skipped_replay_checks_input_and_rejected_replay_keeps_risk(fixture_data,monkeypatch):
    from app.agents import replay_decision
    config=ExperimentConfig(strategy_id='single_agent',start_date='2023-01-01',end_date='2023-01-31',max_llm_calls=0)
    result=run_experiment(config,fixture_data)
    snap=snapshots(fixture_data)[date(2023,1,6)]
    changed=snap.model_copy(update={'ma20':snap.ma20+1})
    assert replay_decision(changed,result['decisions'][0])['reason_codes']==['REPLAY_INPUT_MISMATCH']
    rejected_config=config.model_copy(update={'max_llm_calls':32,'max_exposure':.5})
    rejected=run_experiment(rejected_config,fixture_data)
    monkeypatch.setattr(OpenAIProvider,'__init__',lambda *a,**k:pytest.fail('Replay constructed live provider'))
    replay=run_experiment(rejected_config.model_copy(update={'llm_mode':'replay'}),fixture_data,source=rejected)
    assert semantic_result(rejected)==semantic_result(replay)


def test_missing_configuration_does_not_construct_provider(store,fixture_data,monkeypatch):
    cfg=PilotConfig(dataset_id=fixture_data.metadata['id'],dataset_hash=fixture_data.hash)
    monkeypatch.setattr(PilotProvider,'__init__',lambda *a,**k:pytest.fail('unconfigured provider constructed'))
    with pytest.raises(ValueError,match='incomplete'):
        execute_pilot(store,cfg,fixture_data)


def test_call_ceiling_shared_across_strategies_and_key_echo_redacted(store,fixture_data,monkeypatch):
    cfg=configured(fixture_data).model_copy(update={'max_calls':5})
    api,requests=provider(cfg,monkeypatch)
    report=execute_pilot(store,cfg,fixture_data,provider=api)
    assert len(requests)==report['budget']['attempts']==5
    assert report['runs'][2]['metrics']['llm_calls']==4
    assert report['runs'][3]['metrics']['llm_calls']==1
    assert report['runs'][3]['result']['trades']==[]
    assert all(e['equal'] for e in report['replay_equality'])
    def echo(body,n):
        return httpx.Response(200,json={'status':'completed','model':cfg.model_id,
            'debug':'private-http-fixture-only','usage':{'input_tokens':1,'output_tokens':1},'output':[]})
    echoed,_=provider(cfg,monkeypatch,echo)
    reply=echoed.reply('decision',{'features':{}},{})
    assert 'private-http-fixture-only' not in json.dumps(reply.envelope)
    assert reply.envelope['debug']=='[REDACTED_API_KEY]'


def test_pilot_budget_api_and_comparison_group_boundary(tmp_path,fixture_data):
    from fastapi.testclient import TestClient
    from app.main import create_app
    cfg=configured(fixture_data)
    manifest=make_manifest(cfg,fixture_data)
    with TestClient(create_app(tmp_path/'api.sqlite3')) as client:
        assert client.get('/api/v1/pilots/unknown/budget').status_code==404
        store=client.app.state.store
        budget=PilotBudget(store,manifest)
        budget.reserve('one',.01)
        value=client.get('/api/v1/pilots/'+manifest['manifest_hash']+'/budget').json()
        assert value['attempts']==1 and value['unknown_reserved_usd']==.01
        rows=[]
        for group in ['pilot-one','pilot-two']:
            config=ExperimentConfig(strategy_id='buy_hold',start_date='2023-01-01',end_date='2023-01-31',research_group_id=group)
            run_id,_=store.create(config)
            store.update(run_id,status='COMPLETED',result=run_experiment(config,fixture_data))
            rows.append(run_id)
        assert client.post('/api/v1/experiments/compare',json={'run_ids':rows}).status_code==422
