"""Six-request synthetic HTTP diagnostics; no paid calls or project keys."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.agents import OpenAIProvider,AgentFailure
from app.data import canonical_hash
from app.pilot import PilotBudget
from test_critic_ablation import prepared,provider,reading
from test_decision_difference import sources
from test_gateway import data,store

spec=importlib.util.spec_from_file_location('acceptance_driver',Path(__file__).resolve().parents[2]/'tools/run_provider_acceptance.py')
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)


@pytest.fixture
def frozen(prepared):
    oldcfg,source=prepared
    cfg=oldcfg.model_copy(update={'total_budget_usd':.2,'max_calls':6,'max_attempts_per_role':1})
    inputs={}
    for label,obs in zip(('A','B'),source['observations'][:2]):
        request=OpenAIProvider.payload(SimpleNamespace(model_id=cfg.model_id,settings=cfg.provider_settings()),'decision',obs['input'],source['prompt']['schema'])
        inputs[label]={**copy.deepcopy(obs),'request':request,'request_hash':canonical_hash(request)}
    proposal={'sequence':list(driver.SEQUENCE),'source_ablation_manifest_sha256':'a'*64,'configuration':{
        'model_id':cfg.model_id,'provider_group':cfg.provider_group,'provider_settings':cfg.provider_settings()},
        'inputs':inputs,'dataset_hash':source['dataset_hash'],'source_run_ids':source['source_run_ids'],
        'prompt':source['prompt'],'prompt_hash':source['prompt_hash'],'schema_hash':source['schema_hash'],
        'pricing':{'per_attempt_reserve_usd':'.02267136'},'historical_accounting_separate':{
            'last_saved_cumulative_key_requests':16,'last_saved_cumulative_key_actual_usd':'.001'}}
    proposal['proposal_hash']=canonical_hash(proposal)
    group=source['group_binding'];authorization={'exact_owner_reply':'synthetic fixture approval','approved_proposal_sha256':'b'*64}
    catalogue={'name':cfg.model_id,'pricing':{'billing_mode':'token','per_request_price':None},'token_pricing':{'sell':{'multiplier':.15,
        'pricing':{'input_price':3e-7,'output_price':1.5e-6,'cache_read_price':1.5e-8,'cache_write_price':3.75e-7}}}}
    args=(proposal,source,cfg,group,catalogue,reading(cfg),authorization,'b'*64,'a'*64)
    return cfg,driver.prepare_manifest(*args),args


def test_pure_freeze_two_inputs_ids_and_no_tuning(frozen):
    cfg,m,args=frozen
    assert [o['condition'] for o in m['observations']]==['A','B','B','A','A','B']
    assert len({o['slot_id'] for o in m['observations']})==6 and m['maximum_reserved_usd']=='0.13602816'
    assert cfg.max_attempts_per_role==1 and m['configuration']['max_calls']==6
    a,b=m['observations'][:2]
    assert a['input']['input']=={k:v for k,v in b['input']['input'].items() if k!='critic'}
    other=driver.prepare_manifest(*args)
    assert other['experiment_id']!=m['experiment_id'] and other['manifest_hash']!=m['manifest_hash']


def test_http_collection_known_charge_format_failures_and_durable_holds(store,frozen,monkeypatch):
    cfg,m,_=frozen
    def respond(request,env,n):
        with store.connect() as conn:
            rows=conn.execute("SELECT * FROM pilot_attempts WHERE status='unknown_reserved'").fetchall()
            assert len(rows)==1 and float(rows[0]['reserved'])==pytest.approx(.02267136)
            obs=json.loads(conn.execute('SELECT payload FROM contract_slots WHERE slot=?',(n,)).fetchone()[0])
            assert obs['status']=='RUNNING' and obs['calls'][0]['status']=='RUNNING'
            assert obs['calls'][0]['call_id']==rows[0]['call_id']
        assert json.loads(request.content)==m['observations'][n-1]['input']['request']
        env['id']='synthetic-response-'+str(n)
        env['service_tier']='auto' if n%2 else 'default'
        if n==2:env['output'][0]['content'][0]['text']='{"rationale":"synthetic unterminated'
        if n==3:env['output'][0]['content']=[{'type':'refusal','refusal':'Synthetic refusal'}]
        if n==4:env.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
        if n==5:env['output'][0]['status']='incomplete'
        return httpx.Response(200,json=env)
    api,requests=provider(cfg,monkeypatch,respond)
    result=driver.execute(store,m,cfg,api,lambda:reading(cfg,len(requests)))
    assert result['status']=='COMPLETED' and len(requests)==6 and result['valid_outputs']==2
    assert not result['provisional_acceptance']
    assert result['budget']['confirmed_actual_usd']==pytest.approx(.000324)
    assert result['budget']['unknown_reserved_usd']==0 and result['budget']['attempts']==6
    assert all(o['status']=='SETTLED' for o in result['observations'])
    calls=[o['calls'][0] for o in result['observations']]
    assert len({c['call_id'] for c in calls})==6 and all(c['stage']=='decision' and c['attempt']==1 for c in calls)
    assert all(c['input_hash']==o['input']['input_hash'] for c,o in zip(calls,result['observations']))


@pytest.mark.parametrize('failure',['timeout','missing_usage','http429','redirect','billing_drift','model_drift','tool'])
def test_hard_stops_one_attempt_no_retry_and_no_resume(store,frozen,monkeypatch,failure):
    cfg,m,_=frozen
    def respond(request,env,n):
        if failure=='timeout':raise httpx.ReadTimeout('Synthetic timeout')
        if failure=='missing_usage':env.pop('usage')
        if failure=='http429':return httpx.Response(429,json={'error':'synthetic throttling'})
        if failure=='redirect':return httpx.Response(307,headers={'Location':'https://elsewhere.invalid'})
        if failure=='model_drift':env['model']='different-synthetic-model'
        if failure=='tool':env['output'].append({'type':'function_call','name':'unrequested'})
        return httpx.Response(200,json=env)
    api,requests=provider(cfg,monkeypatch,respond)
    def readonly():
        value=reading(cfg,len(requests))
        if failure=='billing_drift' and requests:value['billing']['effective_rate_multiplier']=.14
        return value
    result=driver.execute(store,m,cfg,api,readonly)
    assert result['status']=='STOPPED' and len(requests)==1 and result['budget']['attempts']==1
    assert sum(o['status']=='NOT_RUN' for o in result['observations'])==5
    if failure not in ('model_drift','tool'):assert result['budget']['unknown_reserved_usd']==pytest.approx(.02267136)
    again=driver.execute(store,m,cfg,api,lambda:pytest.fail('halted diagnostic performed readonly work'))
    assert again['budget']['attempts']==1 and len(requests)==1


def test_successful_diagnostic_resume_is_zero_provider_calls(store,frozen,monkeypatch):
    cfg,m,_=frozen;api,requests=provider(cfg,monkeypatch)
    first=driver.execute(store,m,cfg,api,lambda:reading(cfg,len(requests)))
    assert first['provisional_acceptance'] and first['valid_outputs']==6
    monkeypatch.setattr(api,'reply',lambda *a:pytest.fail('successful diagnostic was repeated'))
    again=driver.execute(store,m,cfg,api,lambda:reading(cfg,len(requests)))
    assert again['provisional_acceptance'] and len(requests)==6 and again['budget']['attempts']==6


def test_interrupted_attempt_retains_full_liability_and_no_repeat(store,frozen,monkeypatch):
    cfg,m,_=frozen
    def interrupt(*args):raise KeyboardInterrupt()
    api,requests=provider(cfg,monkeypatch,interrupt)
    with pytest.raises(KeyboardInterrupt):driver.execute(store,m,cfg,api,lambda:reading(cfg,len(requests)))
    again=driver.execute(store,m,cfg,api,lambda:pytest.fail('interrupted request must not advance'))
    assert again['observations'][0]['status']=='UNCERTAIN_INTERRUPTED'
    assert again['budget']['unknown_reserved_usd']==pytest.approx(.02267136) and len(requests)==1


def test_old_database_guard_and_global_attempt_ceiling(store,frozen,prepared):
    cfg,m,_=frozen
    _,old=prepared;PilotBudget(store,old)
    with pytest.raises(ValueError):driver.validate_database(store.path,m)
    with pytest.raises(ValueError):driver.execute(store,m,cfg,None,lambda:pytest.fail('foreign accounting'))


def test_orphan_reservation_cannot_issue_or_reset(store,frozen):
    cfg,m,_=frozen
    with store.connect() as conn:
        conn.execute('CREATE TABLE contract_meta (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('INSERT INTO contract_meta VALUES (?,?)',('manifest',json.dumps(m)))
    budget=PilotBudget(store,m);budget.reserve('orphan-fixture',.02267136)
    result=driver.execute(store,m,cfg,None,lambda:pytest.fail('orphan cannot advance'))
    assert result['status']=='STOPPED' and result['budget']['unknown_reserved_usd']==pytest.approx(.02267136)
    assert all(o['status']=='NOT_RUN' for o in result['observations'])


def test_new_attempt_ceiling_is_six_not_old_sixteen(store,frozen):
    _,m,_=frozen;budget=PilotBudget(store,m)
    for n in range(6):budget.reserve(str(n),.02267136)
    with pytest.raises(AgentFailure):budget.reserve('seventh',.001)
    assert budget.summary()['attempts']==6


@pytest.mark.parametrize('drift',['input','group','usage','schema'])
def test_preflight_rejects_alignment_group_history_and_schema_drift(frozen,drift):
    _,_,original=frozen;args=list(copy.deepcopy(original));proposal,source,cfg,group,catalogue,baseline,*_=args
    if drift=='input':proposal['inputs']['B']['input']['analysis']['thesis']='Synthetic changed predecessor'
    if drift=='group':group['observed_group_label']='Different synthetic group'
    if drift=='usage':baseline['total']['requests']+=1
    if drift=='schema':proposal['prompt']['instructions']='Changed synthetic instructions'
    proposal['proposal_hash']=canonical_hash({k:v for k,v in proposal.items() if k!='proposal_hash'})
    with pytest.raises(ValueError):driver.prepare_manifest(*args)


def test_new_driver_default_remains_offline_without_loading_key(monkeypatch,capsys):
    def forbidden(*a,**k):pytest.fail('default attempted credential/provider/network work')
    monkeypatch.setattr(driver,'load_key',forbidden);monkeypatch.setattr(driver,'capture_billing',forbidden)
    monkeypatch.setattr(driver,'KuaiAPIProvider',forbidden);monkeypatch.setattr(httpx,'get',forbidden)
    monkeypatch.setattr(sys,'argv',['run_provider_acceptance.py']);driver.main()
    assert json.loads(capsys.readouterr().out)=={'offline':True,'new_provider_calls':0,'key_loaded':False,'execute':False}
