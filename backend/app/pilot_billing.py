"""Key-free billing reconciliation and anchored, non-spending price revisions.

Reading these records never constructs a provider. An observed charge does not
establish a key's group name, an upstream identity, or a future cache tariff.
"""
import json
import math
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .agents import replay_decision
from .data import canonical_hash, sha256
from .models import FeatureSnapshot
from .storage import now


def checked_manifest(manifest):
    if manifest.get('manifest_hash') != canonical_hash({k:v for k,v in manifest.items() if k!='manifest_hash'}):
        raise ValueError('manifest hash mismatch')


def _number(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise ValueError('invalid billing number')
    return Decimal(str(value))


def record_readonly_charge(store, pilot_id, evidence_bytes, window_bytes):
    """Correlate a singleton usage aggregate, without inventing a request ID.

    The preserved execution window must fall within the returned local day.
    Total, today, model and day aggregates must all isolate this one request.
    Subsequent activity makes aggregate-only attribution ambiguous and rejects it.
    """
    evidence=json.loads(evidence_bytes)
    window=json.loads(window_bytes)
    with store.connect() as conn:
        manifest_row=conn.execute('SELECT manifest_json FROM pilot_budgets WHERE pilot_id=?',(pilot_id,)).fetchone()
        probe_row=conn.execute('SELECT payload FROM pilot_probes WHERE pilot_id=?',(pilot_id,)).fetchone()
        attempts=conn.execute('SELECT * FROM pilot_attempts WHERE pilot_id=?',(pilot_id,)).fetchall()
    if not manifest_row or not probe_row or len(attempts)!=1:
        raise ValueError('singleton probe and durable budget required')
    manifest=json.loads(manifest_row[0]); checked_manifest(manifest)
    probe=json.loads(probe_row[0]); result=probe.get('result') or {}
    calls=result.get('calls') or []
    if len(calls)!=1 or calls[0]['status']!='VALIDATED' or calls[0]['cost_status']!='known':
        raise ValueError('strict validated probe with known usage required')
    call=calls[0]
    if attempts[0]['call_id']!=call['call_id']:
        raise ValueError('durable call mismatch')
    original={**result,'snapshot':probe['snapshot'],'status':'APPROVED'}
    replay=replay_decision(FeatureSnapshot.model_validate(probe['snapshot']),original,
                           expected_provider=manifest['provider_attribution'])
    if replay['reason_codes'] or replay['decision_output']!=result['decision_output']:
        raise ValueError('saved probe contract mismatch')
    query=evidence['query']
    if (evidence['source']!='project-key-authenticated-readonly-gateway-usage' or evidence['unit']!='USD' or
        query['start_date']!=query['end_date'] or type(query['days']) is not int or query['days']!=1):
        raise ValueError('single-day USD usage evidence required')
    zone=ZoneInfo(query['timezone'])
    start=datetime.fromisoformat(window['started_at_utc'])
    end=datetime.fromisoformat(window['finished_at_utc'])
    if (start.tzinfo is None or end.tzinfo is None or start>end or
        window['source_commit']!=manifest['code_commit'] or
        any(t.astimezone(zone).date().isoformat()!=query['start_date'] for t in (start,end))):
        raise ValueError('probe execution date/source mismatch')
    daily=evidence['daily_usage']; models=evidence['model_stats']
    if len(daily)!=1 or len(models)!=1 or daily[0]['date']!=query['start_date'] or models[0]['model']!=call['model_id']:
        raise ValueError('ambiguous date/model usage')
    rows=[daily[0],models[0],evidence['usage_today'],evidence['usage_total']]
    for row in rows:
        expected={'requests':1,'input_tokens':call['input_tokens'],'output_tokens':call['output_tokens'],
                  'total_tokens':call['input_tokens']+call['output_tokens']}
        if any(type(row.get(k)) is not int or row[k]!=v for k,v in expected.items()):
            raise ValueError('ambiguous requests/tokens in usage evidence')
        cache_fields=[k for k in row if k in ('cache_read_tokens','cache_write_tokens','cache_creation_tokens')]
        if len(cache_fields)<2 or any(type(row[k]) is not int or row[k]!=0 for k in cache_fields):
            raise ValueError('singleton reconciliation currently requires zero cache usage')
    actuals=[_number(r['actual_cost']) for r in rows]
    bases=[_number(r['cost']) for r in rows]
    billing=evidence['billing']
    multipliers=[_number(billing[k]) for k in ('group_rate_multiplier','resolved_rate_multiplier','effective_rate_multiplier')]
    if (len(set(actuals))!=1 or len(set(bases))!=1 or len(set(multipliers))!=1 or multipliers[0]<=0 or
        billing['object']!='sub2api.key_billing' or billing['billing_scope']!='token' or
        billing['peak_rate_enabled'] is not False or bases[0]*multipliers[0]!=actuals[0]):
        raise ValueError('inconsistent USD charge/multiplier semantics')
    receipt={'pilot_id':pilot_id,'call_id':call['call_id'],'evidence_sha256':sha256(evidence_bytes),
             'execution_window_sha256':sha256(window_bytes),'correlation':'singleton-date-model-tokens',
             'gateway_request_id_exposed':False,'model_id':call['model_id'],'date':query['start_date'],
             'input_tokens':call['input_tokens'],'output_tokens':call['output_tokens'],
             'actual_charge_usd':str(actuals[0]),'base_cost_usd':str(bases[0]),
             'observed_multiplier':str(multipliers[0]),'unit':'USD',
             'key_group_binding_checked':False,'future_cache_tariff_verified':False,
             'provider_calls':0,'replay_contract_equal':True}
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS pilot_charge_evidence (call_id TEXT PRIMARY KEY, pilot_id TEXT NOT NULL, receipt_json TEXT NOT NULL)')
        prior=conn.execute('SELECT receipt_json FROM pilot_charge_evidence WHERE call_id=?',(call['call_id'],)).fetchone()
        if prior and json.loads(prior[0])!=receipt:
            raise ValueError('confirmed charge evidence is immutable')
        row=conn.execute('SELECT actual_charge FROM pilot_attempts WHERE pilot_id=? AND call_id=?',(pilot_id,call['call_id'])).fetchone()
        if row[0] is not None and Decimal(row[0])!=actuals[0]:
            raise ValueError('confirmed actual charge is immutable')
        conn.execute('UPDATE pilot_attempts SET actual_charge=? WHERE pilot_id=? AND call_id=?',
                     (str(actuals[0]),pilot_id,call['call_id']))
        conn.execute('INSERT OR IGNORE INTO pilot_charge_evidence VALUES (?,?,?)',(call['call_id'],pilot_id,json.dumps(receipt)))
        probe['billing_reconciliation']=receipt
        # Billing evidence is deliberately separate from continuation approval.
        # A later verified group record, if any, must still pass the normal gate.
        if not probe.get('reconciliation'):
            probe['status']='BILLING_RECONCILED_GROUP_UNVERIFIED'
        conn.execute('UPDATE pilot_probes SET payload=? WHERE pilot_id=?',(json.dumps(probe),pilot_id))
    return receipt


PRICE_FIELDS={'pricing_unit','input_usd_per_million','output_usd_per_million',
    'input_balance_units_per_million','output_balance_units_per_million','balance_units_per_usd',
    'cache_write_balance_units_per_million','cache_read_balance_units_per_million',
    'runtime_pricing_verified','runtime_rate_multiplier','cache_write_usd_per_million','cache_read_usd_per_million',
    'price_source','price_verified_on','price_evidence_sha256'}


def register_price_revision(store, anchor, revision, evidence_sha256):
    """Explicit reviewed migration, retaining all original calls and manifests.

    This is an offline audit operation, not permission to infer. Incomplete rates
    can be recorded as a blocked candidate; execute still requires complete rates.
    """
    from .pilot import PilotConfig
    checked_manifest(anchor); checked_manifest(revision)
    if revision['code_dirty'] or not revision['code_commit'] or anchor['manifest_hash']==revision['manifest_hash']:
        raise ValueError('revision requires different committed clean source/configuration')
    if len(evidence_sha256)!=64 or any(c not in '0123456789abcdef' for c in evidence_sha256):
        raise ValueError('revision evidence SHA-256 required')
    left=PilotConfig.model_validate(anchor['configuration']).model_dump(mode='json')
    right=PilotConfig.model_validate(revision['configuration']).model_dump(mode='json')
    if ({k:v for k,v in left.items() if k not in PRICE_FIELDS}!=
        {k:v for k,v in right.items() if k not in PRICE_FIELDS}):
        raise ValueError('revision must preserve model/group/settings/data/budget/attempt limits')
    for field in ('provider','provider_settings','provider_attribution','dataset','engine_version','feature_version',
                  'prompt_version','prompts','calendar','sessions'):
        if anchor[field]!=revision[field]:
            raise ValueError('revision changed a research invariant')
    if left['provider']!='kuaiapi' or right['pricing_unit']!='runtime_usd_per_million':
        raise ValueError('only a KuaiAPI runtime USD correction is supported')
    receipt={'pilot_id':anchor['manifest_hash'],'revision_hash':revision['manifest_hash'],
             'anchor_manifest':anchor,'revision_manifest':revision,'evidence_sha256':evidence_sha256,
             'scope':'price-unit/source correction; no budget reset or inference'}
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing=conn.execute('SELECT manifest_json FROM pilot_budgets WHERE pilot_id=?',(anchor['manifest_hash'],)).fetchone()
        if not existing or json.loads(existing[0])!=anchor:
            raise ValueError('missing or mismatched original budget manifest')
        charge=conn.execute('SELECT receipt_json FROM pilot_charge_evidence WHERE pilot_id=?',(anchor['manifest_hash'],)).fetchall()
        if len(charge)!=1 or json.loads(charge[0][0])['evidence_sha256']!=evidence_sha256:
            raise ValueError('confirmed original probe charge required before revision')
        conn.execute('CREATE TABLE IF NOT EXISTS pilot_revisions (pilot_id TEXT PRIMARY KEY, revision_hash TEXT UNIQUE NOT NULL, receipt_json TEXT NOT NULL, created_at TEXT NOT NULL)')
        previous=conn.execute('SELECT receipt_json FROM pilot_revisions WHERE pilot_id=?',(anchor['manifest_hash'],)).fetchone()
        if previous and json.loads(previous[0])!=receipt:
            raise ValueError('registered revision is immutable; review another explicit migration')
        conn.execute('INSERT OR IGNORE INTO pilot_revisions VALUES (?,?,?,?)',
                     (anchor['manifest_hash'],revision['manifest_hash'],json.dumps(receipt),now()))
    return receipt


def require_revision(conn, pilot_id, manifest):
    checked_manifest(manifest)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_revisions'").fetchone():
        raise ValueError('explicit registered price revision required')
    row=conn.execute('SELECT receipt_json FROM pilot_revisions WHERE pilot_id=?',(pilot_id,)).fetchone()
    if not row or json.loads(row[0])['revision_manifest']!=manifest:
        raise ValueError('registered revision mismatch')


def record_aggregate_charge(store, pilot_id, evidence_bytes):
    """Reconcile final key usage without allocating aggregate USD to fake IDs.

    Only fully saved calls with known usage may be covered. Unknown liabilities
    are never released by an aggregate. The receipt remains immutable; further
    calls, if any, keep independent conservative reservations.
    """
    evidence=json.loads(evidence_bytes)
    if evidence.get('unit')!='USD' or evidence.get('source')!='project-key-authenticated-readonly-gateway-usage':
        raise ValueError('explicit readonly USD evidence required')
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        attempts=conn.execute('SELECT * FROM pilot_attempts WHERE pilot_id=?',(pilot_id,)).fetchall()
        manifest=json.loads(conn.execute('SELECT manifest_json FROM pilot_budgets WHERE pilot_id=?',(pilot_id,)).fetchone()[0])
        probe=json.loads(conn.execute('SELECT payload FROM pilot_probes WHERE pilot_id=?',(pilot_id,)).fetchone()[0])
        calls={c['call_id']:c for c in (probe.get('result') or {}).get('calls',[])}
        for row in conn.execute('SELECT config_json,result_json FROM runs WHERE result_json IS NOT NULL'):
            cfg=json.loads(row[0])
            if cfg.get('research_group_id')!=pilot_id or cfg.get('llm_mode')!='api':
                continue
            for decision in json.loads(row[1])['decisions']:
                for call in decision['calls']:
                    if call['call_id'] in calls and calls[call['call_id']]!=call:
                        raise ValueError('duplicate call evidence mismatch')
                    calls[call['call_id']]=call
        if (not attempts or {r['call_id'] for r in attempts}!=set(calls) or
            any(r['usage_estimate'] is None for r in attempts) or
            any(c.get('cost_status')!='known' for c in calls.values())):
            raise ValueError('unknown or incomplete attempts retain their reservations')
        model=manifest['configuration']['model_id']
        if any(c['model_id']!=model for c in calls.values()):
            raise ValueError('mixed model usage cannot be reconciled')
        expected={'requests':len(calls),'input_tokens':sum(c['input_tokens'] for c in calls.values()),
                  'output_tokens':sum(c['output_tokens'] for c in calls.values())}
        expected['total_tokens']=expected['input_tokens']+expected['output_tokens']
        model_rows=evidence['model_stats'];daily=evidence['daily_usage']
        if len(model_rows)!=1 or model_rows[0]['model']!=model or len(daily)!=1:
            raise ValueError('ambiguous model/day aggregate')
        query=evidence['query']
        first_receipt=probe.get('billing_reconciliation')
        if (not first_receipt or query['start_date']!=first_receipt['date'] or query['end_date']!=first_receipt['date'] or
            daily[0]['date']!=first_receipt['date'] or query['days']!=1):
            raise ValueError('aggregate must cover the original probe day')
        rows=[evidence['usage_total'],evidence['usage_today'],model_rows[0],daily[0]]
        for row in rows:
            if any(type(row.get(k)) is not int or row[k]!=v for k,v in expected.items()):
                raise ValueError('aggregate request/token counts do not match saved attempts')
        charges=[_number(row['actual_cost']) for row in rows]
        bases=[_number(row['cost']) for row in rows]
        billing=evidence['billing']
        multiplier=_number(billing['effective_rate_multiplier'])
        revision=conn.execute('SELECT receipt_json FROM pilot_revisions WHERE pilot_id=?',(pilot_id,)).fetchone()
        if (not revision or billing['billing_scope']!='token' or billing['peak_rate_enabled'] is not False or
            any(_number(billing[k])!=multiplier for k in ('group_rate_multiplier','resolved_rate_multiplier')) or
            multiplier!=_number(json.loads(revision[0])['revision_manifest']['configuration']['runtime_rate_multiplier'])):
            raise ValueError('runtime multiplier or billing scope drift')
        if len(set(charges))!=1 or len(set(bases))!=1 or abs(bases[0]*multiplier-charges[0])>Decimal('0.0000000001'):
            raise ValueError('inconsistent aggregate actual USD charge')
        if charges[0]<sum((Decimal(r['actual_charge']) for r in attempts if r['actual_charge'] is not None),Decimal(0)):
            raise ValueError('aggregate deduction cannot erase previously confirmed charges')
        receipt={'pilot_id':pilot_id,'covered_call_ids':sorted(calls),'model':model,**expected,
                 'date':query['start_date'],'unit':'USD','actual_charge_usd':str(charges[0]),
                 'base_cost_usd':str(bases[0]),'multiplier':str(multiplier),
                 'evidence_sha256':sha256(evidence_bytes),'scope':'aggregate only; individual request charges not allocated',
                 'invoiced_actual_usd':None,'unknown_reservations_released':False}
        conn.execute('CREATE TABLE IF NOT EXISTS pilot_usage_reconciliations (pilot_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)')
        previous=conn.execute('SELECT receipt_json FROM pilot_usage_reconciliations WHERE pilot_id=?',(pilot_id,)).fetchone()
        if previous:
            old=json.loads(previous[0])
            if any(old[k]!=receipt[k] for k in ('covered_call_ids','actual_charge_usd','input_tokens','output_tokens')):
                raise ValueError('confirmed aggregate evidence is immutable')
            return old
        conn.execute('INSERT INTO pilot_usage_reconciliations VALUES (?,?)',(pilot_id,json.dumps(receipt)))
    return receipt


def reconcile_study_usage(store, study_id, evidence):
    """Atomically record one aggregate receipt for exact verified study attempts.

    The receipt never allocates a per-call invoice and never releases an attempt
    that is unknown, uncovered, or outside this study. A second identical receipt
    is a no-op. Conflicting evidence raises without a second write.
    """
    if not isinstance(evidence, dict) or evidence.get('kind') != 'synthetic-aggregate-usage-v1':
        raise ValueError('synthetic aggregate study evidence required')
    if evidence.get('unit') != 'USD' or evidence.get('per_call_charges') is not None:
        raise ValueError('aggregate evidence cannot allocate per-call invoices')
    if evidence.get('delayed') is not False or type(evidence.get('external_requests')) is not int or evidence['external_requests'] != 0 or type(evidence.get('day_count')) is not int or evidence['day_count'] != 1:
        raise ValueError('delayed, external or multi-day usage is not allocated')
    covered = evidence.get('covered_call_ids')
    hashes = evidence.get('attempt_evidence_sha256')
    if not isinstance(covered, list) or not covered or len(set(covered)) != len(covered) or not isinstance(hashes, dict):
        raise ValueError('exact covered attempt ids and evidence hashes required')
    if set(hashes) != set(covered):
        raise ValueError('evidence hashes must match covered attempts exactly')
    actual = _number(evidence.get('actual_charge_usd')) if type(evidence.get('actual_charge_usd')) in (int, float) else None
    if actual is None:
        try:
            if isinstance(evidence.get('actual_charge_usd'), bool) or not isinstance(evidence.get('actual_charge_usd'), str):
                raise ValueError('invalid aggregate charge')
            actual = Decimal(evidence['actual_charge_usd'])
        except Exception:
            raise ValueError('invalid aggregate charge') from None
    if not actual.is_finite() or actual < 0:
        raise ValueError('invalid aggregate charge')
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        study = conn.execute('SELECT * FROM research_studies WHERE study_id=?', (str(study_id),)).fetchone()
        if not study:
            raise ValueError('unknown study')
        stored_plan = json.loads(study['plan_json'])
        payload = json.loads(stored_plan['canonical_payload']) if isinstance(stored_plan.get('canonical_payload'), str) else None
        if not isinstance(payload, dict):
            raise ValueError('study plan payload missing')
        if (evidence.get('manifest_hash') != study['manifest_hash'] or
            evidence.get('dataset_hash') != payload['request']['expected_dataset_hash'] or
            evidence.get('source_hash') != payload['source']['source_hash']):
            raise ValueError('study manifest, dataset or source hash mismatch')
        rows = conn.execute('SELECT * FROM pilot_attempts WHERE pilot_id=?', (study['namespace'],)).fetchall()
        by_id = {row['call_id']: row for row in rows}
        slots = {slot['call_id']: slot for slot in conn.execute('SELECT call_id,observation_json,study_id FROM research_slots WHERE study_id=?', (study['study_id'],))}
        for call_id in covered:
            slot = slots.get(call_id)
            attempt = by_id.get(call_id)
            if slot is None or attempt is None:
                raise ValueError('covered attempt is outside this study')
            if attempt['usage_estimate'] is None or attempt['actual_charge'] is not None or attempt['status'] not in ('usage_estimated', 'usage_estimated_held'):
                raise ValueError('unknown or invoiced usage cannot be covered')
            if not slot['observation_json'] or hashes[call_id] != canonical_hash(json.loads(slot['observation_json'])):
                raise ValueError('attempt evidence hash mismatch')
        expected_tokens = {'input_tokens': 0, 'output_tokens': 0}
        for call_id in covered:
            observed = json.loads(slots[call_id]['observation_json'])
            for key in expected_tokens:
                value = observed.get(key)
                if type(value) is not int or value < 0:
                    raise ValueError('covered attempt is missing known token counts')
                expected_tokens[key] += value
        if any(type(evidence.get(key)) is not int or evidence[key] != value for key, value in expected_tokens.items()) or type(evidence.get('requests')) is not int or evidence['requests'] != len(covered):
            raise ValueError('aggregate request or token counts do not match covered attempts')
        # Missing cache observability cannot imply a zero cache-write charge.
        # A receipt must fit the frozen tariff's conservative bounds. Known
        # cache breakdowns narrow the range; no estimate alone releases a hold.
        profile = payload['server_profile']
        lower = upper = Decimal(0)
        for call_id in covered:
            observation = json.loads(slots[call_id]['observation_json'])
            usage = observation.get('envelope', {}).get('usage', {})
            details = usage.get('input_tokens_details') or {}
            read = observation.get('cache_read_tokens', details.get('cached_tokens', details.get('cache_read_tokens', usage.get('cache_read_tokens', usage.get('cached_tokens')))))
            write = observation.get('cache_write_tokens', details.get('cache_write_tokens', details.get('cache_creation_tokens', usage.get('cache_write_tokens', usage.get('cache_creation_tokens')))))
            incoming, outgoing = observation['input_tokens'], observation['output_tokens']
            input_rate, output_rate = Decimal(profile['input_usd_per_million']), Decimal(profile['output_usd_per_million'])
            read_rate, write_rate = Decimal(profile['cache_read_usd_per_million']), Decimal(profile['cache_write_usd_per_million'])
            if read is None:
                low_input = Decimal(incoming) * min(input_rate, read_rate)
                high_input = Decimal(incoming) * max(input_rate, read_rate)
            else:
                low_input = high_input = Decimal(incoming-read)*input_rate + Decimal(read)*read_rate
            low_write = Decimal(write)*write_rate if write is not None else Decimal(0)
            high_write = low_write if write is not None else Decimal(incoming)*write_rate
            lower += (low_input + low_write + Decimal(outgoing)*output_rate)/Decimal(1000000)
            upper += (high_input + high_write + Decimal(outgoing)*output_rate)/Decimal(1000000)
        if not lower <= actual <= upper or actual > sum((Decimal(by_id[c]['reserved']) for c in covered), Decimal(0)):
            raise ValueError('aggregate charge does not match frozen tariff bounds')
        receipt = {'kind': 'synthetic-aggregate-usage-v1', 'pilot_id': study['namespace'], 'study_id': study['study_id'],
            'covered_call_ids': sorted(covered), 'requests': len(covered), **expected_tokens,
            'actual_charge_usd': str(actual), 'unit': 'USD',
            'evidence_sha256': canonical_hash(evidence),
            'scope': 'aggregate only; individual request charges not allocated',
            'invoiced_actual_usd': None, 'unknown_reservations_released': False}
        conn.execute('CREATE TABLE IF NOT EXISTS pilot_usage_reconciliations (pilot_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)')
        previous = conn.execute('SELECT receipt_json FROM pilot_usage_reconciliations WHERE pilot_id=?', (study['namespace'],)).fetchone()
        old = json.loads(previous[0]) if previous else None
        batches = old.get('batches', [old]) if old else []
        same = next((batch for batch in batches if batch['covered_call_ids'] == receipt['covered_call_ids']), None)
        if same:
            if same != receipt:
                raise ValueError('confirmed aggregate evidence is immutable')
            return same
        if old and set(old['covered_call_ids']) & set(covered):
            # Exact union rereads are idempotent, never an additional charge.
            if (set(old['covered_call_ids']) == set(covered) and Decimal(old['actual_charge_usd']) == actual
                    and all(old[k] == receipt[k] for k in ('requests', 'input_tokens', 'output_tokens'))):
                return old
            raise ValueError('confirmed aggregate evidence overlap is immutable')
        all_batches = [*batches, receipt]
        combined = {**receipt, 'covered_call_ids': sorted({c for b in all_batches for c in b['covered_call_ids']}),
                    'actual_charge_usd': str(sum((Decimal(b['actual_charge_usd']) for b in all_batches), Decimal(0))),
                    **{k: sum(b[k] for b in all_batches) for k in ('requests', 'input_tokens', 'output_tokens')},
                    'batches': all_batches}
        # Releasing the verified covered holds and recording their aggregate is
        # one transaction; a crash cannot release money without its receipt.
        for call_id in covered:
            conn.execute("UPDATE pilot_attempts SET status='usage_estimated' WHERE call_id=? AND pilot_id=? AND status='usage_estimated_held'", (call_id, study['namespace']))
        conn.execute('INSERT INTO pilot_usage_reconciliations VALUES (?,?) ON CONFLICT(pilot_id) DO UPDATE SET receipt_json=excluded.receipt_json',
                     (study['namespace'], json.dumps(combined, sort_keys=True)))
    return receipt
