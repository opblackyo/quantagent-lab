"""Offline singleton USD reconciliation and non-resetting migration fixtures."""
import copy
import json

import pytest
from pydantic import ValidationError

from app.agents import AgentFailure
from app.data import canonical_hash,sha256
from app.pilot import PilotBudget,PilotConfig,execute_pilot,make_manifest
from app.pilot_billing import record_readonly_charge,register_price_revision,record_aggregate_charge
from test_gateway import data,store,config,fixture


def runtime_config(data,**updates):
    values=config(data).model_dump(mode='json')
    values.update(pricing_unit='runtime_usd_per_million',input_balance_units_per_million=None,
        output_balance_units_per_million=None,balance_units_per_usd=None,
        input_usd_per_million=.27,output_usd_per_million=1.35,
        cache_write_usd_per_million=.3375,cache_read_usd_per_million=.0135,
        runtime_rate_multiplier=.135,runtime_pricing_verified=True)
    return PilotConfig(**{**values,**updates})


def probe(store,data,monkeypatch):
    cfg=config(data)
    def usage(request,env,n):
        import httpx
        env['usage']={'input_tokens':373,'output_tokens':202,'total_tokens':575}
        return httpx.Response(200,json=env)
    api,requests=fixture(cfg,monkeypatch,usage)
    paused=execute_pilot(store,cfg,data,provider=api)
    return cfg,api,requests,paused


def evidence_for(paused):
    call=paused['probe']['result']['calls'][0]
    row={'requests':1,'input_tokens':373,'output_tokens':202,'total_tokens':575,
         'cache_read_tokens':0,'cache_creation_tokens':0,'cost':.002766,'actual_cost':.00037341}
    evidence={'source':'project-key-authenticated-readonly-gateway-usage','unit':'USD',
        'query':{'start_date':'2026-10-25','end_date':'2026-10-25','days':1,'timezone':'Asia/Taipei'},
        'daily_usage':[dict(row,date='2026-10-25')],'model_stats':[dict(row,model=call['model_id'])],
        'usage_today':row.copy(),'usage_total':row.copy(),
        'billing':{'object':'sub2api.key_billing','billing_scope':'token','peak_rate_enabled':False,
            'group_rate_multiplier':.135,'resolved_rate_multiplier':.135,'effective_rate_multiplier':.135}}
    window={'started_at_utc':'2026-10-24T19:14:17+00:00','finished_at_utc':'2026-10-24T19:15:15+00:00',
            'source_commit':paused['manifest']['code_commit']}
    return evidence,window


def encode(value):
    return json.dumps(value).encode()


def record(store,paused):
    evidence,window=evidence_for(paused)
    return record_readonly_charge(store,paused['manifest']['manifest_hash'],encode(evidence),encode(window))


def clean_revision(data,**updates):
    manifest=make_manifest(runtime_config(data,**updates),data)
    manifest.update(code_dirty=False,code_commit='f'*40)
    manifest['manifest_hash']=canonical_hash({k:v for k,v in manifest.items() if k!='manifest_hash'})
    return manifest


def test_runtime_usd_no_conversion_or_double_multiplier_and_cache_reserve(data,monkeypatch):
    cfg=runtime_config(data);api,_=fixture(cfg,monkeypatch)
    assert cfg.usd_rates()==pytest.approx((.6075,1.35))
    assert api.reserve_cost('decision',{}, {})==pytest.approx((32768*.6075+2048*1.35)/1e6)
    assert api.pricing_attribution['unit']=='USD per 1M tokens'
    assert 'balance_units_per_usd' not in api.pricing_attribution
    assert make_manifest(cfg,data)['estimate']['ceiling_reservation_usd']<2


@pytest.mark.parametrize('updates',[{'runtime_pricing_verified':False},{'cache_write_usd_per_million':None},
    {'cache_read_usd_per_million':None},{'runtime_rate_multiplier':None}])
def test_incomplete_runtime_tariff_cannot_construct_provider_or_execute(store,data,monkeypatch,updates):
    cfg=runtime_config(data,**updates)
    assert cfg.missing() and cfg.usd_rates()==(None,None)
    monkeypatch.setenv('KUAIAPI_API_KEY','fixture-no-request')
    from app.gateway import KuaiAPIProvider
    with pytest.raises(ValueError):KuaiAPIProvider(cfg)
    with pytest.raises(ValueError):execute_pilot(store,cfg,data)


@pytest.mark.parametrize('updates',[{'balance_units_per_usd':6.8},{'input_balance_units_per_million':.24},
    {'cache_write_usd_per_million':-1},{'runtime_rate_multiplier':True}])
def test_runtime_pricing_rejects_mixed_units_and_invalid_values(data,updates):
    with pytest.raises(ValidationError):runtime_config(data,**updates)


def test_singleton_actual_usd_record_is_idempotent_and_does_not_unlock_group(store,data,monkeypatch):
    cfg,api,requests,paused=probe(store,data,monkeypatch)
    budget=PilotBudget(store,paused['manifest'])
    old=budget.summary()['usage_estimated_usd']
    receipt=record(store,paused)
    assert receipt['actual_charge_usd']=='0.00037341'
    assert receipt['key_group_binding_checked'] is False and receipt['gateway_request_id_exposed'] is False
    assert receipt['correlation']=='singleton-date-model-tokens' and receipt['replay_contract_equal']
    assert record(store,paused)==receipt
    after=execute_pilot(store,cfg,data,provider=api)
    assert len(requests)==1 and after['runs']==[]
    assert after['probe']['status']=='BILLING_RECONCILED_GROUP_UNVERIFIED'
    assert budget.summary()['attempts']==1
    assert budget.summary()['charged_or_reserved_usd']==.00037341
    assert budget.summary()['confirmed_actual_usd']==.00037341
    assert budget.summary()['usage_estimated_usd']==old
    assert budget.summary()['invoiced_actual_usd'] is None


@pytest.mark.parametrize('kind',['unit','requests','model','tokens','cache','multiplier','cost','date','bool','window','source'])
def test_ambiguous_or_inconsistent_readonly_evidence_never_confirms_charge(store,data,monkeypatch,kind):
    _,_,requests,paused=probe(store,data,monkeypatch)
    evidence,window=evidence_for(paused)
    if kind=='unit':evidence['unit']='native'
    if kind=='requests':evidence['usage_total']['requests']=2
    if kind=='model':evidence['model_stats'][0]['model']='wrong'
    if kind=='tokens':evidence['usage_today']['input_tokens']=374
    if kind=='cache':evidence['daily_usage'][0]['cache_creation_tokens']=1
    if kind=='multiplier':evidence['billing']['effective_rate_multiplier']=.12
    if kind=='cost':evidence['model_stats'][0]['actual_cost']=.00037341/6.8
    if kind=='date':evidence['query']['end_date']='2026-10-24'
    if kind=='bool':evidence['usage_total']['requests']=True
    if kind=='window':window['started_at_utc']='2026-10-23T19:14:17+00:00'
    if kind=='source':window['source_commit']='wrong'
    with pytest.raises(ValueError):record_readonly_charge(store,paused['manifest']['manifest_hash'],encode(evidence),encode(window))
    assert PilotBudget(store,paused['manifest']).summary()['confirmed_actual_usd']==0 and len(requests)==1


def test_charge_and_revision_cannot_rewrite_actual_or_reset_budget(store,data,monkeypatch):
    _,_,requests,paused=probe(store,data,monkeypatch)
    original=paused['manifest']; old_budget=PilotBudget(store,original)
    charge=record(store,paused)
    revision=clean_revision(data)
    receipt=register_price_revision(store,original,revision,charge['evidence_sha256'])
    assert register_price_revision(store,original,revision,charge['evidence_sha256'])==receipt
    budget=PilotBudget(store,revision,original['manifest_hash'])
    assert budget.id==original['manifest_hash'] and budget.summary()['attempts']==1
    assert budget.summary()['charged_or_reserved_usd']==.00037341
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM pilot_budgets').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM pilot_probes').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM pilot_revisions').fetchone()[0]==1
    with pytest.raises(ValueError):PilotBudget(store,original)
    with pytest.raises(ValueError):old_budget.reserve('stale-pricing',.01)
    with pytest.raises(ValueError):PilotBudget(store,revision) # No fresh budget under revision ID.
    budget.reserve('next',1.999)
    assert budget.summary()['attempts']==2
    with pytest.raises(AgentFailure):budget.reserve('over-cap',.001)
    evidence,window=evidence_for(paused)
    for row in [*evidence['daily_usage'],*evidence['model_stats'],evidence['usage_total'],evidence['usage_today']]:
        row.update(cost=.004,actual_cost=.00054)
    with pytest.raises(ValueError):record_readonly_charge(store,original['manifest_hash'],encode(evidence),encode(window))
    assert budget.summary()['confirmed_actual_usd']==.00037341 and len(requests)==1


@pytest.mark.parametrize('updates',[{'model_id':'changed'},{'provider_group':'changed'},{'total_budget_usd':3},
    {'total_budget_usd':1},{'max_calls':33},{'max_output_tokens':4096},{'fee_bps':6},{'start_date':'2023-01-02'}])
def test_price_revision_rejects_research_and_authorization_changes(store,data,monkeypatch,updates):
    _,_,requests,paused=probe(store,data,monkeypatch)
    charge=record(store,paused)
    with pytest.raises(ValueError):register_price_revision(store,paused['manifest'],clean_revision(data,**updates),charge['evidence_sha256'])
    assert len(requests)==1 and PilotBudget(store,paused['manifest']).summary()['attempts']==1


def test_revision_requires_hash_integrity_clean_source_and_charge_evidence(store,data,monkeypatch):
    _,_,_,paused=probe(store,data,monkeypatch)
    charge=record(store,paused);revision=clean_revision(data)
    wrong=copy.deepcopy(revision);wrong['configuration']['total_budget_usd']=3
    with pytest.raises(ValueError):register_price_revision(store,paused['manifest'],wrong,charge['evidence_sha256'])
    dirty=copy.deepcopy(revision);dirty['code_dirty']=True
    dirty['manifest_hash']=canonical_hash({k:v for k,v in dirty.items() if k!='manifest_hash'})
    with pytest.raises(ValueError):register_price_revision(store,paused['manifest'],dirty,charge['evidence_sha256'])
    with pytest.raises(ValueError):register_price_revision(store,paused['manifest'],revision,'d'*64)
    with pytest.raises(ValueError):PilotBudget(store,revision,paused['manifest']['manifest_hash'])


def test_blocked_rate_revision_preserves_anchor_and_cannot_infer(store,data,monkeypatch):
    _,_,requests,paused=probe(store,data,monkeypatch)
    charge=record(store,paused)
    revision=clean_revision(data,runtime_pricing_verified=False,cache_write_usd_per_million=None)
    register_price_revision(store,paused['manifest'],revision,charge['evidence_sha256'])
    assert PilotBudget(store,revision,paused['manifest']['manifest_hash']).summary()['attempts']==1
    with pytest.raises(ValueError):execute_pilot(store,runtime_config(data,runtime_pricing_verified=False),data,
                                              anchor_manifest_hash=paused['manifest']['manifest_hash'])
    assert len(requests)==1


def test_singleton_usage_hash_cannot_double_as_group_binding_proof(store,data,monkeypatch):
    from test_probe import record_for
    cfg,api,requests,paused=probe(store,data,monkeypatch)
    charge=record(store,paused)
    forged=record_for(paused,cfg,actual_charge_usd=.00037341,
                      key_group_binding_evidence_sha256=charge['evidence_sha256'])
    with pytest.raises(ValueError,match='group name'):
        execute_pilot(store,cfg,data,provider=api,probe_reconciliation=forged)
    assert len(requests)==1


def test_revised_budget_still_counts_all_32_attempts_and_unknown_charges(store,data,monkeypatch):
    _,_,requests,paused=probe(store,data,monkeypatch)
    charge=record(store,paused);revision=clean_revision(data)
    register_price_revision(store,paused['manifest'],revision,charge['evidence_sha256'])
    budget=PilotBudget(store,revision,paused['manifest']['manifest_hash'])
    for n in range(31):budget.reserve('unknown-'+str(n),.001)
    with pytest.raises(AgentFailure):budget.reserve('thirty-third',.001)
    assert budget.summary()['attempts']==32 and budget.summary()['unknown_reserved_usd']==.031
    assert budget.summary()['charged_or_reserved_usd']==pytest.approx(.03137341)
    assert len(requests)==1


def test_explicit_same_anchor_cannot_bypass_manifest_or_cap_checks(store,data,monkeypatch):
    _,_,_,paused=probe(store,data,monkeypatch)
    forged=copy.deepcopy(paused['manifest']);forged['configuration']['total_budget_usd']=100
    with pytest.raises(ValueError,match='hash mismatch'):
        PilotBudget(store,forged,paused['manifest']['manifest_hash'])


def test_budget_api_after_revision_exposes_same_actual_and_attempts(store,data,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    _,_,requests,paused=probe(store,data,monkeypatch)
    charge=record(store,paused);revision=clean_revision(data)
    register_price_revision(store,paused['manifest'],revision,charge['evidence_sha256'])
    path=store.path;store.close()
    with TestClient(create_app(path)) as client:
        response=client.get('/api/v1/pilots/'+paused['manifest']['manifest_hash']+'/budget')
        assert response.status_code==200
        budget=response.json()
        assert budget['attempts']==1 and budget['confirmed_actual_usd']==.00037341
        assert budget['limit_usd']==2 and budget['max_calls']==32
        assert client.get('/api/v1/pilots/'+revision['manifest_hash']+'/budget').status_code==404
    assert len(requests)==1


def test_ui_group_mapping_and_anchored_completion_then_aggregate_usd(store,data,monkeypatch):
    from test_probe import record_for
    cfg,api,requests,paused=probe(store,data,monkeypatch)
    charge=record(store,paused);root=paused['manifest']['manifest_hash']
    current=runtime_config(data);revision=clean_revision(data)
    register_price_revision(store,paused['manifest'],revision,charge['evidence_sha256'])
    revised_api,next_requests=fixture(current,monkeypatch)
    monkeypatch.setattr('app.pilot.make_manifest',lambda *args:revision)
    binding=record_for(paused,current,actual_charge_usd=.00037341,
        key_group_binding_source='authenticated-readonly-key-settings-ui',
        observed_key_group_label='C03 '+current.provider_group,group_display_prefix='C03',
        key_match_scope='unique-project-row-masked-prefix6-suffix4')
    bad={**binding,'observed_key_group_label':'C03 different-group'}
    with pytest.raises(ValueError,match='label mapping'):
        execute_pilot(store,current,data,provider=revised_api,probe_reconciliation=bad,anchor_manifest_hash=root)
    report=execute_pilot(store,current,data,provider=revised_api,probe_reconciliation=binding,anchor_manifest_hash=root)
    assert len(requests)==1 and len(next_requests)==15
    assert report['status']=='COMPLETED' and report['budget']['attempts']==16
    assert all(r['config']['research_group_id']==root for r in report['runs'])
    assert report['runs'][2]['result']['decisions'][0]['calls'][0]['call_id']==paused['probe']['result']['calls'][0]['call_id']
    assert all(e['equal'] and e['provider_calls']==0 for e in report['replay_equality'])
    evidence,window=evidence_for(paused)
    for row in [*evidence['daily_usage'],*evidence['model_stats'],evidence['usage_total'],evidence['usage_today']]:
        row.update(requests=16,input_tokens=1873,output_tokens=502,total_tokens=2375,cost=.008766,actual_cost=.00118341)
    for mutation in ['requests','tokens','unit','charge']:
        bad=copy.deepcopy(evidence)
        if mutation=='requests':bad['usage_total']['requests']=17
        if mutation=='tokens':bad['usage_total']['input_tokens']+=1
        if mutation=='unit':bad['unit']='native'
        if mutation=='charge':bad['usage_total']['actual_cost']=.00118341/6.8
        with pytest.raises(ValueError):record_aggregate_charge(store,root,encode(bad))
    with store.connect() as conn:
        saved=conn.execute('SELECT call_id,usage_estimate FROM pilot_attempts WHERE call_id!=?',
                           (paused['probe']['result']['calls'][0]['call_id'],)).fetchone()
        conn.execute('UPDATE pilot_attempts SET usage_estimate=NULL WHERE call_id=?',(saved[0],))
    with pytest.raises(ValueError,match='unknown'):
        record_aggregate_charge(store,root,encode(evidence))
    with store.connect() as conn:
        conn.execute('UPDATE pilot_attempts SET usage_estimate=? WHERE call_id=?',(saved[1],saved[0]))
    receipt=record_aggregate_charge(store,root,encode(evidence))
    assert record_aggregate_charge(store,root,encode(evidence))==receipt
    budget=PilotBudget(store,revision,root)
    assert budget.summary()['confirmed_actual_usd']==.00118341
    assert budget.summary()['charged_or_reserved_usd']>=.00118341
    assert budget.summary()['invoiced_actual_usd'] is None
    budget.reserve('later-unknown',.01)
    assert budget.summary()['unknown_reserved_usd']==.01
    assert budget.summary()['charged_or_reserved_usd']>=.01118341
