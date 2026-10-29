"""New diagnostic policy fixtures; reuse existing strict validators/adapters."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from app.agents import OpenAIProvider
from app.gateway import KuaiAPIProvider
from app.pilot import PilotBudget
from app.storage import Store

spec=importlib.util.spec_from_file_location('contract_check',Path(__file__).resolve().parents[2]/'tools/check_provider_contract.py')
check=importlib.util.module_from_spec(spec);spec.loader.exec_module(check)


@pytest.fixture
def store(tmp_path):
    value=Store(tmp_path/'fixture.sqlite3');value.initialize()
    yield value
    value.close()


def replace_text(call,text):
    call['raw_response']=text
    call['raw_provider_response']['output'][0]['content'][0]['text']=text


def test_new_protocol_collects_known_fee_format_failures_through_six_without_retry(store):
    calls=check.synthetic_calls();before=copy.deepcopy(calls)
    replace_text(calls[1],'{"rationale":"synthetic unterminated')
    for index,change in [(2,{'target_weight':.7}),(3,{'confidence':float('nan')}),(4,{'extra_field':'forbidden'})]:
        output=json.loads(calls[index]['raw_response']);output.update(change);replace_text(calls[index],json.dumps(output))
    result=check.collect_fixtures(store,check.fixture_manifest(),calls)
    assert result['completed_slots']==6 and result['simulated_budget']['attempts']==6
    assert [r['format_pass'] for r in result['records']]==[True,False,False,False,False,True]
    assert not result['provisional_acceptance'] and not any(r['hard_stop_codes'] for r in result['records'])
    assert result['sequence']==['A','B','B','A','A','B']
    assert calls[1]['raw_response'].endswith('unterminated') # No completion/repair.
    assert before[0]==calls[0]


@pytest.mark.parametrize('contradiction',['message_status','format_type','strict_false','incomplete_details'])
def test_contradictory_protocol_fails_even_when_decision_json_valid(contradiction):
    call=check.synthetic_calls()[0];env=call['raw_provider_response']
    if contradiction=='message_status':env['output'][0]['status']='incomplete'
    if contradiction=='format_type':env['text']['format']['type']='text'
    if contradiction=='strict_false':env['text']['format']['strict']=False
    if contradiction=='incomplete_details':env['incomplete_details']={'reason':'max_output_tokens'}
    result=check.inspect_call(call,'synthetic-model')
    assert result['schema_valid'] and not result['format_pass']
    assert result['decision_output_eligible'] is None and result['hard_stop_codes']==[]


@pytest.mark.parametrize('tier',['auto','default','standard',None,'unmapped-fixture-tier'])
def test_tier_observation_alone_is_not_format_failure_or_pricing_proof(tier):
    call=check.synthetic_calls()[0];call['raw_provider_response']['service_tier']=tier
    result=check.inspect_call(call,'synthetic-model')
    assert result['format_pass'] and result['hard_stop_codes']==[]
    assert result['service_tier_observation']['proves_fast_price_or_model_change'] is False
    assert ('TIER_MAPPING_NOT_ESTABLISHED' in result['observation_warnings'])==(tier not in ('auto','default'))


@pytest.mark.parametrize('kind',['missing','inconsistent_total','inconsistent_record','unknown_charge'])
def test_unknown_usage_preserves_full_reservation_and_stopped_resume(store,kind,monkeypatch):
    calls=check.synthetic_calls();env=calls[1]['raw_provider_response']
    if kind=='missing':env.pop('usage')
    if kind=='inconsistent_total':env['usage']['total_tokens']=999
    if kind=='inconsistent_record':calls[1]['input_tokens']=101
    if kind=='unknown_charge':calls[1]['cost_status']='unknown'
    m=check.fixture_manifest();first=check.collect_fixtures(store,m,calls)
    assert first['completed_slots']==2 and first['not_run_slots']==4
    assert first['simulated_budget']['unknown_reserved_usd']==pytest.approx(.02267136)
    assert not first['provisional_acceptance']
    if kind=='unknown_charge':
        assert first['records'][1]['usage_known'] and not first['records'][1]['estimated_cost_known']
    monkeypatch.setattr(check,'inspect_call',lambda *a:pytest.fail('resume reprocessed a stopped/uncertain request'))
    again=check.collect_fixtures(store,m,check.synthetic_calls())
    assert again['simulated_budget']['attempts']==2 and again['simulated_budget']['unknown_reserved_usd']==pytest.approx(.02267136)


def test_interrupted_reservation_is_not_reissued(store):
    m=check.fixture_manifest();budget=PilotBudget(store,m)
    attempt='interrupted-fixture';budget.reserve(attempt,.02267136)
    with store.connect() as conn:
        conn.execute('CREATE TABLE acceptance_records (slot INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('INSERT INTO acceptance_records VALUES (?,?)',(0,json.dumps({'status':'RUNNING','attempt_id':attempt})))
    result=check.collect_fixtures(store,m,check.synthetic_calls())
    assert result['records'][0]['status']=='UNCERTAIN_INTERRUPTED'
    assert result['simulated_budget']['attempts']==1 and result['simulated_budget']['unknown_reserved_usd']>0


def test_acceptance_and_idempotence_are_not_universal_reliability(store,monkeypatch):
    m=check.fixture_manifest();first=check.collect_fixtures(store,m,check.synthetic_calls())
    assert first['provisional_acceptance'] and first['completed_slots']==6
    monkeypatch.setattr(check,'inspect_call',lambda *a:pytest.fail('completed slot was repeated'))
    again=check.collect_fixtures(store,m,check.synthetic_calls())
    assert again==first and again['new_provider_calls']==again['new_spend_usd']==0


def test_original_ablation_or_pilot_accounting_cannot_be_reused(store):
    foreign={'kind':'foreign-fixture','configuration':{'total_budget_usd':.5,'max_calls':16}}
    from app.data import canonical_hash
    foreign['manifest_hash']=canonical_hash(foreign);PilotBudget(store,foreign)
    with pytest.raises(ValueError):check.collect_fixtures(store,check.fixture_manifest(),check.synthetic_calls())
    live=check.fixture_manifest();live['fixture_only']=False
    with pytest.raises(ValueError):check.collect_fixtures(store,live,check.synthetic_calls())


def test_incomplete_adapter_policy_refusal_and_tool_drift_are_distinct():
    call=check.synthetic_calls()[0];env=call['raw_provider_response'];env['status']='incomplete';env['incomplete_details']={'reason':'max_output_tokens'};env['output'][0]['status']='incomplete'
    call['raw_response']='' # Existing adapter intentionally suppresses text; envelope retains it.
    result=check.inspect_call(call,'synthetic-model')
    assert result['raw_envelope_text_correspondence'] and not result['format_pass']
    assert 'RESPONSE_NOT_COMPLETED' in result['failure_codes']
    refusal=check.synthetic_calls()[0];refusal['raw_response']='';refusal['raw_provider_response']['output'][0]['content']=[{'type':'refusal','refusal':'Synthetic refusal.'}]
    assert 'REFUSAL_NOT_DECISION' in check.inspect_call(refusal,'synthetic-model')['failure_codes']
    tool=check.synthetic_calls()[0];tool['raw_provider_response']['output'].append({'type':'function_call','name':'not_requested'})
    assert 'UNREQUESTED_TOOL_OR_OUTPUT_TYPE' in check.inspect_call(tool,'synthetic-model')['hard_stop_codes']


def test_default_cli_is_offline_without_keys_providers_or_live_execute(monkeypatch,capsys):
    def forbidden(*args,**kwargs):pytest.fail('default run constructed a provider')
    monkeypatch.setattr(OpenAIProvider,'__init__',forbidden);monkeypatch.setattr(KuaiAPIProvider,'__init__',forbidden)
    monkeypatch.setattr(sys,'argv',['check_provider_contract.py']);check.main()
    result=json.loads(capsys.readouterr().out)
    assert result['offline'] and result['new_provider_calls']==result['new_spend_usd']==0
    assert result['live_execute_mode_available'] is False


def test_missing_http_observation_is_not_fabricated_as_200():
    call=check.synthetic_calls()[0]
    result=check.inspect_call(call,'synthetic-model')
    assert result['http_status_observed'] is None and result['format_pass']
    assert 'HTTP_STATUS_NOT_RECORDED' in result['observation_warnings']
    call['http_status']=401
    assert 'TRANSPORT_HTTP_ERROR' in check.inspect_call(call,'synthetic-model')['hard_stop_codes']
