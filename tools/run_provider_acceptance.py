"""Explicit six-request contract diagnostic; default is offline with no key.

--freeze performs documented readonly preflight only. --execute uses a clean
immutable freeze and an independent locked database. No retries or trade path.
This driver is not the research service and cannot enable a live study launch.
"""
import argparse
import copy
from datetime import datetime
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4, uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'tools')]
from app.agents import AgentFailure,AgentWorkflow,INSTRUCTIONS,SCHEMAS,OpenAIProvider
from app.data import canonical_hash
from app.gateway import KuaiAPIProvider
from app.models import ExperimentConfig,FeatureSnapshot,PROMPT_VERSION
from app.pilot import PilotBudget,PilotConfig
from app.pilot_billing import checked_manifest
from app.storage import Store
from app.http_policy import transport_policy
from check_provider_contract import inspect_call,SEQUENCE
from run_critic_ablation import capture_billing,check_billing,verify_tariff,billing_delta
from run_pilot import load_key,private_path


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def write_new(path,value):
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)


def prepare_manifest(proposal,source,config,group,catalogue,baseline,authorization,protocol_sha,source_sha):
    """Pure freeze builder: no provider construction, keys, network or trades."""
    cfg=config
    checked_manifest(source)
    if canonical_hash({k:v for k,v in proposal.items() if k!='proposal_hash'})!=proposal['proposal_hash']:
        raise ValueError('proposal content hash mismatch')
    if (proposal['sequence']!=list(SEQUENCE) or cfg.total_budget_usd!=.2 or cfg.max_calls!=6 or
        cfg.max_attempts_per_role!=1 or cfg.provider!='kuaiapi' or cfg.missing() or
        cfg.pricing_unit!='runtime_usd_per_million' or cfg.max_output_tokens!=2048):
        raise ValueError('independent six-attempt/no-retry USD0.20 contract required')
    if (cfg.dataset_hash!=proposal['dataset_hash'] or str(cfg.start_date)!='2023-01-01' or str(cfg.end_date)!='2023-01-31' or
        source['source_run_ids']!=proposal['source_run_ids'] or source['dataset_hash']!=proposal['dataset_hash']):
        raise ValueError('frozen source identity/period mismatch')
    if not authorization.get('exact_owner_reply') or authorization['approved_proposal_sha256']!=protocol_sha:
        raise ValueError('approved protocol bytes required')
    if source_sha!=proposal['source_ablation_manifest_sha256']:
        raise ValueError('immutable source manifest mismatch')
    if (group['local_project_key_match'] is not True or group['approved_group_label']!=cfg.provider_group or
        group['observed_group_label']!='C03 '+cfg.provider_group or cfg.model_id!=proposal['configuration']['model_id'] or
        cfg.provider_group!=proposal['configuration']['provider_group']):
        raise ValueError('approved model/group evidence mismatch')
    check_billing(baseline,cfg);verify_tariff(cfg,catalogue)
    history=proposal['historical_accounting_separate']
    if (baseline['total']['requests']!=history['last_saved_cumulative_key_requests'] or
        Decimal(str(baseline['total']['actual_cost']))!=Decimal(history['last_saved_cumulative_key_actual_usd'])):
        raise ValueError('unexpected intervening key usage; historical accounting is not reset')
    prompt={'instructions':INSTRUCTIONS['decision'],'schema':SCHEMAS['decision'].model_json_schema(),'version':PROMPT_VERSION}
    if source['prompt']!=prompt or proposal['prompt']!=prompt or cfg.provider_settings()!=proposal['configuration']['provider_settings']:
        raise ValueError('source prompt/schema/settings drift')
    originals=dict(zip(('A','B'),source['observations'][:2]));inputs=proposal['inputs']
    for label in ('A','B'):
        original=originals[label];item=inputs[label]
        if (original['decision']!='2023-01-06' or original['input']!=item['input'] or
            canonical_hash(item['input'])!=item['input_hash'] or original['input_hash']!=item['input_hash'] or
            original['analysis_hash']!=item['analysis_hash'] or original['critic_hash']!=item['critic_hash'] or
            original['saved_analysis_call']!=item['saved_analysis_call'] or
            (label=='B' and original['saved_critic_call']!=item['saved_critic_call'])):
            raise ValueError('saved predecessor/input alignment mismatch')
        snap=FeatureSnapshot.model_validate(original['snapshot'])
        if snap.numeric_input()!=item['input']['features'] or snap.dataset_hash!=proposal['dataset_hash']:
            raise ValueError('immutable snapshot mismatch')
        for stage in ('analysis','critic'):
            if stage in item['input']:
                SCHEMAS[stage].model_validate(item['input'][stage])
                saved=item['saved_'+stage+'_call']
                if saved['status']!='VALIDATED' or saved['validated_output']!=item['input'][stage]:
                    raise ValueError('only saved validated predecessors allowed')
        request=OpenAIProvider.payload(SimpleNamespace(model_id=cfg.model_id,settings=cfg.provider_settings()),'decision',item['input'],prompt['schema'])
        if request!=item['request'] or canonical_hash(request)!=item['request_hash'] or len(json.dumps(request).encode())+4096>32768:
            raise ValueError('frozen wire request drift')
    if (set(inputs['A']['input'])!={'features','analysis'} or set(inputs['B']['input'])!={'features','analysis','critic'} or
        inputs['A']['input']!={k:v for k,v in inputs['B']['input'].items() if k!='critic'}):
        raise ValueError('Critic inclusion must be the only condition difference')
    reserve=(32768*Decimal(str(cfg.usd_rates()[0]))+2048*Decimal(str(cfg.usd_rates()[1])))/1000000
    if reserve!=Decimal(proposal['pricing']['per_attempt_reserve_usd']) or reserve*6>Decimal('.2'):
        raise ValueError('approved conservative reservation drift')
    identity=str(uuid4())
    manifest={'kind':'provider-contract-acceptance-v1','experiment_id':identity,'configuration':cfg.model_dump(mode='json'),
        'proposal_hash':proposal['proposal_hash'],'approved_protocol_sha256':protocol_sha,'authorization':authorization,
        'source_manifest_sha256':source_sha,'source_run_ids':proposal['source_run_ids'],'dataset_hash':proposal['dataset_hash'],
        'source_manifest_hash':source['manifest_hash'],'prompt':prompt,'prompt_hash':proposal['prompt_hash'],
        'schema_hash':proposal['schema_hash'],'provider_settings':cfg.provider_settings(),'provider_attribution':cfg.attribution(),
        'transport_timeout_policy':cfg.timeout_policy.model_dump(),
        'group_binding':group,'group_binding_scope':'historical authenticated masked-key match and unique project label; current readonly multiplier corroboration, not upstream proof',
        'catalogue_hash':canonical_hash(catalogue),'billing_baseline':baseline,'sequence':list(SEQUENCE),
        'reservation_per_attempt_usd':float(reserve),'maximum_reserved_usd':str(reserve*6),
        'observations':[{'slot':i+1,'slot_id':str(uuid5(NAMESPACE_URL,identity+':'+str(i+1))),
            'condition':label,'input':copy.deepcopy(inputs[label]),'snapshot':copy.deepcopy(originals[label]['snapshot'])}
            for i,label in enumerate(SEQUENCE)],
        'collection_policy':'known-charge format failures collect through six; no retries; hard stops preserve unknown liability',
        'no_trade_path':True}
    manifest['manifest_hash']=canonical_hash(manifest)
    return manifest


def validate_database(path,manifest):
    if not path.exists():return
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as conn:
        tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:return
        if 'contract_meta' not in tables:raise ValueError('refusing original or unrelated database before initialization')
        row=conn.execute('SELECT payload FROM contract_meta WHERE id=?',('manifest',)).fetchone()
        if not row or json.loads(row[0])!=manifest:raise ValueError('independent database identity mismatch')


class LiabilityHold:
    """Workflow may estimate costs; only verified readonly billing releases hold."""
    def __init__(self,budget,reserve):self.budget,self.amount=budget,reserve
    def reserve(self,call_id,amount):
        if abs(amount-self.amount)>1e-12:raise AgentFailure('RESERVATION_DRIFT')
        self.budget.reserve(call_id,amount)
    def settle(self,call_id,estimate):pass
    def over_limit(self,call_id,estimate):return self.budget.over_limit(call_id,estimate)


def execute(store,manifest,config,provider,readonly):
    """Single locked worker. All uncertain/reserved attempts are never resent."""
    checked_manifest(manifest)
    if (manifest['kind']!='provider-contract-acceptance-v1' or manifest['configuration']!=config.model_dump(mode='json') or
        config.total_budget_usd!=.2 or config.max_calls!=6 or config.max_attempts_per_role!=1 or
        [o['condition'] for o in manifest['observations']]!=list(SEQUENCE) or
        len({o['slot_id'] for o in manifest['observations']})!=6):
        raise ValueError('independent approved six-slot experiment required')
    with store.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='contract_meta'").fetchone():
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_budgets'").fetchone():
                raise ValueError('original accounting cannot be reused')
        conn.execute('CREATE TABLE IF NOT EXISTS contract_meta (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS contract_slots (slot INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS contract_billing (sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO contract_meta VALUES (?,?)',('manifest',json.dumps(manifest)))
        if json.loads(conn.execute('SELECT payload FROM contract_meta WHERE id=?',('manifest',)).fetchone()[0])!=manifest:
            raise ValueError('immutable execution manifest mismatch')
        for obs in manifest['observations']:
            conn.execute('INSERT OR IGNORE INTO contract_slots VALUES (?,?)',(obs['slot'],json.dumps({**obs,'status':'NOT_RUN','calls':[]})))
    budget=PilotBudget(store,manifest)
    def records():
        with store.connect() as conn:return [json.loads(r[0]) for r in conn.execute('SELECT payload FROM contract_slots ORDER BY slot')]
    def put(obs):
        with store.connect() as conn:conn.execute('UPDATE contract_slots SET payload=? WHERE slot=?',(json.dumps(obs,allow_nan=False),obs['slot']))
    def halt(code):
        with store.connect() as conn:conn.execute('INSERT OR IGNORE INTO contract_meta VALUES (?,?)',('halt',json.dumps(code)))
    def halted():
        with store.connect() as conn:return conn.execute('SELECT payload FROM contract_meta WHERE id=?',('halt',)).fetchone()
    def checkpoint():
        calls=[c for obs in records() for c in obs['calls']]
        with store.connect() as conn:
            durable={r[0] for r in conn.execute('SELECT call_id FROM pilot_attempts WHERE pilot_id=?',(budget.id,))}
        if durable!={c['call_id'] for c in calls}:raise ValueError('orphan reservation; no resend')
        for poll in range(4):
            current=readonly()
            try:receipt=billing_delta(manifest,config,current,calls)
            except Exception as exc:
                with store.connect() as conn:conn.execute('INSERT INTO contract_billing(payload) VALUES (?)',(json.dumps({'status':'FAILED','readonly':current,'reason':'DRIFT_OR_INCOMPLETE_DELTA'}),))
                lag=(isinstance(exc,ValueError) and str(exc)=='billing request/token delta differs from experiment calls' and
                    all(current[k]['requests']-manifest['billing_baseline'][k]['requests']<=len(calls) for k in ('total','today')))
                if lag and poll<3:time.sleep(.5);continue
                raise
            receipt['pilot_id']=budget.id
            # Atomic actual reconciliation + estimate release; unknown holds survive failures.
            with store.connect() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS pilot_usage_reconciliations (pilot_id TEXT PRIMARY KEY,receipt_json TEXT NOT NULL)')
                conn.execute('INSERT INTO pilot_usage_reconciliations VALUES (?,?) ON CONFLICT(pilot_id) DO UPDATE SET receipt_json=excluded.receipt_json',(budget.id,json.dumps(receipt)))
                for call in calls:conn.execute("UPDATE pilot_attempts SET usage_estimate=?,status='usage_estimated' WHERE call_id=? AND pilot_id=?",(str(call['cost_usd']),call['call_id'],budget.id))
                conn.execute('INSERT INTO contract_billing(payload) VALUES (?)',(json.dumps({'status':'VERIFIED','readonly':current,'receipt':receipt}),))
            return receipt
    if not halted():
        try:
            for obs in records():
                if obs['status'] not in ('NOT_RUN','SETTLED'):
                    obs['status']='UNCERTAIN_INTERRUPTED';put(obs);raise ValueError('uncertain request is never resent')
            checkpoint()
            for obs in records():
                if obs['status']=='SETTLED':continue
                if (provider.provider_id!=config.provider or provider.endpoint!='https://kuaiapi.net/v1/responses' or
                    provider.model_id!=config.model_id or provider.settings!=manifest['provider_settings'] or
                    provider.attribution!=manifest['provider_attribution'] or (provider.input_rate,provider.output_rate)!=config.usd_rates() or
                    transport_policy(provider)!=manifest['transport_timeout_policy']):
                    raise ValueError('provider endpoint/settings/pricing drift')
                obs.update(status='RUNNING',calls=[]);put(obs)
                calls=[]
                def save_call(call):
                    call.update(diagnostic_slot_id=obs['slot_id'],diagnostic_condition=obs['condition'],experiment_id=manifest['experiment_id'])
                    obs['calls']=copy.deepcopy(calls);put(obs)
                workflow=AgentWorkflow(ExperimentConfig(strategy_id='single_agent',llm_mode='api',model_provider=config.provider,
                    model_id=config.model_id,max_llm_calls=6,max_llm_budget_usd=.2,max_attempts_per_role=1),provider,
                    LiabilityHold(budget,manifest['reservation_per_attempt_usd']))
                try:workflow._call('decision',obs['input']['input'],calls,FeatureSnapshot.model_validate(obs['snapshot']),save_call)
                except AgentFailure:pass # Inspect and bill known-cost format failures; no retry.
                if len(calls)!=1:raise ValueError('exactly one request per slot required')
                call=calls[0];check=inspect_call(call,config.model_id)
                if (call['input_hash']!=obs['input']['input_hash'] or call['prompt_hash']!=manifest['prompt_hash'] or
                    call['schema_hash']!=manifest['schema_hash']):check['hard_stop_codes'].append('FROZEN_TRACE_MISMATCH')
                response_id=check['provider_response_id']
                if response_id and any(response_id==o.get('inspection',{}).get('provider_response_id') for o in records() if o['slot']!=obs['slot']):
                    check['hard_stop_codes'].append('DUPLICATE_PROVIDER_RESPONSE_ID')
                obs.update(status='AWAITING_RECONCILIATION',calls=copy.deepcopy(calls),inspection=check);put(obs)
                checkpoint()
                obs.update(status='SETTLED');put(obs)
                if check['hard_stop_codes']:raise ValueError('output transport/security/accounting hard stop')
        except BaseException as exc:
            for obs in records():
                if obs['status']=='RUNNING':obs['status']='UNCERTAIN_INTERRUPTED';put(obs)
            halt('INTERRUPTED' if not isinstance(exc,Exception) else 'PROTOCOL_HARD_STOP_OR_INCOMPLETE_BILLING')
            if not isinstance(exc,Exception):raise
    observations=records()
    with store.connect() as conn:checks=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM contract_billing ORDER BY sequence')]
    valid=sum(o.get('inspection',{}).get('format_pass',False) for o in observations)
    return {'status':'STOPPED' if halted() else 'COMPLETED','experiment_id':manifest['experiment_id'],'manifest_hash':manifest['manifest_hash'],
        'observations':observations,'billing_checks':checks,'budget':budget.summary(),'valid_outputs':valid,
        'provisional_acceptance':not halted() and valid==6 and all(o['status']=='SETTLED' for o in observations),
        'acceptance_scope':'six conditioned-input observations only; no universal reliability/upstream identity or trading benefit proof',
        'halt_reason':json.loads(halted()[0]) if halted() else None,'no_trade_path':True}


def code_snapshot():
    names=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    return {n:sha(ROOT/n) for n in names if n}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--freeze',action='store_true',help='Readonly preflight and new immutable execution freeze; no inference')
    actions.add_argument('--execute',action='store_true',help='Explicit approved frozen six-request diagnostic')
    for name in ('proposal-dir','authorization','source-manifest','group-evidence','config','workdir','key-file'):
        parser.add_argument('--'+name,type=Path)
    args=parser.parse_args()
    if not (args.freeze or args.execute):
        print(json.dumps({'offline':True,'new_provider_calls':0,'key_loaded':False,'execute':False}));return
    if any(getattr(args,n) is None for n in ('proposal_dir','authorization','source_manifest','group_evidence','config','workdir','key_file')):
        raise ValueError('explicit approved private inputs and destinations required')
    work=private_path(args.workdir);work.mkdir(parents=True,exist_ok=True)
    frozen=work/'execution-manifest.json';protocol=args.proposal_dir/'PROTOCOL_PROPOSAL.md'
    proposal=load(args.proposal_dir/'DRY_RUN_MANIFEST.json');proof=load(args.proposal_dir/'IMPLEMENTATION_FREEZE.json')
    if (sha(protocol)!=load(args.authorization)['approved_proposal_sha256'] or
        sha(args.proposal_dir/'DRY_RUN_MANIFEST.json')!=proof['proposal_file_sha256'] or proposal['proposal_hash']!=proof['proposal_hash']):
        raise ValueError('approved proposal bytes changed')
    config=PilotConfig.model_validate_json(args.config.read_bytes())
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT).strip():raise ValueError('clean committed source required')
    if args.freeze and frozen.exists():raise ValueError('freeze exists; cannot reset experiment identity')
    if args.execute:
        manifest=load(frozen)
        checked_manifest(manifest)
        if (manifest['configuration']!=config.model_dump(mode='json') or manifest['source_files']!=code_snapshot() or
            manifest['code_commit']!=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip() or
            manifest['approved_protocol_sha256']!=sha(protocol) or manifest['authorization_file_sha256']!=sha(args.authorization) or
            manifest['source_manifest_sha256']!=sha(args.source_manifest) or manifest['group_evidence_file_sha256']!=sha(args.group_evidence)):
            raise ValueError('frozen source/config changed')
        validate_database(work/'diagnostic.sqlite3',manifest)
    load_key(args.key_file,'kuaiapi');key=os.environ['KUAIAPI_API_KEY']
    if args.freeze:
        import httpx
        response=httpx.get('https://kuaiapi.net/model-plaza/data/plaza.json',follow_redirects=False,timeout=30);response.raise_for_status()
        catalogue=response.json();day=datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
        baseline=capture_billing(key,day)
        manifest=prepare_manifest(proposal,load(args.source_manifest),config,load(args.group_evidence),catalogue,baseline,
            load(args.authorization),sha(protocol),sha(args.source_manifest))
        manifest.pop('manifest_hash')
        manifest.update(code_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),source_files=code_snapshot(),
            catalogue_bytes_sha256=hashlib.sha256(response.content).hexdigest(),authorization_file_sha256=sha(args.authorization),
            group_evidence_file_sha256=sha(args.group_evidence),frozen_at_utc=datetime.now(ZoneInfo('UTC')).isoformat(),
            dependency_versions={name:importlib.metadata.version(name) for name in ('httpx','pydantic','langgraph')})
        manifest['manifest_hash']=canonical_hash(manifest)
        write_new(frozen,manifest)
        write_new(work/'preflight-catalogue.json',catalogue)
        print(json.dumps({'status':'FROZEN_ZERO_INFERENCE','historical_key_requests':baseline['total']['requests'],
            'historical_key_actual_usd':baseline['total']['actual_cost'],'six_attempt_ceiling_usd':manifest['maximum_reserved_usd']}));return
    store=Store(work/'diagnostic.sqlite3');store.initialize()
    try:
        result=execute(store,manifest,config,KuaiAPIProvider(config),lambda:capture_billing(key,manifest['billing_baseline']['day']))
        with (work/'result.json').open('w',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
        print(json.dumps({'status':result['status'],'valid_outputs':result['valid_outputs'],'provisional_acceptance':result['provisional_acceptance'],'budget':result['budget']}))
    finally:store.close()


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('Diagnostic interrupted; durable holds retained, no automatic resend.',file=sys.stderr);sys.exit(130)
    except Exception as exc:
        print('Diagnostic stopped ('+type(exc).__name__+'); private evidence retained; exception values omitted.',file=sys.stderr);sys.exit(1)
