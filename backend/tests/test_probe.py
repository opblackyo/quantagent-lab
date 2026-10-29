"""Synthetic-only first-call billing gate/resume tests; no real credentials."""
import json
import httpx
import pytest
from app.pilot import execute_pilot, make_manifest
from app.storage import Store
from test_gateway import data, store, config, fixture, reconcile_paused


def record_for(paused,cfg,**updates):
    call=paused['probe']['result']['calls'][0]
    value={'manifest_hash':paused['manifest']['manifest_hash'],'call_id':call['call_id'],
        'provider_response_id':call['raw_provider_response']['id'],'provider_group':cfg.provider_group,
        'model_id':cfg.model_id,'actual_charge_usd':.0001,'evidence_sha256':'c'*64,
        'source':'owner-provided-private-record','key_group_binding_checked':True,
        'key_group_binding_evidence_sha256':'e'*64,'key_group_binding_source':'owner-verified-key-settings',
        'strict_output_and_usage_checked':True}
    return {**value,**updates}


def test_cache_creation_is_reserved_additively_and_input_bound_is_short(data,monkeypatch):
    cfg=config(data,input_balance_units_per_million=.24,output_balance_units_per_million=1.2,
               cache_write_balance_units_per_million=.30,cache_read_balance_units_per_million=.012)
    api,_=fixture(cfg,monkeypatch)
    reserve=api.reserve_cost('decision',{'features':{}},{})
    assert cfg.usd_rates()==pytest.approx((.54/6.8,1.2/6.8))
    assert reserve==pytest.approx((32768*.54+2048*1.2)/6.8/1e6)
    assert reserve > (32768*.24+2048*1.2)/6.8/1e6
    manifest=make_manifest(cfg,data)
    assert manifest['estimate']['input_token_bound_per_attempt']+cfg.max_output_tokens<272000
    assert manifest['estimate']['ceiling_reservation_usd']==pytest.approx(32*reserve)


def test_first_single_call_pauses_durably_and_reopening_never_repeats(store,data,monkeypatch):
    cfg=config(data);api,requests=fixture(cfg,monkeypatch)
    first=execute_pilot(store,cfg,data,provider=api)
    assert first['status']=='PAUSED_FOR_FIRST_CALL_RECONCILIATION'
    assert len(requests)==first['budget']['attempts']==1 and first['runs']==[]
    assert first['budget']['invoiced_actual_usd'] is None
    path=store.path;store.close()
    reopened=Store(path);reopened.initialize()
    try:
        again=execute_pilot(reopened,cfg,data,provider=api)
        assert len(requests)==1
        assert again['probe']['result']['calls'][0]['call_id']==first['probe']['result']['calls'][0]['call_id']
    finally:reopened.close()


@pytest.mark.parametrize('bad',[{'provider_group':'wrong'},{'provider_response_id':'wrong'},
    {'call_id':'wrong'},{'key_group_binding_checked':False},{'strict_output_and_usage_checked':False},
    {'key_group_binding_evidence_sha256':None},{'key_group_binding_source':None}])
def test_reconciliation_must_match_probe_and_verified_binding(store,data,monkeypatch,bad):
    cfg=config(data);api,requests=fixture(cfg,monkeypatch)
    paused=execute_pilot(store,cfg,data,provider=api)
    with pytest.raises(ValueError):
        execute_pilot(store,cfg,data,provider=api,probe_reconciliation=record_for(paused,cfg,**bad))
    assert len(requests)==1


def test_resume_reuses_probe_in_single_and_separates_confirmed_actual(store,data,monkeypatch):
    cfg=config(data);api,requests=fixture(cfg,monkeypatch)
    paused=execute_pilot(store,cfg,data,provider=api)
    call_id=paused['probe']['result']['calls'][0]['call_id']
    report=execute_pilot(store,cfg,data,provider=api,probe_reconciliation=record_for(paused,cfg))
    assert len(requests)==report['budget']['attempts']==16
    assert report['runs'][2]['result']['decisions'][0]['calls'][0]['call_id']==call_id
    assert [r['metrics']['llm_calls'] for r in report['runs']]==[0,0,4,12]
    assert report['budget']['confirmed_actual_usd']==.0001
    assert report['budget']['usage_estimated_usd']==pytest.approx(16*.00014)
    assert report['budget']['charged_or_reserved_usd']==pytest.approx(15*.00014+.0001)
    assert report['budget']['invoiced_actual_usd'] is None
    assert all(e['equal'] and e['provider_calls']==0 for e in report['replay_equality'])
    repeated=execute_pilot(store,cfg,data,provider=api)
    assert len(requests)==16 and repeated['probe']['status']=='RECONCILED'


@pytest.mark.parametrize('failure',['timeout','unknown_usage','schema_unsupported'])
def test_unchecked_probe_never_retries_or_continues(store,data,monkeypatch,failure):
    cfg=config(data)
    def response(request,env,n):
        if failure=='timeout':raise httpx.ReadTimeout('fixture timeout')
        if failure=='schema_unsupported':return httpx.Response(400,json={'error':'unsupported schema fixture'})
        env.pop('usage');return httpx.Response(200,json=env)
    api,requests=fixture(cfg,monkeypatch,response)
    paused=execute_pilot(store,cfg,data,provider=api)
    assert len(requests)==1 and paused['status']=='PAUSED_FOR_FIRST_CALL_RECONCILIATION'
    assert paused['budget']['unknown_reserved_usd']>0
    assert execute_pilot(store,cfg,data,provider=api)['budget']['attempts']==1
    assert len(requests)==1


def test_interrupted_probe_retains_uncertain_cost_and_does_not_repeat(store,data,monkeypatch):
    cfg=config(data)
    def response(*args):raise KeyboardInterrupt()
    api,requests=fixture(cfg,monkeypatch,response)
    with pytest.raises(KeyboardInterrupt):execute_pilot(store,cfg,data,provider=api)
    paused=execute_pilot(store,cfg,data,provider=api)
    assert paused['probe']['status']=='INTERRUPTED' and len(requests)==1
    assert paused['budget']['unknown_reserved_usd']>0


def test_actual_charge_overrun_blocks_and_cannot_be_rewritten(store,data,monkeypatch):
    cfg=config(data);api,requests=fixture(cfg,monkeypatch)
    paused=execute_pilot(store,cfg,data,provider=api)
    record=record_for(paused,cfg,actual_charge_usd=3)
    with pytest.raises(ValueError,match='exceeds total'):
        execute_pilot(store,cfg,data,provider=api,probe_reconciliation=record)
    with pytest.raises(ValueError,match='immutable'):
        execute_pilot(store,cfg,data,provider=api,probe_reconciliation=record_for(paused,cfg))
    assert len(requests)==1
