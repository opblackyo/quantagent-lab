"""KuaiAPI compatibility fixtures only. No application API call or real key."""
import importlib.util
import json
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.agents import AgentWorkflow, MockProvider, OpenAIProvider
from app.data import DatasetRegistry
from app.features import snapshots
from app.gateway import KuaiAPIProvider
from app.models import ExperimentConfig
from app.pilot import PilotBudget, PilotConfig, execute_pilot, make_manifest
from app.storage import Store


@pytest.fixture(scope='module')
def data():
    original=DatasetRegistry().load('synthetic-spy-v1')
    return replace(original,metadata={**original.metadata,'kind':'historical','source':'Synthetic gateway fixture, not real market data'})


def config(data,**updates):
    values=dict(provider='kuaiapi',provider_group='fixture-group',pricing_unit='balance_units_per_million',
        input_balance_units_per_million=6.8,output_balance_units_per_million=13.6,balance_units_per_usd=6.8,
        gateway_routing_acknowledged=True,price_evidence_sha256='a'*64,
        dataset_id=data.metadata['id'],dataset_hash=data.hash,model_id='gateway-fixture-model',
        total_budget_usd=2,price_source='https://kuaiapi.net/model-plaza/',price_verified_on='2026-10-25')
    return PilotConfig(**{**values,**updates})


@pytest.fixture
def store(tmp_path):
    s=Store(tmp_path/'gateway.sqlite3');s.initialize()
    yield s
    s.close()


def fixture(config,monkeypatch,override=None):
    monkeypatch.setenv('KUAIAPI_API_KEY','gateway-fixture-key')
    monkeypatch.setenv('OPENAI_API_KEY','direct-fixture-key')
    requests=[]
    def handle(request):
        assert str(request.url)=='https://kuaiapi.net/v1/responses'
        assert request.headers['authorization']=='Bearer gateway-fixture-key'
        body=json.loads(request.content);requests.append(body)
        assert body['text']['format']['strict'] is True
        assert body['text']['format']['schema']['additionalProperties'] is False
        assert body['store'] is False and 'service_tier' not in body and 'tools' not in body
        stage=body['text']['format']['name'].removesuffix('_output')
        raw=MockProvider().reply(stage,json.loads(body['input']),{}).raw
        env={'id':'fixture-response-'+str(len(requests)),'status':'completed','model':config.model_id,'output':[{'type':'message','content':[{'type':'output_text','text':raw}]}],
             'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120}}
        return override(request,env,len(requests)) if override else httpx.Response(200,json=env)
    return KuaiAPIProvider(config,httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=True)),requests


def reconcile_paused(store,cfg,data,api,paused):
    call=paused['probe']['result']['calls'][0]
    record={'manifest_hash':paused['manifest']['manifest_hash'],'call_id':call['call_id'],
        'provider_response_id':call['raw_provider_response']['id'],'provider_group':cfg.provider_group,
        'model_id':cfg.model_id,'actual_charge_usd':call['cost_usd'],'evidence_sha256':'b'*64,
        'source':'owner-provided-private-record','key_group_binding_checked':True,
        'key_group_binding_evidence_sha256':'e'*64,'key_group_binding_source':'owner-verified-key-settings',
        'strict_output_and_usage_checked':True}
    return execute_pilot(store,cfg,data,provider=api,probe_reconciliation=record)


def complete_pilot(store,cfg,data,api):
    paused=execute_pilot(store,cfg,data,provider=api)
    assert paused['status']=='PAUSED_FOR_FIRST_CALL_RECONCILIATION'
    return reconcile_paused(store,cfg,data,api,paused)


@pytest.mark.parametrize('updates',[
    {'pricing_unit':'usd_per_million'},{'input_usd_per_million':1},
    {'balance_units_per_usd':True},{'balance_units_per_usd':0},
    {'price_source':'https://developers.openai.com/api/docs/pricing'},
    {'endpoint':'https://elsewhere.invalid/v1/responses'},{'price_evidence_sha256':'not-a-hash'}])
def test_gateway_rejects_ambiguous_or_exfiltrating_configuration(data,updates):
    with pytest.raises(ValidationError):config(data,**updates)


def test_provider_credentials_are_isolated(data,monkeypatch):
    cfg=config(data)
    monkeypatch.setenv('OPENAI_API_KEY','direct-fixture-key')
    monkeypatch.delenv('KUAIAPI_API_KEY',raising=False)
    with pytest.raises(ValueError):KuaiAPIProvider(cfg)
    monkeypatch.setenv('KUAIAPI_API_KEY','gateway-fixture-key')
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    with pytest.raises(ValueError):
        OpenAIProvider(model_id='fixture-2026-01-01',input_rate=1,output_rate=2)
    assert cfg.usd_rates()==(1,2)
    assert config(data,balance_units_per_usd=None).missing()
    assert 'gateway_routing_acknowledged' in config(data,gateway_routing_acknowledged=False).missing()


@pytest.mark.parametrize('usage,expected',[
    ({'input_tokens':100,'output_tokens':20},(100,20,True)),
    ({'prompt_tokens':100,'completion_tokens':20},(100,20,True)),
    ({'input_tokens':100,'output_tokens':20,'prompt_tokens':100,'completion_tokens':20},(100,20,True)),
    ({'input_tokens':100,'output_tokens':20,'prompt_tokens':101,'completion_tokens':20},(0,0,False)),
    ({'input_tokens':1,'output_tokens':2,'prompt_tokens':True,'completion_tokens':2},(0,0,False)),
    ({'input_tokens':100,'output_tokens':20,'total_tokens':121},(0,0,False)),
    ({'prompt_tokens':True,'completion_tokens':20},(0,0,False)),
    ({'input_tokens':-1,'output_tokens':20},(0,0,False)),
    ({'input_tokens':100},(0,0,False)),(None,(0,0,False)),
    ({'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':50},'output_tokens_details':{'reasoning_tokens':10}},(100,20,True))])
def test_gateway_usage_aliases_and_subsets_never_double_count(usage,expected):
    assert KuaiAPIProvider.usage(usage)==expected


def test_gateway_four_runs_normalization_attribution_and_replay(store,data,monkeypatch):
    cfg=config(data);api,requests=fixture(cfg,monkeypatch)
    report=complete_pilot(store,cfg,data,api)
    assert len(requests)==16
    assert [r['metrics']['llm_calls'] for r in report['runs']]==[0,0,4,12]
    assert report['budget']['usage_estimated_usd']==pytest.approx(16*.00014)
    assert all(e['equal'] and e['provider_calls']==0 for e in report['replay_equality'])
    for row in report['runs'][2:]:
        assert row['config']['model_provider']=='kuaiapi'
        assert row['result']['provider_attribution']['identity_scope']=='gateway-reported-label-only'
        for d in row['result']['decisions']:
            for call in d['calls']:
                assert call['provider_attribution']['group']=='fixture-group'
                assert call['pricing_attribution']['balance_units_per_usd']==6.8
                assert call['cost_usd']==pytest.approx(.00014)
    assert 'gateway-fixture-key' not in json.dumps(report) and 'direct-fixture-key' not in json.dumps(report)
    monkeypatch.setattr(KuaiAPIProvider,'reply',lambda *a:pytest.fail('Replay/idempotent rerun made a request'))
    again=execute_pilot(store,cfg,data,provider=api)
    assert report['runs'][2]['run_id']==again['runs'][2]['run_id']
    altered=make_manifest(config(data,provider_group='another-group'),data)
    assert altered['manifest_hash']!=report['manifest']['manifest_hash']


@pytest.mark.parametrize('retry',['429','503','timeout'])
def test_gateway_retry_unknown_reserves_shared_cap(store,data,monkeypatch,retry):
    cfg=config(data,total_budget_usd=.075)
    def response(request,env,n):
        if n==2:
            if retry=='timeout':raise httpx.ReadTimeout('fixture timeout')
            return httpx.Response(int(retry),json={'error':'fixture transient failure'})
        if n>2:env.pop('usage')
        env['debug']='gateway-fixture-key direct-fixture-key'
        return httpx.Response(200,json=env)
    api,requests=fixture(cfg,monkeypatch,response)
    report=complete_pilot(store,cfg,data,api)
    assert len(requests)==3
    assert report['budget']['unknown_reserved_usd']==pytest.approx(2*(32768+4096)/1e6)
    assert report['runs'][3]['metrics']['llm_calls']==0
    assert report['runs'][3]['result']['trades']==[]
    assert all(e['equal'] for e in report['replay_equality'])
    assert 'fixture-key' not in json.dumps(report)


@pytest.mark.parametrize('kind',['unsupported_schema','redirect','invalid','duplicate','nonfinite','model_mismatch','malformed_envelope'])
def test_gateway_contract_failures_skip_without_fallback_or_liquidation(data,monkeypatch,kind):
    cfg=config(data)
    def response(request,env,n):
        if kind=='unsupported_schema':return httpx.Response(400,json={'error':'unsupported json_schema'})
        if kind=='redirect':return httpx.Response(307,headers={'Location':'https://elsewhere.invalid/steal'})
        if kind=='malformed_envelope':return httpx.Response(200,text='gateway-fixture-key malformed envelope')
        if kind=='model_mismatch':env['model']='another-gateway-label'
        if kind in ('invalid','duplicate','nonfinite'):
            raw={'invalid':'not json','duplicate':'{"target_weight":0,"target_weight":1}',
                 'nonfinite':'{"target_weight":0,"confidence":NaN,"rationale":"x","evidence":[]}' }[kind]
            env['output'][0]['content'][0]['text']=raw
        return httpx.Response(200,json=env)
    api,requests=fixture(cfg,monkeypatch,response)
    exp=ExperimentConfig(strategy_id='single_agent',llm_mode='api',model_provider='kuaiapi',model_id=cfg.model_id)
    result=AgentWorkflow(exp,api).evaluate(snapshots(data)[date(2023,1,6)])
    assert result['decision_output'] is None and len(requests)==1
    assert result['calls'][0]['status']=='FAILED'
    assert result['reason_codes']
    if kind=='malformed_envelope':
        assert result['calls'][0]['cost_status']=='unknown_reserved'
        assert result['calls'][0]['raw_provider_response']['malformed_response_body']=='[REDACTED_API_KEY] malformed envelope'


def test_gateway_replay_checks_attribution_and_configuration_mutation(data,monkeypatch):
    from app.agents import replay_decision
    cfg=config(data);api,requests=fixture(cfg,monkeypatch)
    exp=ExperimentConfig(strategy_id='single_agent',llm_mode='api',model_provider='kuaiapi',model_id=cfg.model_id)
    workflow=AgentWorkflow(exp,api);snap=snapshots(data)[date(2023,1,6)]
    evaluated=workflow.evaluate(snap)
    original={'snapshot':snap.model_dump(mode='json'),'status':'APPROVED',**evaluated}
    original['calls'][0]['provider_attribution']['group']='tampered'
    assert replay_decision(snap,original,expected_provider=cfg.attribution())['reason_codes']==['REPLAY_CONTRACT_MISMATCH']
    api.attribution['group']='changed'
    assert workflow.evaluate(snap)['reason_codes']==['PROVIDER_CONFIGURATION_CHANGED']
    assert len(requests)==1


def test_provider_specific_hidden_key_file_flow(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('pilot_cli',Path(__file__).resolve().parents[2]/'tools/run_pilot.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    monkeypatch.setattr(cli,'ROOT',tmp_path)
    subprocess.run(['git','init',str(tmp_path)],capture_output=True,check=True)
    (tmp_path/'.gitignore').write_text('.env.*\nartifacts/\n',encoding='utf-8')
    monkeypatch.setattr(cli.getpass,'getpass',lambda prompt:'hidden-gateway-fixture')
    monkeypatch.setenv('KUAIAPI_API_KEY','prior-fixture-value')
    monkeypatch.setattr('sys.argv',['run_pilot.py','--set-key','--provider','kuaiapi'])
    cli.main()
    keyfile=tmp_path/'.env.pilot.kuaiapi'
    assert keyfile.read_text(encoding='utf-8')=='KUAIAPI_API_KEY=hidden-gateway-fixture\n'
    with pytest.raises(ValueError):cli.load_key(keyfile,'openai')
    cli.load_key(keyfile,'kuaiapi')
    assert cli.os.environ['KUAIAPI_API_KEY']=='hidden-gateway-fixture'
    with pytest.raises(ValueError):cli.main() # Never overwrite an existing key.


def test_gateway_skip_retains_position_and_valid_zero_liquidates(data,monkeypatch):
    from app.experiments import run_experiment
    from app.pilot import semantic_result
    cfg=config(data)
    def response(request,env,n):
        if n==2:return httpx.Response(400,json={'error':'fixture invalid schema'})
        output=json.loads(env['output'][0]['content'][0]['text'])
        output['target_weight']=1 if n==1 else 0
        env['output'][0]['content'][0]['text']=json.dumps(output)
        return httpx.Response(200,json=env)
    api,requests=fixture(cfg,monkeypatch,response)
    exp=ExperimentConfig(strategy_id='single_agent',llm_mode='api',model_provider='kuaiapi',model_id=cfg.model_id,
                         start_date='2023-01-01',end_date='2023-01-31')
    original=run_experiment(exp,data,provider=api)
    assert [t['side'] for t in original['trades']]==['BUY','SELL']
    assert original['decisions'][1]['status']=='SKIPPED'
    assert original['decisions'][2]['status']=='APPROVED' and original['decisions'][2]['approved_weight']==0
    assert next(p for p in original['equity'] if p['session_date']=='2023-01-17')['shares']>0
    assert original['equity'][-1]['shares']==0
    monkeypatch.setattr(KuaiAPIProvider,'reply',lambda *a:pytest.fail('Replay called gateway'))
    monkeypatch.setattr(OpenAIProvider,'reply',lambda *a:pytest.fail('Replay called direct API'))
    replay=run_experiment(exp.model_copy(update={'llm_mode':'replay'}),data,source=original)
    assert semantic_result(replay)==semantic_result(original) and replay['metrics']['llm_calls']==0


def test_gateway_generic_api_cannot_bypass_pilot_budget(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(tmp_path/'api.sqlite3')) as client:
        response=client.post('/api/v1/experiments',json={'strategy_id':'single_agent','llm_mode':'api',
            'model_provider':'kuaiapi','model_id':'gateway-label'})
        assert response.status_code==422 and 'bounded private pilot' in response.json()['detail']


@pytest.mark.parametrize('provider',['openai','kuaiapi'])
def test_endpoint_tampering_is_recorded_without_sending_credentials(data,monkeypatch,provider):
    cfg=config(data)
    monkeypatch.setenv('OPENAI_API_KEY','direct-fixture-key')
    monkeypatch.setenv('KUAIAPI_API_KEY','gateway-fixture-key')
    client=httpx.Client(transport=httpx.MockTransport(lambda request:pytest.fail('credential sent to a changed endpoint')))
    api=KuaiAPIProvider(cfg,client) if provider=='kuaiapi' else OpenAIProvider(client,model_id='fixture-2026-01-01',input_rate=1,output_rate=2)
    api.endpoint='https://elsewhere.invalid/v1/responses'
    exp=ExperimentConfig(strategy_id='single_agent',llm_mode='api',model_provider=provider,model_id=api.model_id)
    result=AgentWorkflow(exp,api).evaluate(snapshots(data)[date(2023,1,6)])
    assert result['reason_codes']==['PROVIDER_ENDPOINT_MISMATCH']
    assert result['calls'][0]['status']=='FAILED'
    assert result['calls'][0]['cost_status']=='unknown_reserved'


def test_key_setup_refuses_echo_fallback(tmp_path,monkeypatch):
    import warnings
    spec=importlib.util.spec_from_file_location('pilot_key_cli',Path(__file__).resolve().parents[2]/'tools/run_pilot.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    monkeypatch.setattr(cli,'ROOT',tmp_path)
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/'.gitignore').write_text('.env.*\nartifacts/\n',encoding='utf-8')
    def unhidden(prompt):warnings.warn('fixture non-TTY',cli.getpass.GetPassWarning)
    monkeypatch.setattr(cli.getpass,'getpass',unhidden)
    monkeypatch.setattr('sys.argv',['run_pilot.py','--set-key','--provider','kuaiapi'])
    with pytest.raises(cli.getpass.GetPassWarning):cli.main()
    assert not (tmp_path/'.env.pilot.kuaiapi').exists()
