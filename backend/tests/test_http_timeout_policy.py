"""Server transport policy fixtures, no live endpoints or private credentials."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest
from pydantic import ValidationError

from app.agents import AgentWorkflow,OpenAIProvider,AgentFailure
from app.http_policy import HTTPTimeoutPolicy
from app.models import ExperimentConfig
from app.pilot import PilotBudget,make_manifest
from test_gateway import data,store
from test_pilot_billing import runtime_config
from test_agents_and_runs import snapshot,dataset
from test_provider_acceptance_execution import frozen
from test_critic_ablation import prepared,provider,reading
from test_decision_difference import sources


@pytest.mark.parametrize('value',[None,0,-1,float('inf'),float('nan'),301,True,'30'])
def test_policy_rejects_unbounded_or_ambiguous_seconds(value):
    with pytest.raises(ValidationError):HTTPTimeoutPolicy(read=value)


def test_default_compatibility_frozen_fields_and_no_payload_policy(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-timeout-key')
    seen=[]
    def handle(request):
        seen.append(request)
        assert request.extensions['timeout']==dict(connect=30.0,read=30.0,write=30.0,pool=30.0)
        assert set(json.loads(request.content))=={'model','instructions','input','text','max_output_tokens','store'}
        return httpx.Response(200,json={'status':'completed','output':[],'usage':{'input_tokens':1,'output_tokens':1}})
    api=OpenAIProvider(httpx.Client(transport=httpx.MockTransport(handle)),model_id='fixture-model',input_rate=1,output_rate=2)
    api.reply('decision',{'features':{}},{})
    assert len(seen)==1
    with pytest.raises(ValidationError):api.timeout_policy.read=120
    with pytest.raises(ValidationError):HTTPTimeoutPolicy(total=120)
    with pytest.raises(ValidationError):ExperimentConfig(timeout_policy={'read':120})
    # model_copy/model_construct may bypass model validation in internal code;
    # provider construction must still reject an unbounded typed policy.
    with pytest.raises(ValidationError):
        OpenAIProvider(model_id='fixture-model',input_rate=1,output_rate=2,
            timeout_policy=HTTPTimeoutPolicy.model_construct(read=float('inf')))


@pytest.mark.parametrize('exc,phase',[(httpx.ConnectTimeout,'connect'),(httpx.ReadTimeout,'read'),
    (httpx.WriteTimeout,'write'),(httpx.PoolTimeout,'pool'),(httpx.TimeoutException,'unspecified')])
def test_sanitized_timeout_trace_no_retry_and_full_unknown_reserve(store,data,snapshot,monkeypatch,exc,phase):
    cfg=runtime_config(data);budget=PilotBudget(store,make_manifest(cfg,data));seen=[];events=[]
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-timeout-key')
    policy=HTTPTimeoutPolicy(connect=10,read=120,write=20,pool=5)
    def handle(request):
        seen.append(request)
        assert request.extensions['timeout']==policy.model_dump()
        assert events[-1]['status']=='RUNNING' and events[-1]['transport_timeout_policy']==policy.model_dump()
        raise exc('synthetic-timeout-key https://example.invalid/private-query')
    api=OpenAIProvider(httpx.Client(transport=httpx.MockTransport(handle)),model_id='fixture-model',input_rate=1,output_rate=2,timeout_policy=policy)
    workflow=AgentWorkflow(ExperimentConfig(strategy_id='single_agent',llm_mode='api',max_attempts_per_role=2),api,budget)
    result=workflow.evaluate(snapshot,lambda call:events.append(copy.deepcopy(call)))
    assert len(seen)==len(result['calls'])==1 and result['reason_codes']==['PROVIDER_TIMEOUT']
    call=result['calls'][0];diag=call['timeout_diagnostic']
    assert diag['phase']==phase and diag['exception_class']==exc.__name__
    assert diag['attempt_elapsed_ms']==call['latency_ms']>=0
    assert 'not a total wall-clock' in diag['semantics'] and diag['policy']==policy.model_dump()
    assert 'synthetic-timeout-key' not in json.dumps(result) and 'private-query' not in json.dumps(result)
    assert call['cost_status']=='unknown_reserved' and budget.summary()['unknown_reserved_usd']==pytest.approx(call['reserved_usd'])


def test_mutating_policy_after_workflow_freeze_prevents_request(snapshot,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-timeout-key')
    api=OpenAIProvider(model_id='fixture-model',input_rate=1,output_rate=2)
    workflow=AgentWorkflow(ExperimentConfig(strategy_id='single_agent',llm_mode='api'),api)
    api.timeout_policy=HTTPTimeoutPolicy(read=120)
    monkeypatch.setattr(api,'reply',lambda *a:pytest.fail('mutated frozen policy sent request'))
    assert workflow.evaluate(snapshot)['reason_codes']==['PROVIDER_CONFIGURATION_CHANGED']


def test_new_policy_changes_manifest_identity_without_changing_model_payload(data,store,monkeypatch):
    from app.pilot import execute_pilot
    from app.gateway import KuaiAPIProvider
    monkeypatch.setenv('KUAIAPI_API_KEY','synthetic-timeout-key')
    cfg=runtime_config(data);longer=cfg.model_copy(update={'timeout_policy':HTTPTimeoutPolicy(read=120)})
    a,b=make_manifest(cfg,data),make_manifest(longer,data)
    assert a['manifest_hash']!=b['manifest_hash'] and a['configuration']['timeout_policy']['read']==30
    assert b['transport_timeout_policy']['read']==120
    first,second=KuaiAPIProvider(cfg),KuaiAPIProvider(longer)
    assert first.payload('decision',{}, {})==second.payload('decision',{}, {})
    monkeypatch.setattr(second,'reply',lambda *a:pytest.fail('policy drift reached first-call gate'))
    with pytest.raises(ValueError,match='timeout policy'):
        execute_pilot(store,cfg,data,provider=second)


def test_changed_policy_refuses_existing_diagnostic_db_and_stopped_resume_never_sends(store,frozen,monkeypatch):
    import run_provider_acceptance as driver
    cfg,m,_=frozen
    def timeout(*a):raise httpx.PoolTimeout('synthetic')
    api,requests=provider(cfg,monkeypatch,timeout)
    result=driver.execute(store,m,cfg,api,lambda:reading(cfg,len(requests)))
    assert result['status']=='STOPPED' and len(requests)==1
    changed=copy.deepcopy(m);changed['configuration']['timeout_policy']['read']=120
    with pytest.raises(ValueError):driver.validate_database(store.path,changed)
    again=driver.execute(store,m,cfg,api,lambda:pytest.fail('stopped resume advanced'))
    assert again['budget']['attempts']==1 and again['budget']['unknown_reserved_usd']==pytest.approx(.02267136)


def test_default_tools_remain_offline(monkeypatch,capsys):
    import run_provider_acceptance as driver
    import check_provider_contract as checker
    def forbidden(*a,**k):pytest.fail('default path touched provider/key/network')
    monkeypatch.setattr(driver,'load_key',forbidden);monkeypatch.setattr(driver,'KuaiAPIProvider',forbidden)
    monkeypatch.setattr(httpx,'get',forbidden);monkeypatch.setattr(OpenAIProvider,'__init__',forbidden)
    monkeypatch.setattr(sys,'argv',['run_provider_acceptance.py']);driver.main()
    assert json.loads(capsys.readouterr().out)['key_loaded'] is False
    monkeypatch.setattr(sys,'argv',['check_provider_contract.py']);checker.main()
    assert json.loads(capsys.readouterr().out)['new_provider_calls']==0
