"""Private bounded saved-predecessor ablation; no inference unless --execute.

Eight Decision observations, independent durable budget, counterbalanced order.
No Analysis/Critic requests, general queue, tuning, or public artifact creation.
"""
import argparse
import copy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import UUID, NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'tools')]
from app.agents import AgentFailure, AgentWorkflow, INSTRUCTIONS, SCHEMAS
from app.backtest import assess_risk
from app.data import DatasetRegistry,canonical_hash
from app.experiments import run_experiment
from app.features import snapshots
from app.gateway import KuaiAPIProvider
from app.models import AnalysisOutput,CriticOutput,DecisionOutput,ExperimentConfig,FeatureSnapshot,PROMPT_VERSION
from app.pilot import PilotBudget,PilotConfig,semantic_result
from app.storage import Store
from app.http_policy import transport_policy
from analyze_decision_difference import analyze
from run_pilot import load_key,private_path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_billing(key,day,client=None):
    """Only public-UI-documented key endpoints; discard account/plan/balance."""
    def collect(client):
        kw={'headers':{'Authorization':'Bearer '+key},'timeout':30,'follow_redirects':False}
        response=client.get('https://kuaiapi.net/v1/sub2api/billing',**kw);response.raise_for_status();billing=response.json()
        response=client.get('https://kuaiapi.net/v1/usage',params={'start_date':day,'end_date':day,'days':1,'timezone':'Asia/Taipei'},**kw)
        response.raise_for_status();usage=response.json()
        fields={'date','model','requests','input_tokens','output_tokens','total_tokens','cache_creation_tokens','cache_write_tokens','cache_read_tokens','cost','actual_cost'}
        safe=lambda row:{k:v for k,v in row.items() if k in fields}
        result={'day':day,'unit':usage['unit'],'total':safe(usage['usage']['total']),'today':safe(usage['usage']['today']),
            'daily':[safe(r) for r in usage['daily_usage']],'models':[safe(r) for r in usage['model_stats']],
            'billing':{k:v for k,v in billing.items() if k in {'object','billing_scope','group_rate_multiplier','resolved_rate_multiplier','effective_rate_multiplier','peak_rate_enabled'}},
            'observed_at_utc':datetime.now(ZoneInfo('UTC')).isoformat()}
        return json.loads(json.dumps(result).replace(key,'[REDACTED_API_KEY]'))
    if client is not None:
        return collect(client)
    with httpx.Client() as session:
        return collect(session)


def check_billing(snapshot,config):
    billing=snapshot['billing']
    if (snapshot['unit']!='USD' or billing['object']!='sub2api.key_billing' or
        billing['billing_scope']!='token' or billing['peak_rate_enabled'] is not False or
        any(type(billing[k]) not in (int,float) or Decimal(str(billing[k]))!=Decimal(str(config.runtime_rate_multiplier))
            for k in ('group_rate_multiplier','resolved_rate_multiplier','effective_rate_multiplier'))):
        raise ValueError('runtime pricing scope or multiplier drift')


def verify_tariff(config,catalogue):
    matches=[]
    def visit(value):
        if isinstance(value,dict):
            sell=value.get('token_pricing',{}).get('sell',{})
            if value.get('name')==config.model_id and sell.get('multiplier')==.15:
                matches.append(value)
            else:
                for item in value.values():visit(item)
        elif isinstance(value,list):
            for item in value:visit(item)
    visit(catalogue)
    if len(matches)!=1 or matches[0]['pricing']['billing_mode']!='token' or matches[0]['pricing']['per_request_price'] is not None:
        raise ValueError('selected published token tariff unavailable or changed')
    sell=matches[0]['token_pricing']['sell']
    for raw,field in [('input_price','input_usd_per_million'),('output_price','output_usd_per_million'),
                      ('cache_read_price','cache_read_usd_per_million'),('cache_write_price','cache_write_usd_per_million')]:
        effective=Decimal(str(sell['pricing'][raw]))/Decimal(str(sell['multiplier']))*1000000*Decimal(str(config.runtime_rate_multiplier))
        if effective!=Decimal(str(getattr(config,field))):
            raise ValueError('published tariff and frozen effective USD prices differ')


def make_manifest(config,source,group,catalogue,baseline,dataset):
    if (config.provider!='kuaiapi' or config.pricing_unit!='runtime_usd_per_million' or config.missing() or
        config.total_budget_usd!=.5 or config.max_calls!=16 or config.max_attempts_per_role!=2):
        raise ValueError('explicit independent USD0.50/16attempt/2perDecision runtime configuration required')
    check_billing(baseline,config);verify_tariff(config,catalogue)
    if (group['local_project_key_match'] is not True or group['approved_group_label']!=config.provider_group or
        group['observed_group_label']!='C03 '+config.provider_group):
        raise ValueError('approved group and authenticated UI evidence mismatch')
    pair=[next(r for r in source['runs'] if r['config']['strategy_id']==s) for s in ('single_agent','multi_agent')]
    audit=analyze(*pair)
    multi=pair[1]
    if (dataset.hash!=config.dataset_hash or config.dataset_hash!=audit['dataset_hash'] or
        str(config.start_date)!='2023-01-01' or str(config.end_date)!='2023-01-31' or
        config.model_id!=multi['config']['model_id'] or config.provider_settings()!=multi['result']['provider_settings'] or
        config.attribution()!=multi['result']['provider_attribution'] or
        any(getattr(config,k)!=multi['config'][k] for k in ('initial_cash','fee_bps','slippage_bps'))):
        raise ValueError('source dataset/model/group/settings/period mismatch')
    current=snapshots(dataset);observations=[]
    from datetime import date
    decisions=sorted(multi['result']['decisions'],key=lambda d:d['decision_session'])
    if [d['decision_session'] for d in decisions]!=['2023-01-06','2023-01-13','2023-01-20','2023-01-27']:
        raise ValueError('exact four source decisions required')
    for index,decision in enumerate(decisions):
        snap=FeatureSnapshot.model_validate(decision['snapshot'])
        if current[date.fromisoformat(decision['decision_session'])].numeric_input()!=snap.numeric_input():
            raise ValueError('immutable saved/current feature mismatch')
        analysis=AnalysisOutput.model_validate(decision['analysis_output']).model_dump(mode='json')
        critic=CriticOutput.model_validate(decision['critic_output']).model_dump(mode='json')
        order=('without_critic','with_critic') if index%2==0 else ('with_critic','without_critic')
        saved={c['stage']:c for c in decision['calls'] if c['status']=='VALIDATED'}
        for condition in order:
            payload={'features':snap.numeric_input(),'analysis':analysis}
            if condition=='with_critic':payload['critic']=critic
            identity=canonical_hash({'source':multi['run_id'],'day':decision['decision_session'],'condition':condition,'input':payload})
            observations.append({'observation_id':identity,'sequence':len(observations)+1,'condition':condition,
                'decision':decision['decision_session'],'execution':decision['execution_session'],'snapshot':decision['snapshot'],
                'input':payload,'input_hash':canonical_hash(payload),'analysis_hash':canonical_hash(analysis),
                'critic_hash':canonical_hash(critic) if condition=='with_critic' else None,
                'saved_analysis_call':saved['analysis'],'saved_critic_call':saved['critic'] if condition=='with_critic' else None})
    paths=[*sorted((ROOT/'backend/app').glob('*.py')),ROOT/'tools/run_critic_ablation.py',ROOT/'tools/run_pilot.py',
           ROOT/'tools/analyze_decision_difference.py',ROOT/'uv.lock',ROOT/'frontend/package-lock.json']
    source_hash=canonical_hash({p.relative_to(ROOT).as_posix():sha(p) for p in paths})
    manifest={'kind':'critic-ablation-v1','configuration':config.model_dump(mode='json'),
        'source_run_ids':audit['source_ids'],'source_export_hash':canonical_hash(source),'dataset_hash':dataset.hash,
        'group_binding':group,'catalogue_hash':canonical_hash(catalogue),'billing_baseline':baseline,
        'group_binding_scope':'unique project row, masked prefix6/suffix4; operational evidence, not cryptographic upstream proof',
        'prompt':{'instructions':INSTRUCTIONS['decision'],'schema':SCHEMAS['decision'].model_json_schema(),'version':PROMPT_VERSION},
        'prompt_hash':canonical_hash({'version':PROMPT_VERSION,'instructions':INSTRUCTIONS['decision'],'schema':SCHEMAS['decision'].model_json_schema()}),
        'schema_hash':canonical_hash(SCHEMAS['decision'].model_json_schema()),
        'provider_settings':config.provider_settings(),'provider_attribution':config.attribution(),
        'transport_timeout_policy':config.timeout_policy.model_dump(),
        'source_hash':source_hash,'code_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
        'code_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT).strip()),
        'observations':observations,'estimate':{'expected_new_decisions':8,'maximum_attempts':16,'max_attempts_per_observation':2,
        'new_analysis_calls':0,'new_critic_calls':0,'separate_smoke_calls':0,'input_bound':32768,'output_bound':config.max_output_tokens,
        'reserved_usd_per_attempt':(32768*config.usd_rates()[0]+config.max_output_tokens*config.usd_rates()[1])/1e6,
        'whole_attempt_ceiling_usd':16*(32768*config.usd_rates()[0]+config.max_output_tokens*config.usd_rates()[1])/1e6},
        'stop_policy':'stop on drift, unknown usage/fees, invalid schema/provider failure, budget exhaustion or billing mismatch; resume never repeats saved/uncertain calls',
        'readonly_usage_lag_policy':'up to4readonly polls spaced0.5seconds only when known usage counters lag; no inference retry for telemetry lag',
        'cost_scope':'new experiment only; before/after key usage delta, never old pilot leftover or per-call invoice allocation',
        'cache_scope':'published derived tariff; full input plus full cache-write reserved; cache deductions not presumed observed'}
    manifest['manifest_hash']=canonical_hash(manifest)
    return manifest


def validate_database(path,manifest):
    if not path.exists():return
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as conn:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:return
        if 'ablation_meta' not in tables:
            raise ValueError('refusing existing non-ablation database before initialization')
        row=conn.execute('SELECT payload FROM ablation_meta WHERE id=?',('manifest',)).fetchone()
        if not row or json.loads(row[0])!=manifest:raise ValueError('ablation database manifest mismatch')


def billing_delta(manifest,config,current,calls):
    check_billing(current,config);baseline=manifest['billing_baseline']
    if current['day']!=baseline['day'] or any(c.get('cost_status')!='known' for c in calls):
        raise ValueError('unknown charges or billing day changed')
    expected={'requests':len(calls),'input_tokens':sum(c['input_tokens'] for c in calls),'output_tokens':sum(c['output_tokens'] for c in calls)}
    pairs=[(baseline[k],current[k]) for k in ('total','today')]
    for key in ('models','daily'):
        field='model' if key=='models' else 'date'
        a={r[field]:r for r in baseline[key]};b={r[field]:r for r in current[key]}
        if a.keys()!=b.keys():raise ValueError('ambiguous model/day billing coverage')
        selected=config.model_id if field=='model' else baseline['day']
        for identity in a:
            if identity!=selected and a[identity]!=b[identity]:raise ValueError('unrelated key usage changed')
        pairs.append((a[selected],b[selected]))
    amounts=[]
    for before,after in pairs:
        if any(type(after.get(k)) is not int or type(before.get(k)) is not int or after[k]-before[k]!=v for k,v in expected.items()):
            raise ValueError('billing request/token delta differs from experiment calls')
        amount=Decimal(str(after['actual_cost']))-Decimal(str(before['actual_cost']))
        if amount<0:raise ValueError('negative actual delta')
        amounts.append(amount)
    if max(amounts)-min(amounts)>Decimal('0.0000000001'):
        raise ValueError('inconsistent actual USD delta scopes')
    amount=amounts[0]
    if amount>Decimal(str(config.total_budget_usd)) or amount>sum((Decimal(str(c['reserved_usd'])) for c in calls),Decimal(0))+Decimal('0.0000000001'):
        raise ValueError('actual delta exceeds experiment reservation or budget')
    no_cache=all(all(after.get(k,0)-before.get(k,0)==0 for k in ('cache_read_tokens','cache_write_tokens','cache_creation_tokens')) for before,after in pairs)
    if no_cache:
        expected_cost=(Decimal(expected['input_tokens'])*Decimal(str(config.input_usd_per_million))+Decimal(expected['output_tokens'])*Decimal(str(config.output_usd_per_million)))/1000000
        if abs(amount-expected_cost)>Decimal('0.0000000001'):raise ValueError('observed uncached USD tariff drift')
    return {'covered_call_ids':sorted(c['call_id'] for c in calls),'actual_charge_usd':str(amount),'unit':'USD',**expected,
        'scope':'new experiment cumulative key-usage delta; no per-call invoices allocated','observed_cache_delta_zero':no_cache,
        'baseline_hash':canonical_hash(baseline),'current_readonly_hash':canonical_hash(current),'invoiced_actual_usd':None}


def execute(store,manifest,config,dataset,provider,readonly):
    with store.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ablation_meta'").fetchone():
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_budgets'").fetchone():
                raise ValueError('refusing to mix original pilot accounting')
        conn.execute('CREATE TABLE IF NOT EXISTS ablation_meta (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS ablation_observations (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS ablation_billing_checks (sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO ablation_meta VALUES (?,?)',('manifest',json.dumps(manifest)))
        if json.loads(conn.execute('SELECT payload FROM ablation_meta WHERE id=?',('manifest',)).fetchone()[0])!=manifest:
            raise ValueError('immutable ablation manifest mismatch')
    budget=PilotBudget(store,manifest)
    def put(observation):
        with store.connect() as conn:
            conn.execute('INSERT INTO ablation_observations VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                         (observation['observation_id'],json.dumps(observation,allow_nan=False)))
    def halt(reason):
        with store.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO ablation_meta VALUES (?,?)',('halt',json.dumps(reason)))
    def get_all():
        with store.connect() as conn:
            records={r[0]:json.loads(r[1]) for r in conn.execute('SELECT id,payload FROM ablation_observations')}
        return [records.get(o['observation_id'],{**o,'status':'NOT_RUN','calls':[],'decision_output':None,
            'risk':{'status':'SKIPPED','approved_weight':None,'reason_codes':['NOT_RUN']}}) for o in manifest['observations']]
    def checkpoint(poll=0):
        calls=[c for o in get_all() for c in o['calls']]
        with store.connect() as conn:
            durable={r[0] for r in conn.execute('SELECT call_id FROM pilot_attempts WHERE pilot_id=?',(budget.id,))}
        if durable!={c['call_id'] for c in calls}:raise ValueError('orphan reservation retained; billing incomplete')
        current=readonly()
        with store.connect() as conn:
            index=conn.execute('INSERT INTO ablation_billing_checks(payload) VALUES (?)',
                (json.dumps({'readonly':current,'status':'CHECKING'}),)).lastrowid
        try:
            receipt=billing_delta(manifest,config,current,calls)
        except Exception as exc:
            with store.connect() as conn:
                conn.execute('UPDATE ablation_billing_checks SET payload=? WHERE sequence=?',
                    (json.dumps({'readonly':current,'status':'FAILED','reason':'DRIFT_OR_UNKNOWN_USAGE_OR_DELTA_MISMATCH'}),index))
            expected=len(calls)
            lag=(isinstance(exc,ValueError) and str(exc)=='billing request/token delta differs from experiment calls' and
                 all(current[k]['requests']-manifest['billing_baseline'][k]['requests']<=expected for k in ('total','today')))
            if lag and poll<3:
                time.sleep(.5)
                return checkpoint(poll+1)
            raise
        receipt['pilot_id']=manifest['manifest_hash']
        with store.connect() as conn:
            conn.execute('UPDATE ablation_billing_checks SET payload=? WHERE sequence=?',
                (json.dumps({'readonly':current,'status':'VERIFIED','receipt':receipt}),index))
            conn.execute('CREATE TABLE IF NOT EXISTS pilot_usage_reconciliations (pilot_id TEXT PRIMARY KEY,receipt_json TEXT NOT NULL)')
            conn.execute('INSERT INTO pilot_usage_reconciliations VALUES (?,?) ON CONFLICT(pilot_id) DO UPDATE SET receipt_json=excluded.receipt_json',
                         (budget.id,json.dumps(receipt)))
        return receipt
    with store.connect() as conn:
        stopped=conn.execute('SELECT payload FROM ablation_meta WHERE id=?',('halt',)).fetchone()
    if not stopped:
        try:
            # Recover a fully persisted successful reply; never resend RUNNING/uncertain work.
            for observation in get_all():
                if observation['status']=='RUNNING':
                    tail=observation['calls'][-1] if observation['calls'] else None
                    if tail and tail['status']=='VALIDATED' and tail['cost_status']=='known':
                        observation.update(status='COMPLETED',decision_output=tail['validated_output'],risk=assess_risk(DecisionOutput.model_validate(tail['validated_output'])))
                        put(observation)
                    else:
                        observation.update(status='INTERRUPTED',decision_output=None,risk={'status':'SKIPPED','approved_weight':None,'reason_codes':['INTERRUPTED_UNKNOWN']})
                        put(observation);raise ValueError('uncertain prior attempt requires review; not resent')
            checkpoint()
            for observation in get_all():
                if observation['status']=='COMPLETED':continue
                if (provider.settings!=config.provider_settings() or provider.attribution!=config.attribution() or
                    provider.model_id!=config.model_id or (provider.input_rate,provider.output_rate)!=config.usd_rates() or
                    transport_policy(provider)!=config.timeout_policy.model_dump()):
                    raise ValueError('provider identity/settings drift')
                observation.update(status='RUNNING',calls=[],decision_output=None);put(observation)
                calls=[]
                def on_call(call):
                    call.update(observation_id=observation['observation_id'],condition=observation['condition'],experiment_id=budget.id)
                    observation['calls']=copy.deepcopy(calls);put(observation)
                exp=ExperimentConfig(strategy_id='single_agent',llm_mode='api',model_provider=config.provider,model_id=config.model_id,
                    max_llm_calls=16,max_llm_budget_usd=.5,max_attempts_per_role=2)
                try:
                    decision=AgentWorkflow(exp,provider,budget)._call('decision',observation['input'],calls,
                        FeatureSnapshot.model_validate(observation['snapshot']),on_call)
                    if any(c['raw_provider_response'].get('service_tier') not in ('default','standard') for c in calls if c['status']=='VALIDATED'):
                        raise AgentFailure('SERVICE_TIER_DRIFT')
                    if any(c['input_tokens']>32768 or c['output_tokens']>config.max_output_tokens for c in calls):
                        raise AgentFailure('USAGE_BOUND_MISMATCH')
                    observation.update(status='COMPLETED',decision_output=decision,risk=assess_risk(DecisionOutput.model_validate(decision)))
                except Exception as exc:
                    observation.update(status='FAILED',decision_output=None,risk={'status':'SKIPPED','approved_weight':None,'reason_codes':[str(exc) if isinstance(exc,AgentFailure) else 'EXECUTION_FAILED']})
                put(observation)
                checkpoint()
                if observation['status']!='COMPLETED':raise ValueError('failed/schema-invalid observation; stopped')
        except BaseException as exc:
            for observation in get_all():
                if observation['status']=='RUNNING':
                    observation.update(status='INTERRUPTED',decision_output=None,
                        risk={'status':'SKIPPED','approved_weight':None,'reason_codes':['INTERRUPTED_UNKNOWN']})
                    put(observation)
            halt('INTERRUPTED' if not isinstance(exc,Exception) else 'DRIFT_FAILURE_OR_INCOMPLETE_BILLING')
            if not isinstance(exc,Exception):raise
    observations=get_all()
    with store.connect() as conn:
        stopped=conn.execute('SELECT payload FROM ablation_meta WHERE id=?',('halt',)).fetchone()
        checks=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM ablation_billing_checks ORDER BY sequence')]
    pairs=[]
    def raw_target(observation):
        try:
            parsed=json.loads(observation['calls'][-1]['raw_response'])
            return parsed.get('target_weight') if isinstance(parsed,dict) else None
        except (IndexError,ValueError,TypeError):return None
    for day in sorted({o['decision'] for o in observations}):
        by={o['condition']:o for o in observations if o['decision']==day}
        a,b=by['without_critic'],by['with_critic']
        pairs.append({'decision':day,'without_observation_id':a['observation_id'],'with_observation_id':b['observation_id'],
            'without_target':(a['decision_output'] or {}).get('target_weight'),'with_target':(b['decision_output'] or {}).get('target_weight'),
            'raw_target_without':raw_target(a),'raw_target_with':raw_target(b),
            'without_risk':a['risk'],'with_risk':b['risk'],'both_valid':a['status']==b['status']=='COMPLETED',
            'risk_codes_without':(a['decision_output'] or {}).get('acknowledged_risks'),
            'risk_codes_with':(b['decision_output'] or {}).get('acknowledged_risks'),
            'saved_critic_flags':b['input']['critic']['risk_flags'],
            'with_critic_flags_explicitly_acknowledged':set(b['input']['critic']['risk_flags'])-{'NO_OBJECTION'} <=
                set((b['decision_output'] or {}).get('acknowledged_risks',[])) if b['status']=='COMPLETED' else None})
    result={'status':'STOPPED' if stopped else 'COMPLETED','manifest':manifest,'observations':observations,'paired':pairs,
        'budget':budget.summary(),'billing_checks':checks,'halt_reason':json.loads(stopped[0]) if stopped else None}
    return result


def financial_replays(report,dataset):
    """Reuse saved A/C as Replay provenance, never as new model invocations."""
    config=report['manifest']['configuration'];results={}
    for condition in ('without_critic','with_critic'):
        observations=sorted([o for o in report['observations'] if o['condition']==condition],key=lambda o:o['decision'])
        source_id=str(uuid5(NAMESPACE_URL,report['manifest']['manifest_hash']+condition))
        decisions=[]
        for o in observations:
            prefix=[copy.deepcopy(o['saved_analysis_call'])]
            if o['saved_critic_call']:prefix.append(copy.deepcopy(o['saved_critic_call']))
            for call in prefix:call['saved_predecessor_reused_not_new_inference']=True
            decisions.append({'snapshot':o['snapshot'],'decision_session':o['decision'],'execution_session':o['execution'],
                'status':o['risk']['status'],'reason_codes':o['risk']['reason_codes'],'analysis_output':o['input']['analysis'],
                'critic_output':o['input'].get('critic'),'decision_output':o['decision_output'],'calls':prefix+o['calls']})
        source={'decisions':decisions,'provider_attribution':report['manifest']['provider_attribution'],
                'provider_settings':report['manifest']['provider_settings'],'decision_origin':'saved-conditioned-decisions'}
        exp=ExperimentConfig(strategy_id='single_agent',llm_mode='replay',replay_source_run_id=UUID(source_id),
            dataset_id=config['dataset_id'],dataset_hash=config['dataset_hash'],start_date=config['start_date'],end_date=config['end_date'],
            initial_cash=config['initial_cash'],fee_bps=config['fee_bps'],slippage_bps=config['slippage_bps'])
        first=run_experiment(exp,dataset,source=source);second=run_experiment(exp,dataset,source=first)
        equal=semantic_result(first)==semantic_result(second)
        if not equal or first['metrics']['llm_calls'] or first['metrics']['llm_cost_usd']:
            raise ValueError('zero-provider financial Replay equality failed')
        results[condition]={'source_recordset_id':source_id,'saved_source_recordset':source,'result':first,
            'second_replay_equal':equal,'provider_calls':0,'replay_cost_usd':0}
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','source-report','group-evidence','catalogue','manifest','database','output','key-file'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    config=PilotConfig.model_validate_json(private_path(args.config).read_bytes())
    source=json.loads(args.source_report.read_text(encoding='utf-8-sig'));group=json.loads(args.group_evidence.read_text(encoding='utf-8-sig'))
    catalogue=json.loads(args.catalogue.read_text(encoding='utf-8-sig'));dataset=DatasetRegistry().load(config.dataset_id)
    manifest_path=private_path(args.manifest);database=private_path(args.database);output=private_path(args.output)
    load_key(args.key_file,'kuaiapi')
    key=os.environ['KUAIAPI_API_KEY']
    if args.execute:
        frozen=json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest=make_manifest(config,source,group,catalogue,frozen['billing_baseline'],dataset)
        if manifest!=frozen or manifest['code_dirty']:raise ValueError('clean frozen source/config/inputs required')
    else:
        day=datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
        baseline=capture_billing(key,day)
        manifest=make_manifest(config,source,group,catalogue,baseline,dataset)
        if manifest['code_dirty']:raise ValueError('commit reviewed source before preregistration')
        manifest_path.parent.mkdir(parents=True,exist_ok=True)
        with manifest_path.open('x',encoding='utf-8') as stream:json.dump(manifest,stream,ensure_ascii=False,indent=2)
        print(json.dumps({'status':'PREREGISTERED_ZERO_INFERENCE','estimate':manifest['estimate']}));return
    validate_database(database,manifest)
    store=Store(database);store.initialize()
    try:
        result=execute(store,manifest,config,dataset,KuaiAPIProvider(config),lambda:capture_billing(key,manifest['billing_baseline']['day']))
        result['financial_replays']=financial_replays(result,dataset)
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        print(json.dumps({'status':result['status'],'valid_observations':sum(o['status']=='COMPLETED' for o in result['observations']),
            'budget':result['budget'],'paired':result['paired'],'replay_provider_calls':0}))
    finally:store.close()


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('Ablation interrupted; durable reservations retained; no automatic resend.',file=sys.stderr);sys.exit(130)
    except Exception as exc:
        print('Ablation stopped ('+type(exc).__name__+'); inspect private manifest/ledger; no exception values printed.',file=sys.stderr);sys.exit(1)
