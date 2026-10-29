"""Synthetic offline preview fixtures; no runtime profile, key or research DB."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.data import DatasetRegistry, canonical_hash, get_calendar, sha256
from app.research_contracts import (
    FrozenStudyPlan, ResearchSettings, ServerResearchProfile, SourceContext, StudyRequest,
)
from app.research_service import PlanningError, plan_study, resolve_profile, verify_plan


@pytest.fixture
def setup_plan(tmp_path):
    days = get_calendar(date(2022, 9, 1), date(2023, 1, 31)).sessions_in_range("2022-09-01", "2023-01-31")
    content = "session_date,open,high,low,close,volume\n" + "".join(
        f"{d.date()},{100+i},{102+i},{99+i},{101+i},1000\n" for i, d in enumerate(days))
    csv = tmp_path / "synthetic-planner.csv"
    csv.write_text(content, encoding="utf-8")
    meta = {"id":"synthetic-planner", "file":csv.name, "sha256":sha256(csv.read_bytes()),
        "kind":"synthetic", "symbol":"SPY", "currency":"USD", "adjustment_mode":"raw", "return_basis":"price-only",
        "rows":len(days), "start_date":str(days[0].date()), "end_date":str(days[-1].date()),
        "source":"generated synthetic fixture", "redistributable":True}
    metadata = tmp_path / "synthetic-planner.metadata.json"
    metadata.write_text(json.dumps(meta), encoding="utf-8")
    context = SourceContext(source_commit="a"*40, source_hash="b"*64, uv_lock_hash="c"*64,
        frontend_lock_hash="d"*64, dependency_versions=(("fixture-engine","1.0"),),
        checked_at=datetime(2023, 2, 1, tzinfo=timezone.utc))
    profile = ServerResearchProfile(profile_id="synthetic-profile", revision=1, provider="kuaiapi",
        model_id="synthetic-model-v1", identity_scope="gateway-reported-label-only", dataset_id=meta["id"],
        dataset_hash=meta["sha256"], dataset_kind="synthetic", max_budget_usd="1", max_attempts=32,
        attempts_per_role=1, input_usd_per_million="0.2", output_usd_per_million="0.8",
        cache_read_usd_per_million="0.01", cache_write_usd_per_million="0.3",
        settings=ResearchSettings(max_output_tokens=1024),
        valid_from=datetime(2023, 1, 31, tzinfo=timezone.utc), expires_at=datetime(2023, 2, 2, tzinfo=timezone.utc),
        expected_source_hash=context.source_hash, expected_uv_lock_hash=context.uv_lock_hash,
        expected_frontend_lock_hash=context.frontend_lock_hash,
        private_evidence=({"kind":"pricing","reference_id":"private-price-marker","sha256":"e"*64},
                          {"kind":"binding","reference_id":"private-binding-marker","sha256":"f"*64}))
    request = StudyRequest(profile_id=profile.profile_id, dataset_id=meta["id"], expected_dataset_hash=meta["sha256"],
        requested_budget_usd="1", requested_max_attempts=32)
    return request, profile, DatasetRegistry([tmp_path]), context


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """Fail if preview adds any provider, DB, worker, credential or socket use."""
    import concurrent.futures
    import httpx
    def forbidden(*args, **kwargs):
        raise AssertionError("side effect forbidden in offline planner")
    for cls in (httpx.Client, concurrent.futures.ThreadPoolExecutor):
        monkeypatch.setattr(cls, "__init__", forbidden)
    # Guard already-loaded execution modules without importing a runtime to test preview.
    for module_name, class_name in (("app.agents","OpenAIProvider"),("app.agents","MockProvider"),
            ("app.gateway","KuaiAPIProvider"),("app.pilot","PilotBudget"),("app.storage","Store")):
        module=sys.modules.get(module_name)
        if module is not None:
            monkeypatch.setattr(getattr(module,class_name),"__init__",forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    for owner, name in ((socket.socket,"connect"),(socket.socket,"connect_ex"),(socket.socket,"sendto"),
                         (socket,"getaddrinfo"),(socket,"create_connection")):
        monkeypatch.setattr(owner, name, forbidden)
    original_getitem = os._Environ.__getitem__
    def guarded_getitem(env, name):
        if str(name).startswith(("OPENAI_", "KUAIAPI_", "LLM_")):
            return forbidden()
        return original_getitem(env, name)
    monkeypatch.setattr(os._Environ, "__getitem__", guarded_getitem)
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        if path.name.startswith(".env") or path.suffix in (".sqlite3", ".db", ".key"):
            return forbidden()
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)


def preview(parts):
    request, profile, registry, context = parts
    return plan_study(request, profile, registry, context=context)


def test_deterministic_common_calendar_inputs_and_bound(setup_plan, monkeypatch):
    a, view = preview(setup_plan)
    b, again = preview(setup_plan)
    assert a == b and view == again
    assert [(s.decision.day,s.execution.day) for s in view.sessions] == [(6,9),(13,17),(20,23),(27,30)]
    assert all(s.warmup_bars >= 60 for s in view.sessions)
    assert view.estimate.single_nominal_calls == 4 and view.estimate.multi_nominal_calls == 12
    assert view.estimate.total_nominal_calls == view.estimate.complete_attempt_ceiling == 16
    assert view.estimate.baseline_calls == view.estimate.baseline_cost_usd == view.estimate.separate_smoke_calls == 0
    # Hand-check: 32768*(0.2+0.3) + 1024*0.8, all per million, across 16 attempts.
    assert view.estimate.reservation_usd_per_attempt == Decimal("0.0172032")
    assert view.estimate.complete_reservation_usd == Decimal("0.2752512")
    assert view.estimate.all_attempts_fit_requested_caps and view.execution_authorized is False
    for d in a.payload["decision_inputs"]:
        assert "snapshot_id" not in d["snapshot"] and "as_of" in d["snapshot"]
        templates = d["role_input_templates"]
        assert templates["single_agent"][0]["features"] == templates["multi_agent"][0]["features"]
        assert [x["validated_predecessors"] for x in templates["multi_agent"]] == [[],["analysis"],["analysis","critic"]]
        assert "decision_session" not in templates["single_agent"][0]["features"]
    # Even a future random snapshot identity must not affect the canonical payload.
    from app import research_service as service
    original = service.snapshots
    monkeypatch.setattr(service, "snapshots", lambda ds:{d:s.model_copy(update={"snapshot_id":uuid4()}) for d,s in original(ds).items()})
    assert preview(setup_plan)[0] == a


@pytest.mark.parametrize("field,value", [
    ("endpoint","https://elsewhere.invalid"),("model_id","unlisted-model"),("key","synthetic-secret"),
    ("input_usd_per_million",0),("multiplier",0.01),("provider_group","private-marker"),
    ("approved",True),("settings",{}),("max_attempts_per_role",2),
])
def test_client_cannot_inject_server_truth(setup_plan, field, value):
    data = setup_plan[0].model_dump(mode="json");data[field] = value
    with pytest.raises(ValidationError):StudyRequest.model_validate(data)


def test_private_profile_allowlist_and_safe_projection(setup_plan):
    request, profile, registry, context = setup_plan
    assert resolve_profile(request.profile_id,{profile.profile_id:profile}) is profile
    with pytest.raises(PlanningError,match="PROFILE_NOT_ALLOWLISTED"):resolve_profile("unknown",{profile.profile_id:profile})
    frozen, view = preview(setup_plan)
    safe = view.model_dump_json()
    assert "private-price-marker" not in safe and "private-binding-marker" not in safe
    for field in ("private_evidence","server_profile","source_hash","expected_source_hash","timeout_policy","pricing_unit"):
        assert field not in view.model_dump()
    assert frozen.payload["server_profile"]["private_evidence"]
    detached = frozen.payload;detached["server_profile"]["revision"] = 999
    assert frozen.payload["server_profile"]["revision"] == 1
    with pytest.raises(ValidationError):FrozenStudyPlan(canonical_payload=json.dumps(detached),manifest_hash=frozen.manifest_hash)
    verify_plan(frozen,request,profile,registry,context=context)


@pytest.mark.parametrize("owner,field,value", [
    ("request","initial_cash",200000.0),("request","fee_bps",6.0),
    ("request","slippage_bps",7.0),("request","requested_budget_usd",Decimal("0.8")),
    ("request","requested_max_attempts",31),
    ("profile","revision",2),("profile","model_id","synthetic-model-v2"),
    ("profile","attempts_per_role",2),("profile","cache_read_usd_per_million",Decimal("0.02")),
    ("profile","cache_write_usd_per_million",Decimal("0.4")),
    ("context","source_commit","f"*40),("context","dependency_versions",(("fixture-engine","1.1"),)),
])
def test_meaningful_frozen_changes_rehash_and_refuse_old_plan(setup_plan, owner, field, value):
    frozen,_ = preview(setup_plan)
    parts = list(setup_plan);i={"request":0,"profile":1,"context":3}[owner]
    parts[i] = parts[i].model_copy(update={field:value})
    changed,_ = preview(parts)
    assert changed.manifest_hash != frozen.manifest_hash
    with pytest.raises(PlanningError,match="FROZEN_PLAN_DRIFT"):
        verify_plan(frozen,parts[0],parts[1],parts[2],context=parts[3])


def test_settings_timeout_evidence_and_clock_are_frozen(setup_plan):
    from app.http_policy import HTTPTimeoutPolicy
    frozen,_ = preview(setup_plan)
    request,profile,registry,context = setup_plan
    variants = [profile.model_copy(update={"settings":ResearchSettings(max_output_tokens=2048)}),
                profile.model_copy(update={"timeout_policy":HTTPTimeoutPolicy(read=60.0)}),
                profile.model_copy(update={"private_evidence":(profile.private_evidence[0].model_copy(update={"sha256":"a"*64}),profile.private_evidence[1])})]
    for variant in variants:
        assert plan_study(request,variant,registry,context=context)[0].manifest_hash != frozen.manifest_hash
    next_clock=context.model_copy(update={"checked_at":datetime(2023,2,1,1,tzinfo=timezone.utc)})
    assert plan_study(request,profile,registry,context=next_clock)[0].manifest_hash != frozen.manifest_hash


@pytest.mark.parametrize("owner,field,value,code", [
    ("request","profile_id","unknown","PROFILE_NOT_ALLOWLISTED"),
    ("request","expected_dataset_hash","0"*64,"DATASET_BINDING_MISMATCH"),
    ("context","source_hash","0"*64,"SOURCE_OR_LOCK_DRIFT"),
    ("context","uv_lock_hash","0"*64,"SOURCE_OR_LOCK_DRIFT"),
    ("context","frontend_lock_hash","0"*64,"SOURCE_OR_LOCK_DRIFT"),
    ("context","engine_version","unknown","VERSION_DRIFT"),
    ("context","checked_at",datetime(2023,2,2,tzinfo=timezone.utc),"PROFILE_OUTSIDE_VALIDITY"),
    ("context","checked_at",datetime(2023,1,30,tzinfo=timezone.utc),"PROFILE_OUTSIDE_VALIDITY"),
    ("request","requested_max_attempts",64,"REQUESTED_CAP_EXCEEDS_PROFILE"),
    ("request","requested_budget_usd",Decimal("2"),"REQUESTED_CAP_EXCEEDS_PROFILE"),
    ("request","start_date",date(2022,12,31),"PERIOD_NOT_ALLOWLISTED"),
])
def test_drift_and_expiry_reject_before_data_or_side_effects(setup_plan,owner,field,value,code,monkeypatch):
    parts=list(setup_plan);i={"request":0,"profile":1,"context":3}[owner]
    parts[i]=parts[i].model_copy(update={field:value})
    def no_data(*args):raise AssertionError("rejection must precede data load")
    monkeypatch.setattr(parts[2],"load",no_data)
    with pytest.raises(PlanningError,match=code):preview(parts)


@pytest.mark.parametrize("damage",["bytes","metadata","gap","duplicate","nan","adjusted","kind","return-basis","large-field"])
def test_dataset_revalidation_does_not_trust_discovery(setup_plan,damage):
    request,profile,registry,context=setup_plan
    root=registry.directories[0];meta_path=root/'synthetic-planner.metadata.json';csv=root/'synthetic-planner.csv'
    meta=json.loads(meta_path.read_text());lines=csv.read_text().splitlines()
    if damage=='bytes':csv.write_text(csv.read_text()+'\n')
    elif damage=='metadata':meta['rows']+=1
    elif damage=='adjusted':meta['adjustment_mode']='adjusted'
    elif damage=='kind':meta['kind']='historical'
    elif damage=='return-basis':meta['return_basis']='total-return'
    else:
        if damage=='gap':del lines[2]
        elif damage=='duplicate':lines.insert(2,lines[1])
        elif damage=='nan':lines[1]=lines[1].replace(',100,',',NaN,')
        else:lines[1]=lines[1].replace(',100,',','+'1'*150000+',')
        csv.write_text('\n'.join(lines)+'\n');meta['sha256']=sha256(csv.read_bytes())
        request=request.model_copy(update={'expected_dataset_hash':meta['sha256']});profile=profile.model_copy(update={'dataset_hash':meta['sha256']})
    meta_path.write_text(json.dumps(meta))
    assert registry.list()  # Discovery is deliberately not proof of validity.
    with pytest.raises(PlanningError):plan_study(request,profile,registry,context=context)


def test_insufficient_warmup_no_execution_and_insufficient_requested_caps(setup_plan):
    request,profile,registry,context=setup_plan
    early=profile.model_copy(update={'allowed_start':date(2022,9,1),'allowed_end':date(2022,9,30)})
    with pytest.raises(PlanningError,match='CALENDAR_OR_WARMUP_INVALID'):
        plan_study(request.model_copy(update={'start_date':early.allowed_start,'end_date':early.allowed_end}),early,registry,context=context)
    with pytest.raises(PlanningError,match='CALENDAR_OR_WARMUP_INVALID'):
        plan_study(request.model_copy(update={'start_date':date(2023,1,27),'end_date':date(2023,1,27)}),profile,registry,context=context)
    small=request.model_copy(update={'requested_max_attempts':8,'requested_budget_usd':Decimal('0.01')})
    _,view=plan_study(small,profile,registry,context=context)
    assert view.estimate.complete_attempt_ceiling==16 and view.estimate.capped_attempt_ceiling==8
    assert not view.estimate.all_attempts_fit_requested_caps
    assert 'REQUESTED_CAPS_CANNOT_COVER_COMPLETE_ATTEMPT_PLAN' in view.warnings
    assert not view.execution_authorized


@pytest.mark.parametrize("field,value",[("input_usd_per_million",None),("output_usd_per_million",0),
    ("cache_write_usd_per_million",None),("input_usd_per_million",float('nan')),
    ("attempts_per_role",None),("attempts_per_role",True)])
def test_missing_prices_or_retry_policy_never_become_zero_estimate(setup_plan,field,value):
    data=setup_plan[1].model_dump(mode='json');data[field]=value
    with pytest.raises(ValidationError):ServerResearchProfile.model_validate(data)


def test_revalidated_objects_and_nonfinite_client_values(setup_plan):
    request,profile,registry,context=setup_plan
    with pytest.raises(ValidationError):
        plan_study(request.model_copy(update={'fee_bps':float('nan')}),profile,registry,context=context)
    with pytest.raises(ValidationError):
        plan_study(request,profile.model_copy(update={'settings':{'endpoint':'https://elsewhere.invalid'}}),registry,context=context)
    with pytest.raises(ValidationError):
        StudyRequest.model_validate({**request.model_dump(mode='json'),'end_date':'2025-01-31'})
    with pytest.raises(ValidationError):
        StudyRequest.model_validate({**request.model_dump(mode='json'),'max_exposure':True})


def test_ambient_decimal_precision_cannot_change_cost_bound(setup_plan):
    original,_=preview(setup_plan)
    with localcontext() as arithmetic:
        arithmetic.prec=6
        assert preview(setup_plan)[0]==original


def test_future_perturbation_changes_data_not_prior_numeric_inputs(setup_plan):
    request,profile,registry,context=setup_plan
    original,_=preview(setup_plan)
    root=registry.directories[0];csv=root/'synthetic-planner.csv';meta_path=root/'synthetic-planner.metadata.json'
    lines=csv.read_text().splitlines()
    for index,line in enumerate(lines[1:],1):
        parts=line.split(',')
        if parts[0]>='2023-01-20':
            parts[1:5]=[str(float(value)*2) for value in parts[1:5]]
            lines[index]=','.join(parts)
    csv.write_text('\n'.join(lines)+'\n')
    digest=sha256(csv.read_bytes());meta=json.loads(meta_path.read_text());meta['sha256']=digest;meta_path.write_text(json.dumps(meta))
    changed,_=plan_study(request.model_copy(update={'expected_dataset_hash':digest}),
        profile.model_copy(update={'dataset_hash':digest}),registry,context=context)
    left=original.payload['decision_inputs'];right=changed.payload['decision_inputs']
    assert original.manifest_hash!=changed.manifest_hash
    assert [x['numeric_input_hash'] for x in left[:2]]==[x['numeric_input_hash'] for x in right[:2]]
    assert left[-1]['numeric_input_hash']!=right[-1]['numeric_input_hash']


def test_no_features_calculated_after_requested_end(setup_plan,monkeypatch):
    from app import research_service as service
    request,profile,registry,context=setup_plan
    original=service.snapshots
    def bounded(dataset):
        assert max(b.session_date for b in dataset.bars)<=date(2023,1,26)
        return original(dataset)
    monkeypatch.setattr(service,'snapshots',bounded)
    _,view=plan_study(request.model_copy(update={'end_date':date(2023,1,26)}),profile,registry,context=context)
    assert len(view.sessions)==3


@pytest.mark.parametrize('label',['/fixture/model','fixture://model'])
def test_server_model_identifier_cannot_be_filesystem_or_endpoint(setup_plan,label):
    with pytest.raises(ValidationError):
        ServerResearchProfile.model_validate({**setup_plan[1].model_dump(mode='json'),'model_id':label})


def test_prompt_source_and_retry_ceiling_are_explicit(setup_plan,monkeypatch):
    from app import research_service as service
    request,profile,registry,context=setup_plan
    original,_=preview(setup_plan)
    monkeypatch.setitem(service.INSTRUCTIONS,'decision',service.INSTRUCTIONS['decision']+' Synthetic fixture instruction.')
    assert preview(setup_plan)[0].manifest_hash!=original.manifest_hash
    revised=context.model_copy(update={'source_hash':'0'*64})
    revised_profile=profile.model_copy(update={'expected_source_hash':'0'*64,'attempts_per_role':2})
    frozen,view=plan_study(request,revised_profile,registry,context=revised)
    assert frozen.manifest_hash!=original.manifest_hash
    assert view.estimate.complete_attempt_ceiling==32 and view.attempts_per_role==2
    assert view.estimate.complete_reservation_usd==Decimal('0.5505024')


def test_import_in_fresh_process_never_initializes_runtime():
    code = r'''
import os,sys,socket,sqlite3,concurrent.futures
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'backend'))
def forbidden(*args,**kwargs):raise AssertionError('runtime side effect during planner import')
get=os._Environ.__getitem__
def guarded(env,name):
    if str(name).startswith(('OPENAI_','KUAIAPI_','LLM_')):return forbidden()
    return get(env,name)
os._Environ.__getitem__=guarded
sqlite3.connect=forbidden
concurrent.futures.ThreadPoolExecutor.__init__=forbidden
socket.socket.connect=forbidden;socket.socket.connect_ex=forbidden
socket.create_connection=forbidden;socket.getaddrinfo=forbidden
original_open=Path.open
def guarded_open(path,*args,**kwargs):
    if path.name.startswith('.env') or path.suffix in ('.sqlite3','.db','.key'):return forbidden()
    return original_open(path,*args,**kwargs)
Path.open=guarded_open
import httpx
httpx.Client.__init__=forbidden
from app.research_service import plan_study
assert not any(name in sys.modules for name in ('app.agents','app.gateway','app.pilot','app.storage','app.experiments'))
print('IMPORTED_OFFLINE')
'''
    completed=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,timeout=30)
    assert completed.returncode==0,completed.stderr
    assert completed.stdout.strip()=='IMPORTED_OFFLINE'
