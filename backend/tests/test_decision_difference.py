"""Offline difference analysis: synthetic HTTP fixtures, never real model calls."""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys

import httpx
import pytest

from app.storage import Store
from test_gateway import data, config, fixture, complete_pilot

spec = importlib.util.spec_from_file_location('decision_diff', Path(__file__).resolve().parents[2]/'tools/analyze_decision_difference.py')
diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diff)


@pytest.fixture(scope='module')
def sources(data, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp('offline-diff')/'synthetic.sqlite3')
    store.initialize()
    try:
        with pytest.MonkeyPatch.context() as patch:
            cfg = config(data)
            def fixed_weight(request, envelope, number):
                if json.loads(request.content)['text']['format']['name']=='decision_output':
                    output=json.loads(envelope['output'][0]['content'][0]['text'])
                    output['target_weight']=.5
                    envelope['output'][0]['content'][0]['text']=json.dumps(output)
                return httpx.Response(200,json=envelope)
            api, requests = fixture(cfg, patch, fixed_weight)
            report = complete_pilot(store,cfg,data,api)
            assert len(requests)==16
            return copy.deepcopy(report['runs'][2:]), store.path, report
    finally:
        store.close()


def test_exact_finances_distinct_role_work_and_no_input_mutation(sources):
    pair,_,_=sources
    before=diff.digest(pair)
    result=diff.analyze(*pair)
    assert all(v['exact_equal'] for v in result['comparisons'].values())
    assert result['counts']=={'weeks':4,'trades_each':[4,4],'equity_points_each':[20,20]}
    assert {week['convergence_layer'] for week in result['weeks']}=={'model-target'}
    assert result['artifact_audit']['unique_response_ids']==16
    assert result['artifact_audit']['unique_cache_keys']==16
    assert [row['calls'] for row in result['cost_by_strategy_stage']]==[4,4,4,4]
    assert result['new_provider_calls']==0 and result['new_spending_usd']==0
    assert diff.digest(pair)==before


@pytest.mark.parametrize('kind',['replay','run_id','calendar','features','predecessor','raw_output','response_id','cache_key','provider','prompt'])
def test_corrupt_or_wrong_source_evidence_is_rejected(sources,kind):
    pair=copy.deepcopy(sources[0]);single,multi=pair
    if kind=='replay':single['config']['llm_mode']='replay'
    if kind=='run_id':multi['run_id']=single['run_id']
    if kind=='calendar':multi['result']['decisions'][0]['execution_session']='2023-01-10'
    if kind=='features':multi['result']['decisions'][0]['snapshot']['last_close']+=1
    if kind=='predecessor':multi['result']['decisions'][0]['calls'][1]['input']['analysis']['thesis']='tampered'
    if kind=='raw_output':multi['result']['decisions'][0]['calls'][-1]['raw_response']='{"target_weight":true}'
    if kind=='response_id':multi['result']['decisions'][0]['calls'][0]['raw_provider_response']['id']=single['result']['decisions'][0]['calls'][0]['raw_provider_response']['id']
    if kind=='cache_key':multi['result']['decisions'][0]['calls'][-1]['cache_key']=single['result']['decisions'][0]['calls'][0]['cache_key']
    if kind=='provider':multi['result']['decisions'][0]['calls'][0]['provider_attribution']['group']='changed'
    if kind=='prompt':multi['result']['decisions'][0]['calls'][0]['prompt_hash']='0'*64
    with pytest.raises(ValueError):diff.analyze(*pair)


def test_full_precision_detects_changes_hidden_by_ui_and_reports_tolerance(sources):
    pair=copy.deepcopy(sources[0]);multi=pair[1]
    multi['result']['equity'][-1]['equity']+=.000001
    multi['result']['metrics']['final_equity']+=.000001
    multi['metrics']['final_equity']=multi['result']['metrics']['final_equity']
    result=diff.analyze(*pair)
    assert not result['comparisons']['equity_cash_holdings_drawdown']['exact_equal']
    assert not result['comparisons']['equity_cash_holdings_drawdown']['within_tolerance']
    assert round(pair[0]['metrics']['final_equity'],2)==round(multi['metrics']['final_equity'],2)
    tiny=diff.numerical_difference({'shares':1.0},{'shares':1.0+1e-13})
    assert not tiny['exact_equal'] and tiny['within_tolerance']


def test_convergence_layers_do_not_invent_model_or_critic_causality():
    assert diff.classify([.5,1],[.5,1],[None,None],['REJECTED','REJECTED'],True,True,True)=='deterministic-risk-outcome'
    assert diff.classify([None,None],[None,None],[None,None],['SKIPPED','SKIPPED'],True,True,True)=='shared-skip-or-rejection'
    assert diff.classify([.5,.5],[None,None],[None,None],['SKIPPED','SKIPPED'],True,True,True)=='shared-skip-or-rejection'
    assert diff.classify([.5,1],[.5,1],[.5,1],['APPROVED','APPROVED'],False,False,True)=='rounded-performance-only'
    assert diff.classify([.5,1],[.5,1],[.5,1],['APPROVED','APPROVED'],True,True,True)=='fill-or-no-trade-threshold'


def test_readonly_sqlite_checks_exact_sources_and_keeps_aggregate_cost_separate(sources,tmp_path):
    pair,database,report=sources
    report=copy.deepcopy(report)
    path=tmp_path/'fixture.json';path.write_text(json.dumps(report),encoding='utf-8')
    before=database.read_bytes()
    selected,charges=diff.load_sources(path,pair[0]['run_id'],pair[1]['run_id'],database)
    ids=[c['call_id'] for row in pair for d in row['result']['decisions'] for c in d['calls']]
    aggregate={'covered_call_ids':ids,'actual_charge_usd':'0.001','scope':'aggregate-only','unit':'USD',
               'requests':16,'input_tokens':1600,'output_tokens':320}
    result=diff.analyze(*selected,actual_charges=charges,aggregate=aggregate)
    assert sum(row['individually_confirmed_calls'] for row in result['cost_by_strategy_stage'])==1
    assert result['confirmed_actual_aggregate']==aggregate
    assert all(not row['actual_all_calls_confirmed_individually'] for row in result['cost_by_strategy_stage'])
    assert database.read_bytes()==before
    with pytest.raises(ValueError):diff.analyze(*selected,aggregate={**aggregate,'covered_call_ids':ids[:-1]})
    report['runs'][2]['result']['metrics']['final_equity']+=1
    path.write_text(json.dumps(report),encoding='utf-8')
    with pytest.raises(ValueError):diff.load_sources(path,pair[0]['run_id'],pair[1]['run_id'],database)


def test_network_guard_blocks_tcp_udp_and_dns_then_restores():
    original=socket.create_connection
    with diff.offline():
        with pytest.raises(RuntimeError):socket.create_connection(('invalid.example',443))
        with pytest.raises(RuntimeError):socket.getaddrinfo('invalid.example',443)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as channel:
            with pytest.raises(RuntimeError):channel.sendto(b'fixture',('127.0.0.1',9))
    assert socket.create_connection is original


def test_cli_utf8_tariff_and_no_provider_import_or_overwrite(sources,tmp_path):
    pair,database,report=sources
    source=tmp_path/'sources.json';source.write_text(json.dumps(report),encoding='utf-8')
    tariff=tmp_path/'tariff.json';tariff.write_text(json.dumps({'input_usd_per_million':.27,
        'output_usd_per_million':1.35,'note':'合成測試，不含真實資料'},ensure_ascii=False),encoding='utf-8')
    output=tmp_path/'analysis.json'
    command=[sys.executable,str(Path(diff.__file__)),'--source-report',str(source),'--single-id',pair[0]['run_id'],
        '--multi-id',pair[1]['run_id'],'--database',str(database),'--tariff',str(tariff),'--output',str(output)]
    completed=subprocess.run(command,capture_output=True)
    assert completed.returncode==0
    result=json.loads(output.read_bytes())
    assert result['provider_modules_imported']==[] and result['new_provider_calls']==0
    assert all('uncached_tariff_allocation_usd_not_observed_actual' in row for row in result['cost_by_strategy_stage'])
    original=output.read_bytes()
    assert subprocess.run(command,capture_output=True).returncode!=0
    assert output.read_bytes()==original
