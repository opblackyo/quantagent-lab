"""Synthetic-only ablation execution/unknown billing/resume fixtures. No paid API."""
import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.agents import AgentFailure,OpenAIProvider
from app.data import canonical_hash
from app.gateway import KuaiAPIProvider
from app.pilot import PilotBudget
from app.storage import Store
from test_decision_difference import sources
from test_gateway import data,store,fixture
from test_pilot_billing import runtime_config

spec=importlib.util.spec_from_file_location('ablation',Path(__file__).resolve().parents[2]/'tools/run_critic_ablation.py')
ablation=importlib.util.module_from_spec(spec);spec.loader.exec_module(ablation)


def reading(cfg,n=0):
    row={'requests':16+n,'input_tokens':1600+100*n,'output_tokens':320+20*n,'total_tokens':1920+120*n,
         'cache_creation_tokens':0,'cache_read_tokens':0,'actual_cost':.001+.000054*n,'cost':(.001+.000054*n)/.135}
    return {'day':'2026-10-25','unit':'USD','total':row.copy(),'today':row.copy(),
        'models':[dict(row,model=cfg.model_id)],'daily':[dict(row,date='2026-10-25')],
        'billing':{'object':'sub2api.key_billing','billing_scope':'token','peak_rate_enabled':False,
                  'group_rate_multiplier':.135,'resolved_rate_multiplier':.135,'effective_rate_multiplier':.135}}


@pytest.fixture
def prepared(sources,data):
    _,_,source=sources
    cfg=runtime_config(data,total_budget_usd=.5,max_calls=16)
    group={'local_project_key_match':True,'approved_group_label':cfg.provider_group,'observed_group_label':'C03 '+cfg.provider_group}
    catalogue={'name':cfg.model_id,'pricing':{'billing_mode':'token','per_request_price':None},'token_pricing':{'sell':{'multiplier':.15,
        'pricing':{'input_price':3e-7,'output_price':1.5e-6,'cache_read_price':1.5e-8,'cache_write_price':3.75e-7}}}}
    manifest=ablation.make_manifest(cfg,source,group,catalogue,reading(cfg),data)
    return cfg,manifest


def provider(cfg,monkeypatch,override=None):
    def tier(request,envelope,n):
        assert json.loads(request.content)['text']['format']['name']=='decision_output'
        envelope['service_tier']='default'
        return override(request,envelope,n) if override else httpx.Response(200,json=envelope)
    return fixture(cfg,monkeypatch,tier)


def test_frozen_paired_input_control_and_counterbalanced_order(prepared):
    cfg,m=prepared;obs=m['observations']
    assert [o['condition'] for o in obs]==['without_critic','with_critic','with_critic','without_critic',
                                          'without_critic','with_critic','with_critic','without_critic']
    assert len({o['observation_id'] for o in obs})==8
    for day in {o['decision'] for o in obs}:
        a,b=sorted([o for o in obs if o['decision']==day],key=lambda o:o['condition'])
        assert a['input']['features']==b['input']['features'] and a['input']['analysis']==b['input']['analysis']
        without=next(o for o in (a,b) if o['condition']=='without_critic')
        with_=next(o for o in (a,b) if o['condition']=='with_critic')
        assert {k:v for k,v in with_['input'].items() if k!='critic'}==without['input']
        assert canonical_hash(with_['input']['critic'])==with_['critic_hash']
    assert m['estimate']['whole_attempt_ceiling_usd']==pytest.approx(.36274176)
    assert m['configuration']['total_budget_usd']==.5 and m['configuration']['max_calls']==16


def test_eight_decision_calls_separate_budget_idempotence_and_replay(store,data,prepared,monkeypatch):
    cfg,m=prepared;api,requests=provider(cfg,monkeypatch)
    report=ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    assert report['status']=='COMPLETED' and len(requests)==8
    assert report['budget']['attempts']==8 and report['budget']['confirmed_actual_usd']==pytest.approx(.000432)
    assert all(p['both_valid'] for p in report['paired'])
    calls=[c for o in report['observations'] for c in o['calls']]
    assert len({c['call_id'] for c in calls})==8 and all(c['stage']=='decision' for c in calls)
    again=ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    assert len(requests)==8 and again['budget']['attempts']==8
    def forbidden(*a,**k):pytest.fail('Replay constructed or invoked provider')
    monkeypatch.setattr(OpenAIProvider,'__init__',forbidden);monkeypatch.setattr(OpenAIProvider,'reply',forbidden)
    monkeypatch.setattr(KuaiAPIProvider,'__init__',forbidden);monkeypatch.setattr(KuaiAPIProvider,'reply',forbidden)
    diagnostics=ablation.financial_replays(report,data)
    assert all(x['second_replay_equal'] and x['provider_calls']==x['replay_cost_usd']==0 for x in diagnostics.values())


@pytest.mark.parametrize('failure',['timeout_then_success','missing_usage','schema_400','tier_drift'])
def test_unknown_fees_or_schema_failure_stop_and_never_resume_inference(store,data,prepared,monkeypatch,failure):
    cfg,m=prepared
    def response(request,envelope,n):
        if failure=='timeout_then_success' and n==1:raise httpx.ReadTimeout('synthetic timeout')
        if failure=='missing_usage':envelope.pop('usage')
        if failure=='schema_400':return httpx.Response(400,json={'error':'unsupported strict schema'})
        if failure=='tier_drift':envelope['service_tier']='priority'
        return httpx.Response(200,json=envelope)
    api,requests=provider(cfg,monkeypatch,response)
    report=ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    assert report['status']=='STOPPED' and len(requests)==1
    assert sum(o['status']=='NOT_RUN' for o in report['observations'])==7
    if failure!='tier_drift':assert report['budget']['unknown_reserved_usd']>0
    before=len(requests)
    again=ablation.execute(store,m,cfg,data,api,lambda:pytest.fail('halted experiment must not advance'))
    assert len(requests)==before and again['budget']['attempts']==before


def test_interruption_retains_attempt_and_resume_does_not_repeat(store,data,prepared,monkeypatch):
    cfg,m=prepared
    def interrupt(*args):raise KeyboardInterrupt()
    api,requests=provider(cfg,monkeypatch,interrupt)
    with pytest.raises(KeyboardInterrupt):ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    report=ablation.execute(store,m,cfg,data,api,lambda:pytest.fail('interrupted experiment must stay halted'))
    assert len(requests)==1 and report['observations'][0]['status']=='INTERRUPTED'
    assert report['budget']['unknown_reserved_usd']>0 and report['budget']['attempts']==1


def test_runtime_multiplier_and_usage_delta_drift_stop_before_next_call(store,data,prepared,monkeypatch):
    cfg,m=prepared;api,requests=provider(cfg,monkeypatch)
    def drift():
        snapshot=reading(cfg,len(requests));snapshot['billing']['effective_rate_multiplier']=.14;return snapshot
    report=ablation.execute(store,m,cfg,data,api,drift)
    assert report['status']=='STOPPED' and requests==[]
    assert report['billing_checks'][0]['status']=='FAILED'
    altered=reading(cfg,1);altered['total']['requests']+=1
    with pytest.raises(ValueError):ablation.billing_delta(m,cfg,altered,[])


def test_attempt_cap_unknown_reservations_and_original_database_guard(store,prepared):
    cfg,m=prepared
    budget=PilotBudget(store,m)
    for index in range(16):budget.reserve(str(index),.02267136)
    assert budget.summary()['unknown_reserved_usd']==pytest.approx(.36274176)
    with pytest.raises(AgentFailure):budget.reserve('seventeenth',.001)
    before=store.path.read_bytes()
    with pytest.raises(ValueError):ablation.validate_database(store.path,m)
    assert store.path.read_bytes()==before


def test_recover_persisted_success_without_duplicate_request(store,data,prepared,monkeypatch):
    cfg,m=prepared;api,requests=provider(cfg,monkeypatch)
    first=ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    recovered=copy.deepcopy(first['observations'][0]);recovered['status']='RUNNING';recovered.pop('risk')
    with store.connect() as conn:
        conn.execute('UPDATE ablation_observations SET payload=? WHERE id=?',(json.dumps(recovered),recovered['observation_id']))
    report=ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    assert report['status']=='COMPLETED' and len(requests)==8 and report['budget']['attempts']==8


def test_readonly_collector_isolated_no_redirects_no_account_fields(prepared):
    cfg,m=prepared;key='synthetic-key-only';seen=[]
    def handle(request):
        seen.append(str(request.url));assert request.headers['Authorization']=='Bearer '+key
        snapshot=reading(cfg)
        if request.url.path.endswith('/billing'):return httpx.Response(200,json=snapshot['billing'])
        return httpx.Response(200,json={'unit':'USD','usage':{'total':snapshot['total'],'today':snapshot['today']},
            'daily_usage':snapshot['daily'],'model_stats':snapshot['models'],'planName':'private fixture','balance':999})
    result=ablation.capture_billing(key,'2026-10-25',httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=True))
    assert key not in json.dumps(result) and 'planName' not in result and 'balance' not in result
    assert len(seen)==2
    redirected=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(307,headers={'Location':'https://elsewhere.invalid'})),follow_redirects=True)
    with pytest.raises(httpx.HTTPStatusError):ablation.capture_billing(key,'2026-10-25',redirected)


def test_readonly_lag_polls_without_duplicate_inference(store,data,prepared,monkeypatch):
    cfg,m=prepared;api,requests=provider(cfg,monkeypatch);lag_reads=0
    monkeypatch.setattr(ablation.time,'sleep',lambda seconds:None)
    def lagged():
        nonlocal lag_reads
        if len(requests)==1 and lag_reads<2:
            lag_reads+=1;return reading(cfg,0)
        return reading(cfg,len(requests))
    report=ablation.execute(store,m,cfg,data,api,lagged)
    assert report['status']=='COMPLETED' and len(requests)==8 and lag_reads==2
    assert report['budget']['attempts']==8
    assert sum(c['status']=='FAILED' for c in report['billing_checks'])==2


def test_usd_cap_and_unknown_reservations_cannot_be_released(store,prepared):
    cfg,m=prepared;budget=PilotBudget(store,m)
    budget.reserve('uncertain',.49)
    with pytest.raises(AgentFailure):budget.reserve('would-exceed',.02267136)
    assert budget.summary()['unknown_reserved_usd']==.49 and budget.summary()['attempts']==1


@pytest.mark.parametrize('changed',['budget','attempts','group','tariff'])
def test_preregistration_rejects_changed_authorization_or_tariff(prepared,sources,data,changed):
    cfg,m=prepared
    group=copy.deepcopy(m['group_binding'])
    catalogue={'name':cfg.model_id,'pricing':{'billing_mode':'token','per_request_price':None},'token_pricing':{'sell':{'multiplier':.15,
        'pricing':{'input_price':3e-7,'output_price':1.5e-6,'cache_read_price':1.5e-8,'cache_write_price':3.75e-7}}}}
    if changed=='budget':cfg=cfg.model_copy(update={'total_budget_usd':2})
    if changed=='attempts':cfg=cfg.model_copy(update={'max_calls':32})
    if changed=='group':group['observed_group_label']='different'
    if changed=='tariff':catalogue['token_pricing']['sell']['pricing']['input_price']=6e-7
    with pytest.raises(ValueError):ablation.make_manifest(cfg,sources[2],group,catalogue,reading(cfg),data)


def test_financial_replay_preserves_failed_position_and_distinguishes_zero_liquidation(store,data,prepared,monkeypatch):
    cfg,m=prepared
    def response(request,envelope,n):
        text=envelope['output'][0]['content'][0]
        output=json.loads(text['text']);output['target_weight']=0 if n==3 else 1
        text['text']='invalid fixture JSON' if n==4 else json.dumps(output)
        return httpx.Response(200,json=envelope)
    api,requests=provider(cfg,monkeypatch,response)
    report=ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    assert report['status']=='STOPPED' and len(requests)==4
    assert report['paired'][1]['without_risk']['status']=='SKIPPED'
    assert report['paired'][1]['with_risk']['approved_weight']==0
    def forbidden(*a,**k):pytest.fail('financial diagnostic called a provider')
    monkeypatch.setattr(KuaiAPIProvider,'__init__',forbidden);monkeypatch.setattr(KuaiAPIProvider,'reply',forbidden)
    replay=ablation.financial_replays(report,data)
    assert replay['without_critic']['result']['equity'][-1]['shares']>0
    assert replay['with_critic']['result']['equity'][-1]['shares']==0


def test_completed_envelope_with_unterminated_json_is_billed_but_never_repaired_or_retried(store,data,prepared,monkeypatch):
    cfg,m=prepared
    def response(request,envelope,n):
        if n==2:
            envelope['service_tier']='auto'
            envelope['text']={'format':{'type':'json_schema'}}
            envelope['output'][0]['content'][0]['text']='{"confidence":0.6,"rationale":"synthetic unterminated'
        return httpx.Response(200,json=envelope)
    api,requests=provider(cfg,monkeypatch,response)
    report=ablation.execute(store,m,cfg,data,api,lambda:reading(cfg,len(requests)))
    assert len(requests)==2 and report['status']=='STOPPED'
    assert report['observations'][0]['status']=='COMPLETED'
    failed=report['observations'][1]
    assert failed['calls'][0]['error_code']=='INVALID_AGENT_OUTPUT' and failed['decision_output'] is None
    assert report['paired'][0]['raw_target_with'] is None
    assert sum(o['status']=='NOT_RUN' for o in report['observations'])==6
    assert report['budget']['unknown_reserved_usd']==0
    assert report['budget']['confirmed_actual_usd']==pytest.approx(.000108)
    again=ablation.execute(store,m,cfg,data,api,lambda:pytest.fail('stopped malformed response must not advance'))
    assert len(requests)==2 and again['budget']['attempts']==2
