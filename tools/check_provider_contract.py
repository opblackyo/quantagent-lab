"""Offline-only Responses acceptance inspector and synthetic six-slot harness.

Reuses adapters' usage parsing, strict validators and durable PilotBudget. It has
no execute mode, key loading, HTTP client construction or trade/risk path.
Live execution requires a separately approved protocol and explicit driver.
"""
import argparse
import copy
import json
import math
from pathlib import Path
import sys
import tempfile
from uuid import NAMESPACE_URL,uuid5

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'tools')]
from app.agents import validate_agent_output
from app.data import canonical_hash
from app.gateway import KuaiAPIProvider
from app.pilot import PilotBudget
from app.storage import Store
from analyze_decision_difference import offline

SEQUENCE=('A','B','B','A','A','B')
DOCUMENTED_TIERS={'auto','default','flex','scale','priority','fast','ultrafast'}


def inspect_call(call,expected_model):
    """Classify saved adapter output, never repair it or assert tier causality."""
    env=call.get('raw_provider_response',{})
    issues=[];warnings=[];stop=[]
    if not isinstance(env,dict) or 'malformed_response_body' in env or 'malformed_response_envelope' in env:
        env=env if isinstance(env,dict) else {}
        stop.append('TRANSPORT_ENVELOPE_INVALID')
    http_status=call.get('http_status')
    if http_status is None:warnings.append('HTTP_STATUS_NOT_RECORDED')
    elif type(http_status) is not int or not 200<=http_status<300:stop.append('TRANSPORT_HTTP_ERROR')
    if call.get('error_code') in ('PROVIDER_TIMEOUT','PROVIDER_NETWORK_ERROR','PROVIDER_HTTP_ERROR','PROVIDER_ENDPOINT_MISMATCH'):
        stop.append('TRANSPORT_OR_ENDPOINT_FAILURE')
    if env.get('model')!=expected_model:stop.append('MODEL_LABEL_DRIFT')
    if env.get('error') is not None:stop.append('PROVIDER_ERROR')
    output=env.get('output',[])
    if not isinstance(output,list):output=[];issues.append('OUTPUT_SHAPE_INVALID')
    if any(isinstance(item,dict) and item.get('type') not in ('reasoning','message') for item in output):
        stop.append('UNREQUESTED_TOOL_OR_OUTPUT_TYPE')
    messages=[item for item in output if isinstance(item,dict) and item.get('type')=='message']
    parts=[]
    for message in messages:
        content=message.get('content',[])
        if not isinstance(content,list):issues.append('MESSAGE_CONTENT_SHAPE_INVALID')
        else:parts.extend(part for part in content if isinstance(part,dict))
    envelope_text=''.join(part['text'] for part in parts if part.get('type')=='output_text' and isinstance(part.get('text'),str))
    expected_adapter_text=envelope_text if env.get('status')=='completed' else ''
    correspondence=call.get('raw_response')==expected_adapter_text
    if not correspondence:stop.append('ADAPTER_TEXT_INTEGRITY_MISMATCH')
    if env.get('status')!='completed':issues.append('RESPONSE_NOT_COMPLETED')
    if env.get('incomplete_details') is not None:
        issues.append('INCOMPLETE_DETAILS_PRESENT')
    if not messages:issues.append('MESSAGE_MISSING')
    for message in messages:
        status=message.get('status')
        if status is None:warnings.append('MESSAGE_STATUS_NOT_REPORTED')
        elif env.get('status')=='completed' and status!='completed':issues.append('MESSAGE_STATUS_CONTRADICTS_RESPONSE')
    if any(part.get('type')=='refusal' for part in parts):issues.append('REFUSAL_NOT_DECISION')
    text_config=env.get('text')
    fmt=text_config.get('format') if isinstance(text_config,dict) else None
    if not isinstance(fmt,dict):warnings.append('FORMAT_ECHO_NOT_REPORTED')
    elif fmt.get('type')!='json_schema' or fmt.get('strict') is False:issues.append('FORMAT_ECHO_CONTRADICTS_REQUEST')
    tier=env.get('service_tier')
    tier_scope='documented-observation' if isinstance(tier,str) and tier in DOCUMENTED_TIERS else 'unsupported-or-absent-observability'
    if tier_scope!='documented-observation':warnings.append('TIER_MAPPING_NOT_ESTABLISHED')
    input_tokens,output_tokens,known=KuaiAPIProvider.usage(env.get('usage'))
    if not known or call.get('input_tokens')!=input_tokens or call.get('output_tokens')!=output_tokens:
        known=False;stop.append('UNKNOWN_OR_INCONSISTENT_USAGE')
    cost=call.get('cost_usd')
    estimated_cost_known=(call.get('cost_status')=='known' and type(cost) in (int,float) and math.isfinite(cost) and cost>=0)
    if not estimated_cost_known:stop.append('UNKNOWN_ESTIMATED_COST')
    if known and (input_tokens>32768 or output_tokens>2048):stop.append('USAGE_BOUND_EXCEEDED')
    parsed=None;schema_errors=[]
    try:
        parsed=validate_agent_output('decision',call.get('raw_response')).model_dump(mode='json')
    except Exception as exc:
        issues.append('DECISION_JSON_OR_SCHEMA_INVALID')
        if hasattr(exc,'errors'):
            schema_errors=[{'type':e['type'],'location':list(e['loc'])} for e in exc.errors()]
        else:schema_errors=[{'type':type(exc).__name__}]
    accepted=parsed is not None and not issues and not stop
    return {'format_pass':accepted,'schema_valid':parsed is not None,
        'decision_output_eligible':parsed if accepted else None,'validated_output':parsed,
        'failure_codes':sorted(set(issues)),'schema_errors':schema_errors,'observation_warnings':sorted(set(warnings)),
        'hard_stop_codes':sorted(set(stop)),'usage_known':known,'estimated_cost_known':estimated_cost_known,
        'actual_charge_verification':'not performed by output inspector; separate readonly reconciliation required',
        'reported_tokens':{'input':input_tokens,'output':output_tokens},
        'http_status_observed':http_status,'provider_response_id':env.get('id'),
        'response_status':env.get('status'),'incomplete_details':env.get('incomplete_details'),
        'message_statuses':[item.get('status') for item in messages],'format_echo':fmt,
        'raw_envelope_text_correspondence':correspondence,'adapter_status_policy_preserved':True,
        'envelope_text_hash':canonical_hash(envelope_text),'adapter_raw_hash':canonical_hash(call.get('raw_response')),
        'service_tier_observation':{'value':tier,'scope':tier_scope,'proves_fast_price_or_model_change':False},
        'raw_envelope_hash':canonical_hash(env),'call_id':call.get('call_id'),'latency_ms':call.get('latency_ms'),
        'no_trade_or_repair':True}


def inspect_stage(stage, envelope, raw_response, expected):
    """Stage-aware gate over the shared classifier. inspect_call stays decision-only."""
    from app.agents import classify_stage_envelope
    return classify_stage_envelope(stage, envelope, raw_response, expected)


def fixture_manifest():
    m={'kind':'provider-contract-acceptance-fixture-v1','configuration':{'total_budget_usd':.20,'max_calls':6},
        'sequence':list(SEQUENCE),'expected_model':'synthetic-model','per_attempt_reserve_usd':.02267136,
        'fixture_only':True,'attempts_per_slot':1,'collect_known_fee_format_failures':True}
    m['manifest_hash']=canonical_hash(m);return m


def synthetic_calls():
    output={'target_weight':.5,'confidence':.6,'rationale':'Synthetic offline fixture.',
        'evidence':['ma20'],'acknowledged_risks':[]}
    calls=[]
    for index in range(6):
        raw=json.dumps(output)
        env={'id':'synthetic-response-'+str(index),'model':'synthetic-model','status':'completed','service_tier':'auto',
            'text':{'format':{'type':'json_schema','strict':True}},
            'output':[{'type':'message','status':'completed','content':[{'type':'output_text','text':raw}]}],
            'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120}}
        calls.append({'call_id':'synthetic-call-'+str(index),'raw_provider_response':env,'raw_response':raw,
            'input_tokens':100,'output_tokens':20,'cost_status':'known','cost_usd':.00008775,'latency_ms':1})
    return calls


def collect_fixtures(store,manifest,fixtures):
    """Durability/idempotence fixture harness; cannot send application requests."""
    if manifest.get('kind')!='provider-contract-acceptance-fixture-v1' or not manifest.get('fixture_only'):
        raise ValueError('fixture harness cannot execute a live protocol')
    if manifest['sequence']!=list(SEQUENCE) or len(fixtures)!=6:raise ValueError('exact six-slot sequence required')
    with store.connect() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_budgets'").fetchone():
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='acceptance_records'").fetchone():
                raise ValueError('refusing original pilot/ablation accounting')
        conn.execute('CREATE TABLE IF NOT EXISTS acceptance_records (slot INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
    budget=PilotBudget(store,manifest)
    records=[]
    for index,condition in enumerate(SEQUENCE):
        with store.connect() as conn:
            prior=conn.execute('SELECT payload FROM acceptance_records WHERE slot=?',(index,)).fetchone()
        if prior:
            record=json.loads(prior[0])
            if record['status']=='RUNNING':
                record.update(status='UNCERTAIN_INTERRUPTED',hard_stop_codes=['UNCERTAIN_INTERRUPTED'])
                with store.connect() as conn:conn.execute('UPDATE acceptance_records SET payload=? WHERE slot=?',(json.dumps(record),index))
            records.append(record)
            if record.get('hard_stop_codes'):break
            continue
        attempt=str(uuid5(NAMESPACE_URL,manifest['manifest_hash']+':'+str(index)))
        budget.reserve(attempt,manifest['per_attempt_reserve_usd'])
        record={'slot':index+1,'input_label':condition,'attempt_id':attempt,'status':'RUNNING'}
        with store.connect() as conn:conn.execute('INSERT INTO acceptance_records VALUES (?,?)',(index,json.dumps(record)))
        call=copy.deepcopy(fixtures[index]);record.update(inspect_call(call,manifest['expected_model']),status='OBSERVED',fixture_call=call)
        budget.settle(attempt,call['cost_usd'] if record['usage_known'] and record['estimated_cost_known'] else None)
        with store.connect() as conn:conn.execute('UPDATE acceptance_records SET payload=? WHERE slot=?',(json.dumps(record),index))
        records.append(record)
        if record['hard_stop_codes']:break
    accepted=len(records)==6 and all(r.get('format_pass') for r in records)
    return {'fixture_only':True,'new_provider_calls':0,'new_spend_usd':0,'records':records,
        'sequence':list(SEQUENCE),'completed_slots':len(records),'not_run_slots':6-len(records),
        'provisional_acceptance':accepted,'acceptance_scope':'six-sample fixture result, not universal reliability/upstream proof',
        'simulated_budget':budget.summary()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saved-result',type=Path,help='Explicit private saved ablation JSON; inspect only')
    parser.add_argument('--output',type=Path,help='New private ignored/outside-checkout JSON; never overwrite')
    args=parser.parse_args()
    with offline():
        if args.saved_result:
            saved=json.loads(args.saved_result.read_text(encoding='utf-8-sig'))
            model=saved['manifest']['configuration']['model_id']
            result={'offline_inspection_only':True,'new_provider_calls':0,'new_spend_usd':0,
                'records':[inspect_call(c,model) for o in saved['observations'] for c in o['calls']]}
        else:
            with tempfile.TemporaryDirectory(prefix='contract-fixture-') as folder:
                store=Store(Path(folder)/'fixture.sqlite3');store.initialize()
                try:result=collect_fixtures(store,fixture_manifest(),synthetic_calls())
                finally:store.close()
        if args.output:
            output=args.output.resolve()
            try:relative=output.relative_to(ROOT)
            except ValueError:pass
            else:
                import subprocess
                if subprocess.run(['git','check-ignore','-q','--',relative.as_posix()],cwd=ROOT).returncode:
                    raise ValueError('private output must be ignored')
                if subprocess.check_output(['git','ls-files','--',relative.as_posix()],cwd=ROOT):raise ValueError('private output is tracked')
            output.parent.mkdir(parents=True,exist_ok=True)
            with output.open('x',encoding='utf-8') as stream:json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
        print(json.dumps({'offline':True,'new_provider_calls':0,'new_spend_usd':0,
            'observations':len(result['records']),'valid':sum(r.get('format_pass',False) for r in result['records']),
            'live_execute_mode_available':False}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Offline contract check stopped ('+type(exc).__name__+'); no source values printed.',file=sys.stderr);sys.exit(1)
