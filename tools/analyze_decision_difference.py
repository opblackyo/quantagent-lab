"""Compare explicitly selected saved API sources offline; never construct a provider.

Only contract models are imported. No SDK, HTTP client, key loader or run creator
is used. IDs are excluded from financial equality, never from source selection.
Outputs contain private traces and require a new, explicit private destination.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.models import AnalysisOutput, CriticOutput, DecisionOutput, FEATURE_NAMES

CONTRACTS = {'analysis': AnalysisOutput, 'critic': CriticOutput, 'decision': DecisionOutput}
NON_FINANCIAL = ('llm_', 'agent_', 'mock_', 'uncertain_', 'reserved_')
TOLERANCES = {'usd_absolute': 1e-9, 'shares_absolute': 1e-12, 'dimensionless_absolute': 1e-12, 'relative': 1e-12}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('nonfinite JSON')
    return json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)


@contextmanager
def offline():
    """Also fail closed if a future dependency tries a socket connection."""
    targets = [(socket.socket, 'connect'), (socket.socket, 'connect_ex'), (socket.socket, 'sendto'),
               (socket, 'create_connection'), (socket, 'getaddrinfo')]
    originals = [(owner, name, getattr(owner, name)) for owner, name in targets]
    def forbidden(*args, **kwargs):
        raise RuntimeError('network forbidden in offline analysis')
    for owner, name in targets:
        setattr(owner, name, forbidden)
    try:
        yield
    finally:
        for owner, name, value in originals:
            setattr(owner, name, value)


def numerical_difference(left, right, field=''):
    """Exact binary-float equality first; tolerances are an additional result."""
    differences = []
    def visit(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
            for key in a:
                visit(a[key], b[key], path + '/' + key)
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for index, (x, y) in enumerate(zip(a, b)):
                visit(x, y, path + '/' + str(index))
        elif a != b:
            numeric = type(a) in (int, float) and type(b) in (int, float)
            absolute = 1e-12 if any(k in path for k in ('shares', 'quantity', 'weight', 'return', 'drawdown', 'ratio', 'fraction')) else 1e-9
            close = numeric and math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=absolute)
            differences.append({'field': path, 'left': a, 'right': b,
                                'absolute_difference': abs(a-b) if numeric else None, 'within_tolerance': close})
    visit(left, right, field)
    numeric = [d['absolute_difference'] for d in differences if d['absolute_difference'] is not None]
    return {'exact_equal': not differences, 'within_tolerance': all(d['within_tolerance'] for d in differences),
            'different_fields': len(differences), 'max_absolute_difference': max(numeric, default=0), 'differences': differences}


def financial_trades(result):
    return [{k:v for k,v in trade.items() if k not in ('trade_id', 'decision_id')} for trade in result['trades']]


def financial_metrics(result):
    return {k:v for k,v in result['metrics'].items() if not k.startswith(NON_FINANCIAL)}


def paired_counts(left, right):
    pairs = [numerical_difference(a, b) for a,b in zip(left, right)]
    return {'aligned_pairs':len(pairs), 'exact_pairs':sum(p['exact_equal'] for p in pairs),
            'different_pairs':sum(not p['exact_equal'] for p in pairs),
            'unpaired_records':abs(len(left)-len(right))}


def classify(raw, validated, approved, status, trades_equal, metrics_equal, rounded_equal):
    if raw[0] is not None and validated[0] is not None and raw[0] == raw[1] and validated[0] == validated[1]:
        return 'model-target'
    if validated[0] == validated[1] and raw[0] != raw[1]:
        return 'schema-validation-outcome'
    if approved[0] == approved[1] and status[0] == status[1] and validated[0] != validated[1]:
        return 'deterministic-risk-outcome'
    if approved[0] is None and approved[1] is None:
        return 'shared-skip-or-rejection'
    if trades_equal:
        return 'fill-or-no-trade-threshold'
    if metrics_equal:
        return 'terminal-metric-coincidence-with-different-path'
    return 'rounded-performance-only' if rounded_equal else 'different'


def audit_trace(row, decision):
    snap = decision['snapshot']
    features = {k:snap[k] for k in FEATURE_NAMES}
    if snap['dataset_hash'] != row['config']['dataset_hash'] or snap['feature_version'] != row['result']['feature_version']:
        raise ValueError('snapshot dataset/feature mismatch')
    state, proof = {}, []
    stages = ['decision'] if row['config']['strategy_id'] == 'single_agent' else ['analysis', 'critic', 'decision']
    previous_stage = -1
    for call in decision['calls']:
        stage = call['stage']
        if stage not in stages or stages.index(stage) < previous_stage or call['mode'] != 'api':
            raise ValueError('source stage order or API mode mismatch')
        if stage in state or (stages.index(stage)>0 and stages[stages.index(stage)-1] not in state):
            raise ValueError('required validated predecessor missing or successful stage repeated')
        previous_stage = stages.index(stage)
        expected = {'features': features}
        if stage in ('critic', 'decision') and 'analysis' in state:
            expected['analysis'] = state['analysis']
        if stage == 'decision' and 'critic' in state:
            expected['critic'] = state['critic']
        schema = CONTRACTS[stage].model_json_schema()
        prompt = digest({'version':call['prompt_version'], 'instructions':call['instructions'], 'schema':schema})
        cache = digest({'data':snap['dataset_hash'], 'feature_version':snap['feature_version'],
                        'model':call['model_id'], 'settings':call['model_settings'],
                        'provider':call['provider_attribution'], 'prompt':prompt, 'input':expected})
        if (call['input'] != expected or call['input_hash'] != digest(expected) or
            call['schema_hash'] != digest(schema) or call['prompt_hash'] != prompt or call['cache_key'] != cache):
            raise ValueError('stage input/predecessor/prompt/schema/cache hash mismatch')
        if (call['model_id'] != row['config']['model_id'] or call['actual_model_id'] != row['config']['model_id'] or
            call['model_settings'] != row['result']['provider_settings'] or
            call['provider_attribution'] != row['result']['provider_attribution']):
            raise ValueError('model/provider/settings mismatch')
        manifest_prompt = row['result'].get('pilot_manifest', {}).get('prompts', {}).get(stage)
        if manifest_prompt and (manifest_prompt['instructions'] != call['instructions'] or manifest_prompt['prompt_hash'] != prompt):
            raise ValueError('frozen prompt mismatch')
        try:
            raw = strict_json(call['raw_response'])
        except (ValueError, TypeError):
            raw = None
        if call['status'] == 'VALIDATED':
            parsed = CONTRACTS[stage].model_validate(raw).model_dump(mode='json')
            if parsed != call['validated_output'] or parsed != decision[stage + '_output']:
                raise ValueError('raw/validated/recorded output mismatch')
            envelope=call.get('raw_provider_response', {})
            text=''.join(part['text'] for item in envelope.get('output',[]) if item.get('type')=='message'
                         for part in item.get('content',[]) if part.get('type')=='output_text')
            if envelope.get('model')!=call['actual_model_id'] or envelope.get('status')!='completed' or text!=call['raw_response']:
                raise ValueError('saved provider envelope differs from the validated response')
            state[stage] = parsed
        proof.append({'call_id':call['call_id'], 'stage':stage, 'attempt':call['attempt'], 'status':call['status'],
            'provider_response_id':call.get('raw_provider_response', {}).get('id'),
            'input':call['input'], 'input_hash':call['input_hash'], 'numeric_features_hash':digest(features),
            'predecessor_hashes':{k:digest(v) for k,v in expected.items() if k!='features'},
            'prompt_hash':prompt, 'schema_hash':call['schema_hash'], 'cache_key':cache,
            'model_provider_settings_hash':digest({'model':call['model_id'], 'settings':call['model_settings'], 'provider':call['provider_attribution']}),
            'raw_response_hash':digest(call['raw_response']), 'validated_output_hash':digest(call['validated_output']),
            'envelope_hash':digest(call.get('raw_provider_response')), 'validated_output':call['validated_output'],
            'raw_target':raw.get('target_weight') if isinstance(raw, dict) else None})
    return proof


def decision_ledger(row, decision):
    result, cfg = row['result'], row['config']
    trade = next((t for t in result['trades'] if t['decision_id'] == decision['decision_id']), None)
    before = [p for p in result['equity'] if p['session_date'] < (decision['execution_session'] or decision['decision_session'])]
    after = next((p for p in result['equity'] if p['session_date'] == decision['execution_session']), None)
    prior = before[-1] if before else {'cash':cfg['initial_cash'], 'shares':0, 'equity':cfg['initial_cash']}
    if trade and (trade['execution_session'] != decision['execution_session'] or trade['trade_id'] != decision['trade_id']):
        raise ValueError('trade/decision execution link mismatch')
    return {'decision_id':decision['decision_id'], 'raw_target':None,
            'validated_target':(decision['decision_output'] or {}).get('target_weight'),
            'risk_requested':decision['requested_weight'], 'risk_approved':decision['approved_weight'],
            'risk_status':decision['status'], 'reason_codes':decision['reason_codes'],
            'decision_output':decision['decision_output'], 'prior_position':prior,
            'next_position':{'cash':trade['cash_after'], 'shares':trade['shares_after']} if trade else {k:prior[k] for k in ('cash', 'shares')},
            'next_open_fill':trade, 'execution_close_equity_drawdown':after}


def analyze(single, multi, *, actual_charges=None, aggregate=None, tariff=None):
    rows = [single, multi]
    for row, strategy in zip(rows, ('single_agent', 'multi_agent')):
        cfg = row['config']
        if (row['status'] != 'COMPLETED' or cfg['strategy_id'] != strategy or cfg['llm_mode'] != 'api' or
            cfg.get('replay_source_run_id') is not None or row['result']['decision_origin'] != 'api'):
            raise ValueError('explicit completed original API source required, not Replay')
        if row['metrics']!=row['result']['metrics']:
            raise ValueError('summary metrics differ from saved source result')
        trades=row['result']['trades']
        decision_ids={d['decision_id'] for d in row['result']['decisions']}
        if (len({t['decision_id'] for t in trades})!=len(trades) or
            any(t['decision_id'] not in decision_ids for t in trades)):
            raise ValueError('duplicate or unlinked fill evidence')
    if single['run_id'] == multi['run_id']:
        raise ValueError('source run identities must differ')
    if single.get('source_hash')!=multi.get('source_hash'):
        raise ValueError('source engine fingerprints differ')
    config_keys = ('dataset_hash', 'start_date', 'end_date', 'initial_cash', 'fee_bps', 'slippage_bps',
                   'max_exposure', 'research_group_id', 'prompt_version', 'model_provider', 'model_id')
    result_keys = ('engine_version', 'feature_version', 'provider_settings', 'provider_attribution', 'model_ids')
    if (any(single['config'][k] != multi['config'][k] for k in config_keys) or
        any(single['result'][k] != multi['result'][k] for k in result_keys)):
        raise ValueError('source comparison configuration mismatch')
    indexed = []
    for row in rows:
        decisions = {(d['decision_session'], d['execution_session']):d for d in row['result']['decisions']}
        if len(decisions) != len(row['result']['decisions']):
            raise ValueError('duplicate decision/execution session')
        indexed.append(decisions)
    if indexed[0].keys() != indexed[1].keys():
        raise ValueError('decision/execution calendars differ')
    trades = numerical_difference(financial_trades(single['result']), financial_trades(multi['result']), 'trades')
    equity = numerical_difference(single['result']['equity'], multi['result']['equity'], 'equity')
    metrics = numerical_difference(financial_metrics(single['result']), financial_metrics(multi['result']), 'metrics')
    rounded = all((None if single['metrics'][k] is None else round(single['metrics'][k]*scale,2)) ==
                  (None if multi['metrics'][k] is None else round(multi['metrics'][k]*scale,2))
                  for k,scale in {'total_return':100,'max_drawdown':100,'final_equity':1,'sharpe_ratio':1}.items())
    weeks, calls, costs = [], [], defaultdict(list)
    for key in sorted(indexed[0]):
        pair, traces = [], []
        for row, decisions in zip(rows, indexed):
            decision = decisions[key]
            traces.append(audit_trace(row, decision))
            pair.append(decision_ledger(row, decision))
            finals = [p for p in traces[-1] if p['stage']=='decision']
            pair[-1]['raw_target'] = finals[-1]['raw_target'] if finals else None
            for proof, call in zip(traces[-1], decision['calls']):
                calls.append({**proof, 'strategy':row['config']['strategy_id'], 'decision_session':key[0]})
                costs[(row['config']['strategy_id'], call['stage'])].append(call)
        if (digest({k:indexed[0][key]['snapshot'][k] for k in FEATURE_NAMES}) !=
            digest({k:indexed[1][key]['snapshot'][k] for k in FEATURE_NAMES})):
            raise ValueError('numeric feature snapshots differ')
        snap_a = {k:v for k,v in indexed[0][key]['snapshot'].items() if k!='snapshot_id'}
        snap_b = {k:v for k,v in indexed[1][key]['snapshot'].items() if k!='snapshot_id'}
        if snap_a != snap_b:
            raise ValueError('snapshot/session/as-of evidence differs')
        fill_equal = numerical_difference(
            {k:v for k,v in (pair[0]['next_open_fill'] or {}).items() if k not in ('trade_id', 'decision_id')},
            {k:v for k,v in (pair[1]['next_open_fill'] or {}).items() if k not in ('trade_id', 'decision_id')})['exact_equal']
        weeks.append({'decision':key[0], 'execution':key[1], 'numeric_snapshot_hash':digest({k:snap_a[k] for k in FEATURE_NAMES}),
            'snapshot_without_id_hash':digest(snap_a), 'single_snapshot_id':indexed[0][key]['snapshot_id'],
            'multi_snapshot_id':indexed[1][key]['snapshot_id'], 'single':pair[0], 'multi':pair[1],
            'convergence_layer':classify([d['raw_target'] for d in pair], [d['validated_target'] for d in pair],
                [d['risk_approved'] for d in pair], [d['risk_status'] for d in pair], fill_equal, metrics['exact_equal'], rounded),
            'traces':{'single':traces[0], 'multi':traces[1]},
            'critic_flags_explicitly_acknowledged':set((indexed[1][key]['critic_output'] or {}).get('risk_flags',[])).issubset(
                set((indexed[1][key]['decision_output'] or {}).get('acknowledged_risks',[]))),
            'critic_causal_contribution':'unidentifiable without no-Critic counterfactual; Analysis has no target'})
    for field in ('call_id', 'provider_response_id'):
        values = [c[field] for c in calls]
        if any(v is None for v in values) or len(set(values)) != len(values):
            raise ValueError('missing/reused source call or provider response identity')
    cache_scopes = defaultdict(set)
    for c in calls:
        cache_scopes[c['cache_key']].add((c['strategy'], c['stage'], c['decision_session']))
    collisions = [k for k, scopes in cache_scopes.items() if len(scopes)>1]
    if collisions:
        raise ValueError('cross-strategy/stage/session cache key collision')
    cost_rows = []
    actual_charges = actual_charges or {}
    for (strategy, stage), group in sorted(costs.items()):
        known = [Decimal(str(actual_charges[c['call_id']])) for c in group if c['call_id'] in actual_charges]
        entry = {'strategy':strategy, 'stage':stage, 'calls':len(group),
            'input_tokens':sum(c['input_tokens'] for c in group), 'output_tokens':sum(c['output_tokens'] for c in group),
            'latency_ms':sum(c['latency_ms'] for c in group),
            'historical_recorded_usage_estimate_usd':str(sum((Decimal(str(c['cost_usd'])) for c in group), Decimal(0))),
            'individually_confirmed_charge_usd':str(sum(known)) if known else None,
            'individually_confirmed_calls':len(known), 'actual_all_calls_confirmed_individually':len(known)==len(group)}
        if tariff:
            entry['uncached_tariff_allocation_usd_not_observed_actual'] = str((Decimal(entry['input_tokens'])*Decimal(str(tariff['input_usd_per_million']))+
                Decimal(entry['output_tokens'])*Decimal(str(tariff['output_usd_per_million'])))/Decimal(1000000))
        cost_rows.append(entry)
    if aggregate and set(aggregate['covered_call_ids']) != {c['call_id'] for c in calls}:
        raise ValueError('aggregate coverage differs from selected source calls')
    if aggregate and (aggregate.get('unit')!='USD' or aggregate.get('requests')!=len(calls) or
        aggregate.get('input_tokens')!=sum(c['input_tokens'] for group in costs.values() for c in group) or
        aggregate.get('output_tokens')!=sum(c['output_tokens'] for group in costs.values() for c in group)):
        raise ValueError('aggregate unit/request/token counts differ from selected source calls')
    return {'version':1, 'offline':True, 'new_provider_calls':0, 'new_spending_usd':0,
        'source_ids':{'single':single['run_id'], 'multi':multi['run_id']},
        'source_result_hashes':{r['config']['strategy_id']:digest(r['result']) for r in rows},
        'dataset_hash':single['config']['dataset_hash'], 'configuration_hashes':{r['config']['strategy_id']:digest(r['config']) for r in rows},
        'tolerances':TOLERANCES, 'comparisons':{'trades':trades, 'equity_cash_holdings_drawdown':equity, 'financial_metrics':metrics},
        'counts':{'weeks':len(weeks), 'trades_each':[len(r['result']['trades']) for r in rows],
                  'equity_points_each':[len(r['result']['equity']) for r in rows]},
        'exact_pair_counts':{'trades':paired_counts(financial_trades(single['result']),financial_trades(multi['result'])),
            'daily_positions_cash_equity':paired_counts(single['result']['equity'],multi['result']['equity'])},
        'weeks':weeks, 'artifact_audit':{'unique_call_ids':len(calls), 'unique_response_ids':len(calls),
            'unique_cache_keys':len(cache_scopes), 'cross_strategy_stage_collisions':collisions,
            'distinct_raw_response_hashes':len({c['raw_response_hash'] for c in calls}),
            'original_runtime_object_identity':'not recoverable from serialized evidence; distinct IDs/hashes and source construction must be interpreted together'},
        'cost_by_strategy_stage':cost_rows, 'confirmed_actual_aggregate':aggregate,
        'causal_conclusion':'Critic benefit or target change is unidentifiable from these observational traces.'}


def load_sources(path, single_id, multi_id, database=None):
    report = strict_json(path.read_text(encoding='utf-8'))
    selected = []
    charges = {}
    for source_id in (single_id, multi_id):
        matches = [r for r in report['runs'] if r['run_id']==source_id]
        if len(matches)!=1:
            raise ValueError('explicit source ID not unique in export')
        selected.append(matches[0])
    if database:
        # Immutable settled snapshot: no WAL, schema initialization or locks written.
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro&immutable=1', uri=True) as conn:
            for row in selected:
                stored = conn.execute('SELECT status,config_json,result_json FROM runs WHERE run_id=?',(row['run_id'],)).fetchone()
                if not stored or stored[0]!='COMPLETED' or strict_json(stored[1])!=row['config'] or strict_json(stored[2])!=row['result']:
                    raise ValueError('SQLite/export source identity mismatch')
            charges = {call_id:amount for call_id,amount in conn.execute('SELECT call_id,actual_charge FROM pilot_attempts WHERE actual_charge IS NOT NULL')}
    return selected, charges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-report', type=Path, required=True)
    parser.add_argument('--single-id', required=True)
    parser.add_argument('--multi-id', required=True)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--billing', type=Path)
    parser.add_argument('--tariff', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    try:
        relative = output.relative_to(ROOT)
    except ValueError:
        pass  # Explicit private evidence outside the publication checkout.
    else:
        if subprocess.run(['git','check-ignore','-q','--',relative.as_posix()],cwd=ROOT).returncode or subprocess.check_output(['git','ls-files','--',relative.as_posix()],cwd=ROOT):
            raise ValueError('private trace output must be ignored and untracked')
    if output.exists():
        raise ValueError('refusing to overwrite settled analysis evidence')
    with offline():
        selected, charges = load_sources(args.source_report, args.single_id, args.multi_id, args.database)
        result = analyze(*selected, actual_charges=charges,
            aggregate=strict_json(args.billing.read_text(encoding='utf-8-sig')) if args.billing else None,
            tariff=strict_json(args.tariff.read_text(encoding='utf-8-sig')) if args.tariff else None)
        result['sqlite_source_identity_verified'] = args.database is not None
        result['provider_modules_imported'] = [name for name in ('app.agents','app.gateway') if name in sys.modules]
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.write('\n')
    print(json.dumps({'weeks':len(result['weeks']), 'exact_financial_equality':result['comparisons']['financial_metrics']['exact_equal'],
                      'new_provider_calls':0, 'network_connections_forbidden':True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Offline analysis stopped ('+type(exc).__name__+'); no source values or paths printed.',file=sys.stderr)
        sys.exit(1)
