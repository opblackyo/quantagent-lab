"""New synthetic scratch databases only; no provider/key/billing or execution."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.data import canonical_hash
from app.pilot import PilotBudget
from app.research_service import plan_study
from app.research_store import (
    PlanningInputs, ResearchStore, ServerAuthorization, StudyStorageError,
    migrate_schema, namespace_for,
)
from app.storage import Store
from test_research_planner import setup_plan


@pytest.fixture(autouse=True)
def no_application_io(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('provider/key/network/worker forbidden in study storage')
    from app.agents import OpenAIProvider,MockProvider
    from app.gateway import KuaiAPIProvider
    for cls in (OpenAIProvider,MockProvider,KuaiAPIProvider,httpx.Client,ThreadPoolExecutor):
        monkeypatch.setattr(cls,'__init__',forbidden)
    for owner,name in ((socket.socket,'connect'),(socket.socket,'connect_ex'),(socket,'getaddrinfo'),(socket,'create_connection')):
        monkeypatch.setattr(owner,name,forbidden)
    original=os._Environ.__getitem__
    def guarded(env,name):
        if str(name).startswith(('OPENAI_','KUAIAPI_','LLM_')):return forbidden()
        return original(env,name)
    monkeypatch.setattr(os._Environ,'__getitem__',guarded)


@pytest.fixture
def bundle(setup_plan,tmp_path):
    request,profile,registry,context=setup_plan
    inputs=PlanningInputs(request,profile,registry,context)
    plan,_=plan_study(request,profile,registry,context=context)
    store=ResearchStore.create_new(tmp_path/'new-study.sqlite3')
    yield store,plan,inputs,context.checked_at
    store.close()


def state_digest(store):
    with store._read() as conn:
        tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return canonical_hash({'schema':[tuple(r) for r in conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name')],
            'version':conn.execute('PRAGMA user_version').fetchone()[0],
            'data':{t:[tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in tables}})


def auth_for(study_id,plan,inputs,authorization_id='synthetic-approval',key='synthetic-key',cohort='synthetic-cohort'):
    return ServerAuthorization(authorization_id=authorization_id,study_id=study_id,manifest_hash=plan.manifest_hash,
        budget_namespace=namespace_for(study_id,plan.manifest_hash),profile_id=inputs.profile.profile_id,
        profile_revision=inputs.profile.revision,source_hash=inputs.context.source_hash,key_ref=key,cohort_ref=cohort,
        cap_usd=inputs.request.requested_budget_usd,max_attempts=inputs.request.requested_max_attempts,
        valid_from=inputs.profile.valid_from,expires_at=inputs.profile.expires_at,evidence_sha256='a'*64)


def frozen_study(bundle,key='synthetic-click'):
    store,plan,inputs,at=bundle
    study=store.register(plan,inputs,idempotency_key=key);store.freeze(study,inputs,at=at)
    return study


def admitted(bundle):
    store,plan,inputs,at=bundle;study=frozen_study(bundle)
    auth=auth_for(study,plan,inputs);store.record_authorization(auth,at=at);store.start(study,auth.authorization_id,inputs,at=at)
    return study,auth


def first_slot(store,study):
    with store._read() as conn:
        row,_=store._study(conn,study)
        return store._candidate_slots(conn,row)[0]['slot_id']


def test_explicit_new_schema_idempotence_and_no_legacy_repair(bundle):
    store,plan,inputs,at=bundle
    before=state_digest(store)
    with store.connect() as conn:migrate_schema(conn)
    assert state_digest(store)==before
    path=store.path;store.close();reopened=ResearchStore.open_existing(path)
    try:assert state_digest(reopened)==before
    finally:reopened.close()


def test_legacy_and_missing_read_paths_never_initialize_or_migrate(tmp_path):
    missing=tmp_path/'absent'/'missing.sqlite3';store=ResearchStore(missing)
    with pytest.raises(StudyStorageError):store.get_study('unknown')
    assert not missing.parent.exists()
    with pytest.raises(sqlite3.OperationalError):ResearchStore.open_existing(missing)
    assert not missing.exists()
    with pytest.raises(StudyStorageError):store.initialize()
    legacy=Store(tmp_path/'legacy.sqlite3');legacy.initialize();legacy.close()
    before=(tmp_path/'legacy.sqlite3').read_bytes()
    with pytest.raises(FileExistsError):ResearchStore.create_new(legacy.path)
    with pytest.raises(StudyStorageError):ResearchStore.open_existing(legacy.path)
    assert legacy.path.read_bytes()==before


@pytest.mark.parametrize('fault_point',['AFTER_SCHEMA','AFTER_VERSION'])
def test_transactional_migration_fault_and_future_version_refusal(tmp_path,fault_point):
    path=tmp_path/'fault.sqlite3'
    def fail(point):
        if point==fault_point:raise RuntimeError('synthetic fault')
    with pytest.raises(RuntimeError):ResearchStore.create_new(path,migration_fault=fail)
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==0
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()
        migrate_schema(conn)
        conn.execute('PRAGMA user_version=2');conn.commit()
    before=path.read_bytes()
    with pytest.raises(StudyStorageError):ResearchStore.open_existing(path)
    with sqlite3.connect(path) as conn:
        with pytest.raises(StudyStorageError):migrate_schema(conn)
    assert path.read_bytes()==before


def test_owned_process_exclusion_and_settled_run_preservation(bundle):
    store,_,_,_=bundle
    from app.models import ExperimentConfig
    run,_=store.create(ExperimentConfig(),'synthetic-child');store.update(run,status='COMPLETED',result={'synthetic':True})
    before=state_digest(store)
    with pytest.raises(RuntimeError):ResearchStore.open_existing(store.path)
    assert state_digest(store)==before
    path=store.path;store.close();other=ResearchStore.open_existing(path)
    try:assert other.get(run,True)['result']=={'synthetic':True}
    finally:other.close()


def test_concurrent_register_and_start_are_single_admission(bundle):
    store,plan,inputs,at=bundle
    values=[];errors=[];barrier=threading.Barrier(2)
    def register():
        try:barrier.wait();values.append(store.register(plan,inputs,idempotency_key='same-click'))
        except BaseException as exc:errors.append(type(exc).__name__)
    threads=[threading.Thread(target=register) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert not errors and len(set(values))==1
    study=values[0];store.freeze(study,inputs,at=at);store.freeze(study,inputs,at=at)
    auth=auth_for(study,plan,inputs);store.record_authorization(auth,at=at)
    barrier=threading.Barrier(2)
    def start():
        try:barrier.wait();store.start(study,auth.authorization_id,inputs,at=at)
        except BaseException as exc:errors.append(type(exc).__name__)
    threads=[threading.Thread(target=start) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert not errors and store.get_study(study)['state']=='QUEUED'
    with store._read() as conn:
        assert conn.execute('SELECT count(*) FROM research_slots').fetchone()[0]==16
        assert conn.execute('SELECT count(*) FROM pilot_budgets').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM research_admissions').fetchone()[0]==1


def test_idempotency_payload_conflict_and_approval_not_browser_truth(bundle):
    store,plan,inputs,at=bundle;study=frozen_study(bundle)
    changed=replace(inputs,request=inputs.request.model_copy(update={'fee_bps':6.0}))
    new_plan,_=plan_study(changed.request,changed.profile,changed.registry,context=changed.context)
    with pytest.raises(StudyStorageError,match='IDEMPOTENCY_CONFLICT'):store.register(new_plan,changed,idempotency_key='synthetic-click')
    with pytest.raises(StudyStorageError,match='SERVER_AUTHORIZATION_REQUIRED'):store.start(study,'browser-approved',inputs,at=at)
    auth=auth_for(study,plan,inputs)
    with pytest.raises(ValidationError):ServerAuthorization.model_validate({**auth.model_dump(mode='json'),'approved':True})
    assert store.get_study(study)['cost'] is None


@pytest.mark.parametrize('field,value',[('cap_usd',Decimal('0.5')),('max_attempts',31),('manifest_hash','0'*64),
    ('budget_namespace','0'*64),('profile_id','wrong-profile'),('profile_revision',2),('source_hash','0'*64)])
def test_authorization_exact_scope_cannot_be_widened_or_reused(bundle,field,value):
    store,plan,inputs,at=bundle;study=frozen_study(bundle);before=state_digest(store)
    auth=auth_for(study,plan,inputs).model_copy(update={field:value})
    with pytest.raises(StudyStorageError):store.record_authorization(auth,at=at)
    assert state_digest(store)==before


@pytest.mark.parametrize('label,key,cohort',[('same-key','synthetic-key','other-cohort'),('same-cohort','other-key','synthetic-cohort')])
def test_cross_study_approval_and_active_key_cohort_blocked(bundle,label,key,cohort):
    store,plan,inputs,at=bundle;first,auth=admitted(bundle)
    second=frozen_study(bundle,'second-study')
    with pytest.raises(StudyStorageError):store.record_authorization(auth.model_copy(update={'study_id':second}),at=at)
    candidate=auth_for(second,plan,inputs,label,key,cohort)
    store.record_authorization(candidate,at=at)
    with pytest.raises(StudyStorageError):store.start(second,label,inputs,at=at)
    with pytest.raises(StudyStorageError):store.record_authorization(candidate.model_copy(update={'authorization_id':'replacement'}),at=at)
    assert store.get_study(first)['cost']['attempts']==0
    assert store.get_study(second)['cost'] is None


def test_expiry_source_profile_reject_and_read_projection(bundle):
    store,plan,inputs,at=bundle;study,auth=admitted(bundle)
    for changed in (replace(inputs,context=inputs.context.model_copy(update={'source_hash':'0'*64})),
                    replace(inputs,profile=inputs.profile.model_copy(update={'revision':2}))):
        with pytest.raises(ValueError):store.start(study,auth.authorization_id,changed,at=at)
    with pytest.raises(StudyStorageError):store.start(study,auth.authorization_id,inputs,at=auth.expires_at)
    before=state_digest(store)
    for _ in range(3):
        safe=json.dumps(store.get_study(study))
        store.resume_eligibility(study,inputs,at=at)
        assert all(marker not in safe for marker in ('synthetic-approval','synthetic-key','synthetic-cohort','private-price-marker','private-binding-marker','plan_json','namespace','payload_json'))
    assert state_digest(store)==before


@pytest.mark.parametrize('point',['AFTER_REGISTER','AFTER_FREEZE_SLOTS','AFTER_ADMISSION'])
def test_registration_freeze_admission_faults_rollback(bundle,monkeypatch,point):
    store,plan,inputs,at=bundle
    if point!='AFTER_REGISTER':study=store.register(plan,inputs,idempotency_key='fault')
    if point=='AFTER_ADMISSION':
        store.freeze(study,inputs,at=at);auth=auth_for(study,plan,inputs);store.record_authorization(auth,at=at)
    before=state_digest(store)
    def fail(actual):
        if actual==point:raise RuntimeError('synthetic crash')
    monkeypatch.setattr(store,'_checkpoint',fail)
    with pytest.raises(RuntimeError):
        if point=='AFTER_REGISTER':store.register(plan,inputs,idempotency_key='fault')
        elif point=='AFTER_FREEZE_SLOTS':store.freeze(study,inputs,at=at)
        else:store.start(study,auth.authorization_id,inputs,at=at)
    assert state_digest(store)==before


@pytest.mark.parametrize('point',['AFTER_RESERVATION','AFTER_DISPATCH','AFTER_OBSERVATION'])
def test_attempt_boundaries_atomic_no_lost_hold(bundle,monkeypatch,point):
    store,plan,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at);slot=first_slot(store,study)
    if point!='AFTER_RESERVATION':store.reserve_slot(slot,inputs,at=at)
    if point=='AFTER_OBSERVATION':store.mark_dispatched(slot,inputs,at=at)
    before=state_digest(store)
    def fail(actual):
        if actual==point:raise RuntimeError('synthetic crash')
    monkeypatch.setattr(store,'_checkpoint',fail)
    with pytest.raises(RuntimeError):
        if point=='AFTER_RESERVATION':store.reserve_slot(slot,inputs,at=at)
        elif point=='AFTER_DISPATCH':store.mark_dispatched(slot,inputs,at=at)
        else:store.record_observation(slot,'SUCCEEDED','c'*64,at=at,usage_estimate=Decimal('0.001'))
    assert state_digest(store)==before


@pytest.mark.parametrize('dispatched',[False,True])
def test_interrupted_reservation_or_send_never_reissued_and_hold_survives_cancel(bundle,dispatched):
    store,plan,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at);slot=first_slot(store,study)
    store.reserve_slot(slot,inputs,at=at)
    if dispatched:store.mark_dispatched(slot,inputs,at=at)
    path=store.path;store.close();reopened=ResearchStore.open_existing(path)
    try:
        reopened.recover_interrupted(at=at)
        assert reopened.get_study(study)['state']=='PAUSED_UNKNOWN'
        assert reopened.get_study(study)['cost']['unknown_reserved_usd']==pytest.approx(.0172032)
        with pytest.raises(StudyStorageError):reopened.reserve_slot(slot,inputs,at=at)
        with pytest.raises(StudyStorageError):reopened.mark_dispatched(slot,inputs,at=at)
        assert not reopened.resume_eligibility(study,inputs,at=at)['can_queue']
        reopened.request_cancel(study,at=at);reopened.finish(study,'CANCELLED',at=at)
        assert reopened.get_study(study)['cost']['unknown_reserved_usd']==pytest.approx(.0172032)
        with pytest.raises(StudyStorageError):reopened.queue_verified_unstarted(study,inputs,at=at)
    finally:reopened.close()


def test_recorded_success_cannot_repeat_and_does_not_fabricate_actual_cost(bundle):
    store,plan,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at);slot=first_slot(store,study)
    store.reserve_slot(slot,inputs,at=at);store.mark_dispatched(slot,inputs,at=at)
    with pytest.raises(StudyStorageError):store.mark_dispatched(slot,inputs,at=at)
    store.record_observation(slot,'SUCCEEDED','c'*64,at=at,usage_estimate=Decimal('0.001'))
    before=state_digest(store);store.record_observation(slot,'SUCCEEDED','c'*64,at=at,usage_estimate=Decimal('0.001'))
    assert state_digest(store)==before
    with pytest.raises(StudyStorageError):store.record_observation(slot,'FAILED','c'*64,at=at)
    cost=store.get_study(study)['cost']
    assert cost['unknown_reserved_usd']==pytest.approx(.0172032) and cost['confirmed_actual_usd']==0
    assert store.get_study(study)['state']=='PAUSED_BILLING'
    assert slot not in store.resume_eligibility(study,inputs,at=at)['never_sent_slot_ids']


def test_orphan_hold_and_legacy_fresh_id_guard_stay_intact(bundle):
    store,plan,inputs,at=bundle;study,auth=admitted(bundle)
    legacy={'configuration':{'total_budget_usd':1,'max_calls':32}}
    legacy['manifest_hash']=canonical_hash(legacy)
    before=state_digest(store)
    with pytest.raises(ValueError,match='anchored revision'):PilotBudget(store,legacy)
    assert state_digest(store)==before
    with store._transaction() as conn:
        conn.execute('INSERT INTO pilot_attempts(call_id,pilot_id,reserved,status) VALUES (?,?,?,?)',('synthetic-orphan',auth.budget_namespace,'0.02','unknown_reserved'))
    store.mark_running(study,inputs,at=at)
    with pytest.raises(StudyStorageError):store.reserve_slot(first_slot(store,study),inputs,at=at)
    store.recover_interrupted(at=at)
    assert store.get_study(study)['state']=='PAUSED_UNKNOWN'
    assert store.resume_eligibility(study,inputs,at=at)['orphan_reservation']


def test_pause_verified_never_sent_queue_and_terminal_failure(bundle):
    store,plan,inputs,at=bundle;study,_=admitted(bundle)
    store.pause(study,'PAUSED_DRIFT',at=at)
    ready=store.resume_eligibility(study,inputs,at=at)
    assert ready['can_queue'] and len(ready['never_sent_slot_ids'])==8  # Four Single + four initial Multi Analysis.
    store.queue_verified_unstarted(study,inputs,at=at)
    assert store.get_study(study)['state']=='QUEUED'
    store.request_cancel(study,at=at)
    assert store.get_study(study)['state']=='CANCELLED'
    with pytest.raises(StudyStorageError):store.mark_running(study,inputs,at=at)


def test_restart_before_reservation_requires_verification_and_recovery_is_idempotent(bundle):
    store,_,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at)
    store.recover_interrupted(at=at)
    assert store.get_study(study)['state']=='PAUSED_DRIFT'
    before=state_digest(store);store.recover_interrupted(at=at)
    assert state_digest(store)==before
    assert store.resume_eligibility(study,inputs,at=at)['can_queue']


def test_read_polling_opens_no_connections_files_or_schema_writes(bundle,monkeypatch):
    store,_,inputs,at=bundle;study,_=admitted(bundle)
    before=state_digest(store);files=set(store.path.parent.iterdir());statements=[]
    store.keeper.set_trace_callback(statements.append)
    def no_connect(*args,**kwargs):raise AssertionError('polling opened a connection')
    monkeypatch.setattr(sqlite3,'connect',no_connect)
    for _ in range(3):
        store.get_study(study);store.resume_eligibility(study,inputs,at=at)
    assert all(sql.lstrip().upper().startswith(('SELECT','PRAGMA')) for sql in statements)
    assert set(store.path.parent.iterdir())==files and state_digest(store)==before
    store.keeper.set_trace_callback(None)


def test_budget_refuses_underfunded_first_reservation_atomically(bundle):
    store,_,inputs,at=bundle
    tiny=replace(inputs,request=inputs.request.model_copy(update={'requested_budget_usd':Decimal('0.01')}))
    plan,_=plan_study(tiny.request,tiny.profile,tiny.registry,context=tiny.context)
    study=store.register(plan,tiny,idempotency_key='tiny');store.freeze(study,tiny,at=at)
    auth=auth_for(study,plan,tiny);store.record_authorization(auth,at=at);store.start(study,auth.authorization_id,tiny,at=at)
    store.mark_running(study,tiny,at=at);before=state_digest(store)
    from app.agents import AgentFailure
    with pytest.raises(AgentFailure,match='PILOT_BUDGET_EXHAUSTED'):store.reserve_slot(first_slot(store,study),tiny,at=at)
    assert state_digest(store)==before and store.get_study(study)['cost']['attempts']==0


def test_child_links_validate_source_group_caps_and_preserve_result(bundle):
    from app.models import ExperimentConfig
    store,plan,inputs,at=bundle;study,_=admitted(bundle)
    config=ExperimentConfig(strategy_id='single_agent',dataset_id=inputs.request.dataset_id,
        dataset_hash=inputs.request.expected_dataset_hash,start_date=inputs.request.start_date,end_date=inputs.request.end_date,
        research_group_id=study,llm_mode='api',model_provider=inputs.profile.provider,model_id=inputs.profile.model_id,
        max_llm_calls=inputs.request.requested_max_attempts,max_llm_budget_usd=float(inputs.request.requested_budget_usd),
        max_attempts_per_role=inputs.profile.attempts_per_role)
    wrong,_=store.create(config.model_copy(update={'research_group_id':'wrong-group'}),'wrong-child')
    with pytest.raises(StudyStorageError):store.link_child(study,'single_agent',wrong)
    run,_=store.create(config,'right-child');store.update(run,status='COMPLETED',result={'synthetic':True})
    store.link_child(study,'single_agent',run);store.link_child(study,'single_agent',run)
    assert store.get(run,True)['result']=={'synthetic':True}


def test_independent_namespaces_do_not_share_caps_or_reuse_old_approval(bundle):
    store,plan,inputs,at=bundle;one,auth=admitted(bundle);two=frozen_study(bundle,'independent')
    separate=auth_for(two,plan,inputs,'separate-approval','separate-key','separate-cohort')
    store.record_authorization(separate,at=at);store.start(two,separate.authorization_id,inputs,at=at)
    assert auth.budget_namespace!=separate.budget_namespace
    store.mark_running(one,inputs,at=at);store.reserve_slot(first_slot(store,one),inputs,at=at)
    assert store.get_study(one)['cost']['attempts']==1 and store.get_study(two)['cost']['attempts']==0
    store.request_cancel(one,at=at);store.finish(one,'CANCELLED',at=at)
    three=frozen_study(bundle,'blocked-old-key')
    third=auth_for(three,plan,inputs,'third-approval',auth.key_ref,'new-cohort')
    store.record_authorization(third,at=at)
    with pytest.raises(StudyStorageError):store.start(three,third.authorization_id,inputs,at=at)


def test_failed_terminal_cannot_reopen_and_incomplete_cannot_complete(bundle):
    store,_,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at)
    with pytest.raises(StudyStorageError):store.finish(study,'COMPLETED',at=at)
    store.finish(study,'FAILED',at=at);before=state_digest(store)
    with pytest.raises(StudyStorageError):store.queue_verified_unstarted(study,inputs,at=at)
    with pytest.raises(StudyStorageError):store.request_cancel(study,at=at)
    assert state_digest(store)==before


def test_stored_admission_drift_cannot_claim_or_reset_budget(bundle):
    store,_,inputs,at=bundle;study,_=admitted(bundle)
    with store._transaction() as conn:
        conn.execute("UPDATE research_admissions SET key_ref='tampered-reference' WHERE study_id=?",(study,))
    with pytest.raises(StudyStorageError,match='ADMISSION_SCOPE_INTEGRITY_MISMATCH'):store.mark_running(study,inputs,at=at)
    assert store.get_study(study)['cost']['attempts']==0


def test_server_authorization_idempotence_and_immutable_replay(bundle):
    store,plan,inputs,at=bundle;study=frozen_study(bundle);auth=auth_for(study,plan,inputs)
    store.record_authorization(auth,at=at);before=state_digest(store)
    store.record_authorization(auth,at=at)
    assert state_digest(store)==before
    with pytest.raises(StudyStorageError):store.record_authorization(auth.model_copy(update={'evidence_sha256':'0'*64}),at=at)
    assert state_digest(store)==before


def test_unknown_recovery_is_idempotent_without_releasing_or_rewriting_hold(bundle):
    store,_,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at)
    store.reserve_slot(first_slot(store,study),inputs,at=at);store.recover_interrupted(at=at)
    before=state_digest(store);store.recover_interrupted(at=at)
    assert state_digest(store)==before


def test_dispatch_checks_current_profile_before_future_send_claim(bundle):
    store,_,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at)
    slot=first_slot(store,study);store.reserve_slot(slot,inputs,at=at);before=state_digest(store)
    bad=replace(inputs,profile=inputs.profile.model_copy(update={'revision':2}))
    with pytest.raises(ValueError):store.mark_dispatched(slot,bad,at=at)
    assert state_digest(store)==before


def test_concurrent_reserve_and_dispatch_each_have_one_winner(bundle):
    store,_,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at)
    slot=first_slot(store,study)
    for operation in (store.reserve_slot,store.mark_dispatched):
        barrier=threading.Barrier(2);wins=[];blocked=[]
        def invoke():
            barrier.wait()
            try:operation(slot,inputs,at=at);wins.append(True)
            except StudyStorageError:blocked.append(True)
        threads=[threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        assert len(wins)==len(blocked)==1
    assert store.get_study(study)['cost']['attempts']==1


def test_cancelled_unknown_hold_survives_idempotent_migration_and_reopen(bundle):
    store,_,inputs,at=bundle;study,_=admitted(bundle);store.mark_running(study,inputs,at=at)
    store.reserve_slot(first_slot(store,study),inputs,at=at)
    store.request_cancel(study,at=at);store.finish(study,'CANCELLED',at=at)
    before=state_digest(store)
    with store.connect() as conn:migrate_schema(conn)
    assert state_digest(store)==before
    path=store.path;store.close();other=ResearchStore.open_existing(path)
    try:
        assert other.get_study(study)['state']=='CANCELLED'
        assert other.get_study(study)['cost']['unknown_reserved_usd']==pytest.approx(.0172032)
        assert state_digest(other)==before
    finally:other.close()


def test_new_research_initialization_cannot_call_legacy_restart_repair(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('legacy initializer called')
    monkeypatch.setattr(Store,'initialize',forbidden)
    store=ResearchStore.create_new(tmp_path/'explicit-new.sqlite3')
    store.close()


def test_storage_import_has_no_runtime_io_and_keeps_planner_pure():
    code=r'''
import sys,socket,sqlite3,os,concurrent.futures
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'backend'))
def forbidden(*a,**k):raise AssertionError('runtime IO forbidden')
sqlite3.connect=forbidden;socket.socket.connect=forbidden;socket.getaddrinfo=forbidden
concurrent.futures.ThreadPoolExecutor.__init__=forbidden
original=os._Environ.__getitem__
def get(env,name):
 if str(name).startswith(('OPENAI_','KUAIAPI_','LLM_')):return forbidden()
 return original(env,name)
os._Environ.__getitem__=get
from app.research_store import ResearchStore
assert not any(x in sys.modules for x in ('app.agents','app.gateway','app.pilot','app.pilot_billing','app.experiments'))
print('IMPORTED_STORAGE_OFFLINE')
'''
    result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='IMPORTED_STORAGE_OFFLINE'
