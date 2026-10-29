"""Durable first Single decision gate. Re-runs never repeat the probe request."""
import json
from typing import Annotated, Literal

from pydantic import Field

from .agents import AgentWorkflow, replay_decision
from .data import canonical_hash
from .features import snapshots
from .models import Contract


class ProbeReconciliation(Contract):
    manifest_hash: str
    call_id: str
    provider_response_id: str
    provider_group: str
    model_id: str
    actual_charge_usd: Annotated[float, Field(strict=True, ge=0)]
    evidence_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    source: Literal['owner-provided-private-record', 'documented-authorized-readonly-billing']
    key_group_binding_checked: Annotated[bool, Field(strict=True)]
    key_group_binding_evidence_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')] | None = None
    key_group_binding_source: Literal['owner-verified-key-settings','documented-group-binding-readonly','authenticated-readonly-key-settings-ui'] | None = None
    observed_key_group_label: str | None = None
    group_display_prefix: str | None = None
    key_match_scope: Literal['unique-project-row-masked-prefix6-suffix4'] | None = None
    strict_output_and_usage_checked: Annotated[bool, Field(strict=True)]


def first_decision_gate(store, manifest, config, dataset, provider, budget, reconciliation=None, anchor_manifest_hash=None):
    """Return (probe, seeded result or None). No request after a saved attempt."""
    pilot_id=anchor_manifest_hash or manifest['manifest_hash']
    day=manifest['sessions'][0]['decision']
    from datetime import date
    snap=snapshots(dataset)[date.fromisoformat(day)]
    with store.connect() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS pilot_probes (pilot_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        row=conn.execute('SELECT payload FROM pilot_probes WHERE pilot_id=?',(pilot_id,)).fetchone()
    probe=json.loads(row[0]) if row else None
    def persist():
        with store.connect() as conn:
            conn.execute('INSERT INTO pilot_probes VALUES (?,?) ON CONFLICT(pilot_id) DO UPDATE SET payload=excluded.payload',
                         (pilot_id,json.dumps(probe,allow_nan=False)))
    if probe is None:
        if reconciliation is not None:
            raise ValueError('cannot reconcile a probe that has not run')
        probe={'manifest_hash':pilot_id,'decision_session':day,'snapshot':snap.model_dump(mode='json'),
               'status':'STARTED','calls':[],'result':None,'reconciliation':None}
        persist()
        def on_call(call):
            probe['calls']=[call]
            persist() # Input/reservation before transport, completion afterwards.
        try:
            result=AgentWorkflow(config.model_copy(update={'max_attempts_per_role':1}),provider,budget).evaluate(snap,on_call)
            probe.update(result=result,status='RECORDED')
            persist()
        except BaseException:
            probe['status']='INTERRUPTED'
            persist()
            raise
    if probe['status']=='STARTED':
        probe['status']='INTERRUPTED'
        persist()
    record=reconciliation or probe.get('reconciliation')
    if record is None:
        return probe,None
    parsed=ProbeReconciliation.model_validate(record)
    if parsed.key_group_binding_source=='authenticated-readonly-key-settings-ui':
        if (not parsed.group_display_prefix or
            parsed.observed_key_group_label!=parsed.group_display_prefix+' '+parsed.provider_group or
            parsed.key_match_scope!='unique-project-row-masked-prefix6-suffix4'):
            raise ValueError('authenticated UI group label mapping or partial key match scope missing')
    result=probe.get('result') or {}
    calls=result.get('calls') or []
    if len(calls)!=1 or calls[0]['status']!='VALIDATED' or calls[0]['cost_status']!='known':
        raise ValueError('probe requires a valid strict output and reported usage before continuation')
    call=calls[0]
    response_id=call.get('raw_provider_response',{}).get('id')
    if (parsed.manifest_hash!=pilot_id or parsed.call_id!=call['call_id'] or
        not response_id or parsed.provider_response_id!=response_id or
        parsed.provider_group!=manifest['provider_attribution']['group'] or parsed.model_id!=provider.model_id or
        not parsed.key_group_binding_checked or not parsed.key_group_binding_evidence_sha256 or not parsed.key_group_binding_source or
        not parsed.strict_output_and_usage_checked or
        call['input_tokens']>provider.input_bound or call['output_tokens']>provider.settings['max_output_tokens']):
        raise ValueError('probe reconciliation identity, key group, schema or usage mismatch')
    billing=probe.get('billing_reconciliation')
    if billing and parsed.key_group_binding_evidence_sha256==billing['evidence_sha256']:
        raise ValueError('singleton usage evidence does not establish a key group name')
    # Revalidate the saved output/input/prompt before seeding the real Single run.
    original={**result,'snapshot':probe['snapshot'],'status':'APPROVED'}
    replay=replay_decision(snap,original,expected_provider=manifest['provider_attribution'])
    if replay['decision_output']!=result['decision_output'] or replay['reason_codes']:
        raise ValueError('saved probe does not satisfy the current contract')
    normalized=parsed.model_dump(mode='json')
    if probe.get('reconciliation') is not None and probe['reconciliation']!=normalized:
        raise ValueError('confirmed probe charge evidence is immutable')
    from decimal import Decimal
    with store.connect() as conn:
        existing=conn.execute('SELECT actual_charge FROM pilot_attempts WHERE pilot_id=? AND call_id=?',
                              (pilot_id,call['call_id'])).fetchone()
        if existing is None or (existing[0] is not None and Decimal(existing[0])!=Decimal(str(parsed.actual_charge_usd))):
            raise ValueError('confirmed actual charge is immutable')
        changed=conn.execute('UPDATE pilot_attempts SET actual_charge=? WHERE pilot_id=? AND call_id=?',
                     (str(parsed.actual_charge_usd),pilot_id,call['call_id']))
        if changed.rowcount!=1:
            raise ValueError('missing durable probe reservation')
    probe.update(reconciliation=normalized,status='RECONCILED',reconciliation_hash=canonical_hash(normalized))
    persist()
    if budget.summary()['charged_or_reserved_usd']>float(budget.limit):
        raise ValueError('confirmed probe charge exceeds total pilot budget')
    return probe,result
