"""Explicit local study storage/admission. No provider sends, keys or routes.

Server authorization injection is an operator boundary, never a browser DTO.
Reservation/dispatch methods only persist state; none sends a request. Unknown
observations retain full financial holds until a scoped reconciliation. Known
estimates retain their full reservation until a scoped billing verification.
"""
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field, model_validator

from . import budget_ledger
from .data import DatasetRegistry, canonical_hash
from .models import Contract
from .research_contracts import (
    Digest, FrozenStudyPlan, Money, OpaqueID, SafePlanView,
    ServerResearchProfile, SourceContext, StudyRequest,
)
from .research_service import STRATEGY_ROLES, plan_study, selected_roles, verify_plan
from .storage import Store

SCHEMA_VERSION = 1
DOMAIN = "quantagent-phase2-research"
TERMINAL = {"CANCELLED", "FAILED", "COMPLETED"}
PAUSED = {"PAUSED_BILLING", "PAUSED_UNKNOWN", "PAUSED_DRIFT"}


class StudyStorageError(ValueError):
    pass


@dataclass(frozen=True)
class PlanningInputs:
    request: StudyRequest
    profile: ServerResearchProfile
    registry: DatasetRegistry
    context: SourceContext


class ServerAuthorization(Contract):
    authorization_id: OpaqueID
    study_id: UUID
    manifest_hash: Digest
    budget_namespace: Digest
    profile_id: OpaqueID
    profile_revision: Annotated[int, Field(strict=True, ge=1)]
    source_hash: Digest
    key_ref: OpaqueID
    cohort_ref: OpaqueID
    cap_usd: Money
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=64)]
    valid_from: datetime
    expires_at: datetime
    evidence_sha256: Digest

    @model_validator(mode="after")
    def validity(self):
        if any(x.tzinfo is None or x.utcoffset() is None for x in (self.valid_from,self.expires_at)) or self.valid_from>=self.expires_at:
            raise ValueError("bounded aware authorization validity required")
        return self


def namespace_for(study_id, manifest_hash):
    return canonical_hash({"study_id":str(UUID(str(study_id))), "manifest_hash":manifest_hash})


def _stamp(value):
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StudyStorageError("AWARE_CLOCK_REQUIRED")
    return value.isoformat()


def _check_schema(conn):
    version=conn.execute('PRAGMA user_version').fetchone()[0]
    if version!=SCHEMA_VERSION:
        raise StudyStorageError("INCOMPATIBLE_RESEARCH_SCHEMA")
    marker=conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_schema'").fetchone()
    row=conn.execute('SELECT domain FROM research_schema').fetchall() if marker else []
    if len(row)!=1 or row[0][0]!=DOMAIN:
        raise StudyStorageError("NOT_A_RESEARCH_DATABASE")
    required={'runs','events','pilot_budgets','pilot_attempts','research_studies','research_authorizations',
              'research_admissions','research_children','research_decisions','research_slots','research_events'}
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required<=tables or 'actual_charge' not in {r[1] for r in conn.execute('PRAGMA table_info(pilot_attempts)')}:
        raise StudyStorageError("INCOMPLETE_RESEARCH_SCHEMA")


def migrate_schema(conn, fault=lambda point: None):
    """Explicit additive v0(empty)→v1; no executescript or implicit commits."""
    version=conn.execute('PRAGMA user_version').fetchone()[0]
    if version==SCHEMA_VERSION:
        _check_schema(conn)
        return
    if version!=0 or conn.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
        raise StudyStorageError("REFUSING_LEGACY_OR_FUTURE_SCHEMA")
    if conn.in_transaction:
        raise StudyStorageError("MIGRATION_REQUIRES_OWN_TRANSACTION")
    try:
        conn.execute('BEGIN IMMEDIATE')
        Store.create_run_schema(conn)
        budget_ledger.create_schema(conn)
        statements = (
            'CREATE TABLE research_schema (domain TEXT PRIMARY KEY)',
            'CREATE TABLE research_studies (study_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, manifest_hash TEXT NOT NULL, plan_json TEXT NOT NULL, safe_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, namespace TEXT UNIQUE NOT NULL)',
            'CREATE TABLE research_authorizations (authorization_id TEXT PRIMARY KEY, study_id TEXT UNIQUE NOT NULL REFERENCES research_studies(study_id), payload_json TEXT NOT NULL)',
            'CREATE TABLE research_admissions (study_id TEXT PRIMARY KEY REFERENCES research_studies(study_id), authorization_id TEXT UNIQUE NOT NULL REFERENCES research_authorizations(authorization_id), namespace TEXT UNIQUE NOT NULL REFERENCES pilot_budgets(pilot_id), key_ref TEXT NOT NULL, cohort_ref TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)))',
            'CREATE UNIQUE INDEX research_active_key ON research_admissions(key_ref) WHERE active=1',
            'CREATE UNIQUE INDEX research_active_cohort ON research_admissions(cohort_ref) WHERE active=1',
            'CREATE TABLE research_children (study_id TEXT NOT NULL REFERENCES research_studies(study_id), strategy TEXT NOT NULL, run_id TEXT UNIQUE NOT NULL REFERENCES runs(run_id), PRIMARY KEY(study_id,strategy))',
            'CREATE TABLE research_decisions (decision_slot_id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(study_id), strategy TEXT NOT NULL, decision_session TEXT NOT NULL, execution_session TEXT NOT NULL, numeric_input_hash TEXT NOT NULL, UNIQUE(study_id,strategy,decision_session))',
            'CREATE TABLE research_slots (slot_id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(study_id), decision_slot_id TEXT NOT NULL REFERENCES research_decisions(decision_slot_id), stage TEXT NOT NULL, attempt_no INTEGER NOT NULL, call_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, outcome TEXT, observation_json TEXT, UNIQUE(decision_slot_id,stage,attempt_no))',
            'CREATE TABLE research_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, study_id TEXT NOT NULL REFERENCES research_studies(study_id), event_type TEXT NOT NULL, at TEXT NOT NULL, slot_id TEXT)',
        )
        for statement in statements:
            conn.execute(statement)
        conn.execute('INSERT INTO research_schema VALUES (?)',(DOMAIN,))
        fault("AFTER_SCHEMA")
        conn.execute('PRAGMA user_version=1')
        fault("AFTER_VERSION")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class ResearchStore(Store):
    """One process owner, same transactions/SQLite and financial representation.

    Construction alone does no IO. Use create_new/open_existing explicitly. Never
    call legacy initialize(), which repairs legacy run state on startup.
    """
    def initialize(self):
        raise StudyStorageError("USE_EXPLICIT_RESEARCH_INITIALIZATION")

    @classmethod
    def create_new(cls, path, *, migration_fault=lambda point: None):
        store=cls(path)
        store.path.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive creation cannot overwrite any old/settled/legacy DB.
        with store.path.open('xb'):
            pass
        try:
            store._acquire_owner()
            store.keeper.row_factory=sqlite3.Row
            with store.connect() as conn:
                migrate_schema(conn,migration_fault)
            return store
        except BaseException:
            store.close()
            raise

    @classmethod
    def open_existing(cls, path):
        store=cls(path)
        # mode=ro refuses a missing path. No migration or legacy restart update.
        with closing(sqlite3.connect(store.path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            _check_schema(conn)
        try:
            store._acquire_owner()
            store.keeper.row_factory=sqlite3.Row
            _check_schema(store.keeper)
            return store
        except BaseException:
            store.close()
            raise

    def _owned(self):
        if self.keeper is None or self.lock is None or not self.path.is_file():
            raise StudyStorageError("RESEARCH_STORE_NOT_OPEN")

    @contextmanager
    def connect(self):
        self._owned()
        with super().connect() as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            yield conn

    @contextmanager
    def _transaction(self):
        with self.connect() as conn:
            _check_schema(conn)
            conn.execute('BEGIN IMMEDIATE')
            yield conn

    @contextmanager
    def _read(self):
        # Reuse the owned keeper; polling opens no files/connections and writes nothing.
        with self.database_lock:
            self._owned()
            with super().read() as conn:
                _check_schema(conn)
                yield conn

    def _checkpoint(self, point):
        """Fault injection seam for atomicity tests, never an execution hook."""

    @staticmethod
    def _study(conn, study_id):
        row=conn.execute('SELECT * FROM research_studies WHERE study_id=?',(str(study_id),)).fetchone()
        if not row:
            raise StudyStorageError("UNKNOWN_STUDY")
        plan=FrozenStudyPlan.model_validate_json(row['plan_json'])
        if plan.manifest_hash!=row['manifest_hash'] or row['namespace']!=namespace_for(row['study_id'],row['manifest_hash']):
            raise StudyStorageError("STUDY_INTEGRITY_MISMATCH")
        return row,plan

    @staticmethod
    def _event(conn, study_id, event, at, slot=None):
        conn.execute('INSERT INTO research_events(study_id,event_type,at,slot_id) VALUES (?,?,?,?)',(study_id,event,_stamp(at),slot))

    @staticmethod
    def _state(conn, row, target, at, reason=None):
        if row['state'] in TERMINAL:
            raise StudyStorageError("TERMINAL_STUDY")
        conn.execute('UPDATE research_studies SET state=?,reason=? WHERE study_id=?',(target,reason,row['study_id']))
        ResearchStore._event(conn,row['study_id'],target,at)

    @staticmethod
    def _verify(plan, inputs, at):
        _stamp(at)
        verify_plan(plan,inputs.request,inputs.profile,inputs.registry,context=inputs.context)
        if not inputs.profile.valid_from<=at<inputs.profile.expires_at:
            raise StudyStorageError("PROFILE_EXPIRED")

    @staticmethod
    def _authorization(conn, row, plan, authorization_id, at):
        _stamp(at)
        saved=conn.execute('SELECT payload_json FROM research_authorizations WHERE authorization_id=?',(authorization_id,)).fetchone()
        if not saved:
            raise StudyStorageError("SERVER_AUTHORIZATION_REQUIRED")
        auth=ServerAuthorization.model_validate_json(saved[0]);p=plan.payload
        expected=(row['study_id'],row['manifest_hash'],row['namespace'],p['server_profile']['profile_id'],p['server_profile']['revision'],p['source']['source_hash'],Decimal(p['request']['requested_budget_usd']),p['request']['requested_max_attempts'])
        actual=(str(auth.study_id),auth.manifest_hash,auth.budget_namespace,auth.profile_id,auth.profile_revision,auth.source_hash,auth.cap_usd,auth.max_attempts)
        if actual!=expected or not auth.valid_from<=at<auth.expires_at:
            raise StudyStorageError("AUTHORIZATION_SCOPE_OR_EXPIRY_MISMATCH")
        return auth

    @staticmethod
    def _funded(conn, row, plan, at):
        admission=conn.execute('SELECT * FROM research_admissions WHERE study_id=?',(row['study_id'],)).fetchone()
        if not admission or not admission['active']:
            raise StudyStorageError("STUDY_NOT_ADMITTED")
        auth=ResearchStore._authorization(conn,row,plan,admission['authorization_id'],at)
        if (admission['namespace'],admission['key_ref'],admission['cohort_ref'])!=(row['namespace'],auth.key_ref,auth.cohort_ref):
            raise StudyStorageError("ADMISSION_SCOPE_INTEGRITY_MISMATCH")
        saved=conn.execute('SELECT manifest_json FROM pilot_budgets WHERE pilot_id=?',(row['namespace'],)).fetchone()
        expected=ResearchStore._budget_manifest(row,auth)
        if not saved or json.loads(saved[0])!=expected:
            raise StudyStorageError("ADMITTED_BUDGET_INTEGRITY_MISMATCH")
        return auth

    @staticmethod
    def _budget_manifest(row, auth):
        value={'kind':'registered-study-account-v1','study_id':row['study_id'],
            'plan_hash':row['manifest_hash'],'authorization_id':auth.authorization_id,
            'configuration':{'total_budget_usd':str(auth.cap_usd),'max_calls':auth.max_attempts}}
        return {**value,'manifest_hash':canonical_hash(value)}

    def register(self, plan, inputs, *, idempotency_key):
        if not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=100:
            raise StudyStorageError("IDEMPOTENCY_KEY_REQUIRED")
        verify_plan(plan,inputs.request,inputs.profile,inputs.registry,context=inputs.context)
        _,view=plan_study(inputs.request,inputs.profile,inputs.registry,context=inputs.context)
        with self._transaction() as conn:
            old=conn.execute('SELECT study_id,manifest_hash FROM research_studies WHERE idempotency_key=?',(idempotency_key,)).fetchone()
            if old:
                if old['manifest_hash']!=plan.manifest_hash:
                    raise StudyStorageError("IDEMPOTENCY_CONFLICT")
                return old['study_id']
            study_id=str(uuid4())
            conn.execute('INSERT INTO research_studies VALUES (?,?,?,?,?,?,?,?)',(study_id,idempotency_key,plan.manifest_hash,plan.model_dump_json(),view.model_dump_json(),'DRAFT',None,namespace_for(study_id,plan.manifest_hash)))
            self._checkpoint('AFTER_REGISTER')
            return study_id

    def freeze(self, study_id, inputs, *, at):
        with self._transaction() as conn:
            row,plan=self._study(conn,study_id);self._verify(plan,inputs,at)
            if row['state']=='FROZEN':
                return
            if row['state']!='DRAFT':
                raise StudyStorageError("FREEZE_REQUIRES_DRAFT")
            p=plan.payload
            for strategy,stages in selected_roles(p["request"].get("study_protocol")).items():
                for index,session in enumerate(p['sessions']):
                    if strategy=='buy_hold' and index>0:
                        continue
                    decision_id=str(uuid5(UUID(row['study_id']),strategy+':'+session['decision']))
                    conn.execute('INSERT INTO research_decisions VALUES (?,?,?,?,?,?)',(decision_id,row['study_id'],strategy,session['decision'],session['execution'],session['numeric_input_hash']))
                    for stage in stages:
                        for attempt in range(1,p['server_profile']['attempts_per_role']+1):
                            slot_id=str(uuid5(UUID(decision_id),stage+':'+str(attempt)))
                            conn.execute('INSERT INTO research_slots VALUES (?,?,?,?,?,?,?,?,?)',(slot_id,row['study_id'],decision_id,stage,attempt,slot_id,'UNSTARTED',None,None))
            self._checkpoint('AFTER_FREEZE_SLOTS')
            self._state(conn,row,'FROZEN',at)

    def record_authorization(self, authorization, *, at):
        """Server-only injection. No actual funding is supplied by this module."""
        authorization=ServerAuthorization.model_validate(authorization.model_dump(mode='json',warnings=False))
        with self._transaction() as conn:
            row,plan=self._study(conn,authorization.study_id)
            if row['state'] not in ('FROZEN','QUEUED'):
                raise StudyStorageError("AUTHORIZATION_REQUIRES_FROZEN_STUDY")
            old=conn.execute('SELECT payload_json FROM research_authorizations WHERE authorization_id=?',(authorization.authorization_id,)).fetchone()
            if old:
                if json.loads(old[0])!=authorization.model_dump(mode='json'):
                    raise StudyStorageError("AUTHORIZATION_REUSE_CONFLICT")
            else:
                try:
                    conn.execute('INSERT INTO research_authorizations VALUES (?,?,?)',(authorization.authorization_id,row['study_id'],authorization.model_dump_json()))
                except sqlite3.IntegrityError:
                    raise StudyStorageError("STUDY_ALREADY_BOUND_TO_AUTHORIZATION") from None
            self._authorization(conn,row,plan,authorization.authorization_id,at)

    def start(self, study_id, authorization_id, inputs, *, at):
        """Admit to QUEUED atomically; never submit a worker or provider."""
        with self._transaction() as conn:
            row,plan=self._study(conn,study_id);self._verify(plan,inputs,at)
            auth=self._authorization(conn,row,plan,authorization_id,at)
            if row['state']=='QUEUED':
                self._funded(conn,row,plan,at)
                return
            if row['state']!='FROZEN':
                raise StudyStorageError("START_REQUIRES_FROZEN_STUDY")
            try:
                conn.execute('INSERT INTO pilot_budgets VALUES (?,?)',(row['namespace'],json.dumps(self._budget_manifest(row,auth),sort_keys=True)))
                conn.execute('INSERT INTO research_admissions VALUES (?,?,?,?,?,1)',(row['study_id'],auth.authorization_id,row['namespace'],auth.key_ref,auth.cohort_ref))
            except sqlite3.IntegrityError:
                raise StudyStorageError("APPROVAL_OR_COHORT_ALREADY_ADMITTED") from None
            self._checkpoint('AFTER_ADMISSION')
            self._state(conn,row,'QUEUED',at)

    def find_study(self, idempotency_key):
        """Read one idempotency row. It does not create, migrate or send."""
        with self._read() as conn:
            row = conn.execute('SELECT study_id,manifest_hash,state FROM research_studies WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        return {'study_id': row['study_id'], 'manifest_hash': row['manifest_hash'], 'state': row['state']} if row else None

    def list_studies(self):
        """Safe identifiers only. Private plans and authorizations stay in the database."""
        with self._read() as conn:
            rows = conn.execute('SELECT study_id,state,reason,manifest_hash,safe_json FROM research_studies ORDER BY rowid DESC').fetchall()
        output = []
        for row in rows:
            view = SafePlanView.model_validate_json(row['safe_json'])
            output.append({'study_id': row['study_id'], 'state': row['state'], 'reason': row['reason'],
                           'manifest_hash': row['manifest_hash'], 'profile_id': view.profile_id,
                           'dataset_id': view.dataset_id, 'dataset_kind': view.dataset_kind})
        return output

    def slot_progress(self, study_id):
        """Counts only. Observation bodies are not loaded."""
        with self._read() as conn:
            row, _ = self._study(conn, study_id)
            grouped = conn.execute('SELECT state, COUNT(*) AS c FROM research_slots WHERE study_id=? GROUP BY state', (row['study_id'],)).fetchall()
        counts = {item['state']: item['c'] for item in grouped}
        total = sum(counts.values())
        return {'total_slots': total, 'recorded_slots': counts.get('RECORDED', 0), 'by_state': counts}

    def get_study(self, study_id):
        with self._read() as conn:
            row,plan=self._study(conn,study_id)
            view=SafePlanView.model_validate_json(row['safe_json'])
            if view.manifest_hash!=plan.manifest_hash:
                raise StudyStorageError("SAFE_VIEW_INTEGRITY_MISMATCH")
            admitted=conn.execute('SELECT 1 FROM research_admissions WHERE study_id=?',(row['study_id'],)).fetchone()
            p=plan.payload
            cost=budget_ledger.summary(conn,row['namespace'],Decimal(p['request']['requested_budget_usd']),p['request']['requested_max_attempts']) if admitted else None
            return {'study_id':row['study_id'],'state':row['state'],'reason':row['reason'],
                'plan':view.model_dump(mode='json'),'cost':cost}

    @staticmethod
    def _candidate_slots(conn, row):
        slots=conn.execute('SELECT s.*,d.strategy,d.decision_session,d.execution_session FROM research_slots s JOIN research_decisions d USING(decision_slot_id) WHERE s.study_id=? ORDER BY CASE d.strategy WHEN \'single_agent\' THEN 0 ELSE 1 END,d.decision_session,CASE s.stage WHEN \'analysis\' THEN 0 WHEN \'critic\' THEN 1 ELSE 2 END,s.attempt_no',(row['study_id'],)).fetchall()
        if any(s['state'] in ('RESERVED','DISPATCHED','UNCERTAIN') for s in slots):
            return []
        output=[]
        for slot in slots:
            if slot['state']!='UNSTARTED' or conn.execute('SELECT 1 FROM pilot_attempts WHERE call_id=?',(slot['call_id'],)).fetchone():
                continue
            siblings=[s for s in slots if s['decision_slot_id']==slot['decision_slot_id'] and s['stage']==slot['stage']]
            if any(s['outcome']=='SUCCEEDED' for s in siblings) or any(s['attempt_no']<slot['attempt_no'] and s['state']!='RECORDED' for s in siblings):
                continue
            stages=STRATEGY_ROLES[slot['strategy']];predecessors=stages[:stages.index(slot['stage'])]
            if any(not any(s['decision_slot_id']==slot['decision_slot_id'] and s['stage']==stage and s['outcome']=='SUCCEEDED' for s in slots) for stage in predecessors):
                continue
            output.append(slot)
        return output

    def resume_eligibility(self, study_id, inputs, *, at):
        with self._read() as conn:
            row,plan=self._study(conn,study_id);self._verify(plan,inputs,at)
            self._funded(conn,row,plan,at)
            cost=budget_ledger.summary(conn,row['namespace'],Decimal(plan.payload['request']['requested_budget_usd']),plan.payload['request']['requested_max_attempts'])
            candidates=self._candidate_slots(conn,row) if row['state'] not in TERMINAL else []
            orphan=self._orphan(conn,row)
            tail = self._completion_pending(conn, row)
            allowed=row['state'] in PAUSED and not orphan and cost['unknown_reserved_usd']==0 and bool(candidates or tail)
            return {'can_queue':allowed,'never_sent_slot_ids':[s['slot_id'] for s in candidates],
                'unknown_reserved_usd':cost['unknown_reserved_usd'],'orphan_reservation':bool(orphan), 'deterministic_completion_only':tail}

    @staticmethod
    def _completion_pending(conn, row):
        """No send remains eligible, but a durable source/final study needs closing."""
        if ResearchStore._candidate_slots(conn, row):
            return False
        if conn.execute("SELECT 1 FROM research_slots WHERE study_id=? AND state NOT IN ('UNSTARTED','RECORDED')", (row['study_id'],)).fetchone():
            return False
        runs = conn.execute('SELECT status FROM runs WHERE idempotency_key LIKE ?', (row['study_id']+':source:%',)).fetchall()
        return bool(runs) and all(run['status'] != 'FAILED' for run in runs)

    def queue_verified_unstarted(self, study_id, inputs, *, at):
        with self._transaction() as conn:
            # RLock keeps the read eligibility and mutation under the same transaction owner.
            row,plan=self._study(conn,study_id);self._verify(plan,inputs,at);self._funded(conn,row,plan,at)
            costs=budget_ledger.summary(conn,row['namespace'],Decimal(plan.payload['request']['requested_budget_usd']),plan.payload['request']['requested_max_attempts'])
            if row['state'] not in PAUSED or costs['unknown_reserved_usd'] or self._orphan(conn,row) or not (self._candidate_slots(conn,row) or self._completion_pending(conn,row)):
                raise StudyStorageError("NO_VERIFIED_UNSTARTED_CONTINUATION")
            self._state(conn,row,'QUEUED',at)

    def mark_running(self, study_id, inputs, *, at):
        with self._transaction() as conn:
            row,plan=self._study(conn,study_id);self._verify(plan,inputs,at);self._funded(conn,row,plan,at)
            if row['state']!='QUEUED':
                raise StudyStorageError("RUNNING_REQUIRES_QUEUED")
            self._state(conn,row,'RUNNING',at)

    def pause(self, study_id, reason, *, at, detail=None):
        if reason not in PAUSED:
            raise StudyStorageError("INVALID_PAUSE_STATE")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 200 or any(c.isspace() and c != ' ' for c in detail)):
            raise StudyStorageError("UNSAFE_PAUSE_DETAIL")
        with self._transaction() as conn:
            row,_=self._study(conn,study_id)
            if row['state'] not in {'RUNNING','QUEUED',*PAUSED}:
                raise StudyStorageError("STUDY_NOT_ACTIVE")
            self._state(conn,row,reason,at,detail or reason)

    def reserve_slot(self, slot_id, inputs, *, at):
        with self._transaction() as conn:
            slot=conn.execute('SELECT * FROM research_slots WHERE slot_id=?',(slot_id,)).fetchone()
            if not slot:
                raise StudyStorageError("UNKNOWN_SLOT")
            row,plan=self._study(conn,slot['study_id']);self._verify(plan,inputs,at);auth=self._funded(conn,row,plan,at)
            candidates=self._candidate_slots(conn,row)
            if row['state']!='RUNNING' or not candidates or slot_id!=candidates[0]['slot_id']:
                raise StudyStorageError("SLOT_NOT_ELIGIBLE")
            if self._orphan(conn,row) or budget_ledger.summary(conn,row['namespace'],auth.cap_usd,auth.max_attempts)['unknown_reserved_usd']:
                raise StudyStorageError("UNRESOLVED_LIABILITY_BLOCKS_RESERVATION")
            amount=Decimal(plan.payload['estimate']['reservation_usd_per_attempt'])
            budget_ledger.reserve(conn,row['namespace'],row['namespace'],auth.cap_usd,auth.max_attempts,slot['call_id'],amount)
            self._checkpoint('AFTER_RESERVATION')
            conn.execute("UPDATE research_slots SET state='RESERVED' WHERE slot_id=?",(slot_id,))
            self._event(conn,row['study_id'],'RESERVED',at,slot_id)
            return slot['call_id']

    def mark_dispatched(self, slot_id, inputs, *, at):
        """One-time durable claim for a FUTURE sender; this method sends nothing."""
        with self._transaction() as conn:
            slot=conn.execute('SELECT * FROM research_slots WHERE slot_id=?',(slot_id,)).fetchone()
            if not slot:
                raise StudyStorageError("UNKNOWN_SLOT")
            row,plan=self._study(conn,slot['study_id']);self._verify(plan,inputs,at);self._funded(conn,row,plan,at)
            if row['state']!='RUNNING' or slot['state']!='RESERVED':
                raise StudyStorageError("DISPATCH_ALREADY_CLAIMED_OR_INELIGIBLE")
            reservation=conn.execute('SELECT * FROM pilot_attempts WHERE call_id=? AND pilot_id=?',(slot['call_id'],row['namespace'])).fetchone()
            if not reservation or self._orphan(conn,row) or reservation['status']!='unknown_reserved' or Decimal(reservation['reserved'])!=Decimal(plan.payload['estimate']['reservation_usd_per_attempt']):
                raise StudyStorageError("MISSING_RESERVATION")
            conn.execute("UPDATE research_slots SET state='DISPATCHED' WHERE slot_id=?",(slot_id,))
            self._event(conn,row['study_id'],'DISPATCHED',at,slot_id)
            self._checkpoint('AFTER_DISPATCH')

    def record_observation(self, slot_id, outcome, evidence_sha256, *, at, usage_estimate=None):
        """Persist a caller's observation only; never release/reconcile a charge."""
        if outcome not in ('SUCCEEDED','FAILED') or not isinstance(evidence_sha256,str) or len(evidence_sha256)!=64 or any(c not in '0123456789abcdef' for c in evidence_sha256):
            raise StudyStorageError("INVALID_OBSERVATION")
        if usage_estimate is not None and (not isinstance(usage_estimate,Decimal) or not usage_estimate.is_finite() or usage_estimate<0):
            raise StudyStorageError("INVALID_USAGE_ESTIMATE")
        payload=json.dumps({'outcome':outcome,'evidence_sha256':evidence_sha256,'usage_estimate_usd':str(usage_estimate) if usage_estimate is not None else None},sort_keys=True)
        with self._transaction() as conn:
            slot=conn.execute('SELECT * FROM research_slots WHERE slot_id=?',(slot_id,)).fetchone()
            if not slot:
                raise StudyStorageError("UNKNOWN_SLOT")
            if slot['state']=='RECORDED' and slot['observation_json']==payload:
                return
            if slot['state']!='DISPATCHED':
                raise StudyStorageError("OBSERVATION_CANNOT_REWRITE_SLOT")
            row,_=self._study(conn,slot['study_id'])
            conn.execute("UPDATE research_slots SET state='RECORDED',outcome=?,observation_json=? WHERE slot_id=?",(outcome,payload,slot_id))
            self._checkpoint('AFTER_OBSERVATION')
            self._event(conn,row['study_id'],'RECORDED',at,slot_id)
            if row['state'] not in TERMINAL and row['state']!='CANCEL_REQUESTED':
                self._state(conn,row,'PAUSED_BILLING',at,'AWAITING_VERIFIED_RECONCILIATION')

    def recover_interrupted(self, *, at):
        """Explicit recovery; uncertain sends/holds are never reset or resent."""
        with self._transaction() as conn:
            for study in conn.execute('SELECT study_id FROM research_studies').fetchall():
                row,_=self._study(conn,study[0])
                inflight=conn.execute("SELECT slot_id FROM research_slots WHERE study_id=? AND state IN ('RESERVED','DISPATCHED')",(row['study_id'],)).fetchall()
                orphan=self._orphan(conn,row)
                if inflight or orphan:
                    conn.execute("UPDATE research_slots SET state='UNCERTAIN' WHERE study_id=? AND state IN ('RESERVED','DISPATCHED')",(row['study_id'],))
                    if row['state'] not in TERMINAL and row['state']!='CANCEL_REQUESTED':
                        if row['state']!='PAUSED_UNKNOWN':
                            self._state(conn,row,'PAUSED_UNKNOWN',at,'INTERRUPTED_OR_ORPHAN_RESERVATION')
                    if inflight or not conn.execute("SELECT 1 FROM research_events WHERE study_id=? AND event_type='UNKNOWN_LIABILITY_RETAINED'",(row['study_id'],)).fetchone():
                        self._event(conn,row['study_id'],'UNKNOWN_LIABILITY_RETAINED',at)
                elif row['state']=='RUNNING':
                    # No reservation does not mean a worker should automatically restart.
                    attempts=budget_ledger.rows(conn,row['namespace'])
                    target='PAUSED_BILLING' if any(r['usage_estimate'] is None and r['actual_charge'] is None for r in attempts) else 'PAUSED_DRIFT'
                    self._state(conn,row,target,at,'RESTART_REQUIRES_VERIFICATION')

    def request_cancel(self, study_id, *, at):
        with self._transaction() as conn:
            row,_=self._study(conn,study_id)
            if row['state']=='CANCELLED':
                return
            if row['state'] in TERMINAL:
                raise StudyStorageError("TERMINAL_STUDY")
            target='CANCELLED' if row['state'] in ('DRAFT','FROZEN','QUEUED') else 'CANCEL_REQUESTED'
            self._state(conn,row,target,at,'CANCEL_REQUESTED_BY_OPERATOR')
            if target=='CANCELLED':
                self._release_empty_lease(conn,row)

    @staticmethod
    def _orphan(conn, row):
        return bool(conn.execute('SELECT 1 FROM pilot_attempts a LEFT JOIN research_slots s ON s.call_id=a.call_id WHERE a.pilot_id=? AND (s.slot_id IS NULL OR s.state=\'UNSTARTED\' OR s.study_id!=?)',(row['namespace'],row['study_id'])).fetchone())

    @staticmethod
    def _release_empty_lease(conn, row):
        # Holds and all historical amounts remain; only a never-spent lease releases here.
        if not budget_ledger.rows(conn,row['namespace']):
            conn.execute('UPDATE research_admissions SET active=0 WHERE study_id=?',(row['study_id'],))

    def finish(self, study_id, outcome, *, at):
        if outcome not in TERMINAL:
            raise StudyStorageError("INVALID_TERMINAL_STATE")
        with self._transaction() as conn:
            row, plan = self._study(conn, study_id)
            if row['state']==outcome:
                return
            if outcome=='CANCELLED' and row['state']!='CANCEL_REQUESTED':
                raise StudyStorageError("CANCEL_ACK_REQUIRED")
            if outcome=='COMPLETED':
                children=conn.execute('SELECT r.status FROM research_children c JOIN runs r USING(run_id) WHERE c.study_id=?',(row['study_id'],)).fetchall()
                inflight=conn.execute("SELECT 1 FROM research_slots WHERE study_id=? AND state IN ('RESERVED','DISPATCHED','UNCERTAIN')",(row['study_id'],)).fetchone()
                expected = len(selected_roles(plan.payload["request"].get("study_protocol")))
                if row['state']!='RUNNING' or len(children)!=expected or any(r[0]!='COMPLETED' for r in children) or inflight or self._orphan(conn,row) or self._candidate_slots(conn,row):
                    raise StudyStorageError("STUDY_NOT_COMPLETE")
            conn.execute("UPDATE research_slots SET state='UNCERTAIN' WHERE study_id=? AND state IN ('RESERVED','DISPATCHED')",(row['study_id'],))
            self._state(conn,row,outcome,at,'TERMINAL_ACKNOWLEDGEMENT')
            self._release_empty_lease(conn,row)

    def link_child(self, study_id, strategy, run_id):
        if strategy not in STRATEGY_ROLES:
            raise StudyStorageError("UNKNOWN_STRATEGY")
        with self._transaction() as conn:
            row,plan=self._study(conn,study_id)
            if row['state'] in TERMINAL:
                raise StudyStorageError("TERMINAL_STUDY")
            run=conn.execute('SELECT config_json FROM runs WHERE run_id=?',(str(run_id),)).fetchone()
            if not run:
                raise StudyStorageError("UNKNOWN_CHILD_RUN")
            cfg=json.loads(run[0]);p=plan.payload;request=p['request'];profile=p['server_profile']
            expected={'strategy_id':strategy,'dataset_id':request['dataset_id'],'dataset_hash':request['expected_dataset_hash'],
                'start_date':request['start_date'],'end_date':request['end_date'],'initial_cash':request['initial_cash'],
                'fee_bps':request['fee_bps'],'slippage_bps':request['slippage_bps'],'max_exposure':request['max_exposure'],
                'research_group_id':row['study_id'],'prompt_version':p['source']['prompt_version'],
                'max_llm_calls':request['requested_max_attempts'],
                'max_llm_budget_usd':float(Decimal(request['requested_budget_usd'])),
                'max_attempts_per_role':profile['attempts_per_role'],
                'llm_mode':'api' if STRATEGY_ROLES[strategy] else 'mock',
                'model_provider':profile['provider'] if STRATEGY_ROLES[strategy] else None,
                'model_id':profile['model_id'] if STRATEGY_ROLES[strategy] else None}
            if any(cfg.get(k)!=v for k,v in expected.items()) or cfg.get('llm_mode')=='replay':
                raise StudyStorageError("CHILD_SOURCE_MISMATCH")
            existing=conn.execute('SELECT run_id FROM research_children WHERE study_id=? AND strategy=?',(row['study_id'],strategy)).fetchone()
            if existing and existing[0]==str(run_id):
                return
            conn.execute('INSERT INTO research_children VALUES (?,?,?)',(row['study_id'],strategy,str(run_id)))

    def checkpoint(self, study_id, name, *, at, slot_id=None):
        """Durable execution checkpoint. It does not send, fill or release a hold."""
        if name not in ('BEFORE_DECISION', 'BEFORE_ROLE', 'BEFORE_FILL'):
            raise StudyStorageError('UNKNOWN_CHECKPOINT')
        with self._transaction() as conn:
            row, _ = self._study(conn, study_id)
            if row['state'] in TERMINAL:
                raise StudyStorageError('TERMINAL_STUDY')
            self._event(conn, row['study_id'], 'execution-checkpoint-v1:' + name, at, slot_id)
            return row['state']

    def load_plan(self, study_id):
        with self._read() as conn:
            _, plan = self._study(conn, study_id)
            return plan

    def list_slots(self, study_id):
        """Worker trace read. Safe reports must not pass observation_json through."""
        with self._read() as conn:
            row, _ = self._study(conn, study_id)
            slots = conn.execute(
                'SELECT s.slot_id,s.decision_slot_id,s.stage,s.attempt_no,s.call_id,s.state,s.outcome,s.observation_json,'
                'd.strategy,d.decision_session,d.execution_session FROM research_slots s '
                'JOIN research_decisions d USING(decision_slot_id) WHERE s.study_id=? '
                'ORDER BY CASE d.strategy WHEN \'single_agent\' THEN 0 ELSE 1 END,d.decision_session,'
                'CASE s.stage WHEN \'analysis\' THEN 0 WHEN \'critic\' THEN 1 ELSE 2 END,s.attempt_no',
                (row['study_id'],)).fetchall()
            return [dict(slot) for slot in slots]

    def list_frozen_inputs(self, study_id):
        """Read-only numeric input identities copied onto each frozen decision."""
        with self._read() as conn:
            row, _ = self._study(conn, study_id)
            rows = conn.execute(
                'SELECT strategy,decision_session,numeric_input_hash FROM research_decisions WHERE study_id=? ORDER BY strategy,decision_session',
                (row['study_id'],)).fetchall()
        return [dict(item) for item in rows]

    def bound_key_ref(self, study_id):
        """Server-side comparison input. Callers must not copy this into a DTO."""
        with self._read() as conn:
            row, _ = self._study(conn, study_id)
            admission = conn.execute('SELECT key_ref FROM research_admissions WHERE study_id=?', (row['study_id'],)).fetchone()
            if not admission:
                raise StudyStorageError('STUDY_NOT_ADMITTED')
            return admission['key_ref']

    def check_execution_scope(self, study_id, inputs, *, at):
        """Pure current-time funding/profile integrity check, including expiry."""
        with self._read() as conn:
            row, plan = self._study(conn, study_id)
            self._verify(plan, inputs, at)
            self._funded(conn, row, plan, at)

    def pin_billing_window(self, study_id, date, timezone, *, at):
        """Execution-only durable day scope; a restart cannot reset its window."""
        prefix = 'billing-window-v1:'
        marker = prefix + canonical_hash({'as_of_date': date, 'timezone': timezone})
        with self._transaction() as conn:
            row, _ = self._study(conn, study_id)
            previous = conn.execute('SELECT event_type FROM research_events WHERE study_id=? AND event_type LIKE ? ORDER BY sequence LIMIT 1',
                                    (row['study_id'], prefix+'%')).fetchone()
            if previous and previous['event_type'] != marker:
                raise StudyStorageError('BILLING_WINDOW_CHANGED')
            if not previous:
                if row['state'] not in ('QUEUED', 'RUNNING'):
                    raise StudyStorageError('BILLING_WINDOW_REQUIRES_EXECUTION')
                self._event(conn, row['study_id'], marker, at)

    def call_billing_verified(self, slot_id):
        with self._read() as conn:
            slot = conn.execute('SELECT study_id,call_id FROM research_slots WHERE slot_id=?', (slot_id,)).fetchone()
            if not slot:
                return False
            row, _ = self._study(conn, slot['study_id'])
            attempt = conn.execute('SELECT status FROM pilot_attempts WHERE call_id=? AND pilot_id=?', (slot['call_id'], row['namespace'])).fetchone()
            receipt = budget_ledger.aggregate(conn, row['namespace'])
            return bool(attempt and attempt['status'] == 'usage_estimated' and receipt and slot['call_id'] in receipt['covered_call_ids'])

    def close_unsent(self, slot_id, reason, *, at):
        """Record a slot that never reserved or sent. It does not pause or release another hold."""
        if reason not in ('INPUT_BOUND', 'NO_AUTOMATIC_RETRY'):
            raise StudyStorageError('UNSUPPORTED_CLOSE_REASON')
        payload = json.dumps({'outcome': 'FAILED', 'reason': reason, 'sent': False}, sort_keys=True, separators=(',', ':'))
        with self._transaction() as conn:
            slot = conn.execute('SELECT * FROM research_slots WHERE slot_id=?', (slot_id,)).fetchone()
            if not slot:
                raise StudyStorageError('UNKNOWN_SLOT')
            if slot['state'] == 'RECORDED' and slot['observation_json'] == payload:
                return
            if slot['state'] != 'UNSTARTED':
                raise StudyStorageError('CLOSE_REQUIRES_UNSTARTED_SLOT')
            if conn.execute('SELECT 1 FROM pilot_attempts WHERE call_id=?', (slot['call_id'],)).fetchone():
                raise StudyStorageError('CLOSE_REQUIRES_NO_RESERVATION')
            row, _ = self._study(conn, slot['study_id'])
            if row['state'] in TERMINAL:
                raise StudyStorageError('TERMINAL_STUDY')
            conn.execute("UPDATE research_slots SET state='RECORDED',outcome='FAILED',observation_json=? WHERE slot_id=?", (payload, slot_id))
            self._event(conn, row['study_id'], 'RECORDED', at, slot_id)

    def settle_known_usage(self, slot_id, outcome, observation, amount, *, at):
        """Persist an estimate, retaining its full hold until verified aggregate evidence."""
        if outcome not in ('SUCCEEDED', 'FAILED'):
            raise StudyStorageError('INVALID_OBSERVATION')
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise StudyStorageError('INVALID_USAGE_ESTIMATE')
        if not isinstance(observation, dict):
            raise StudyStorageError('INVALID_OBSERVATION')
        payload = json.dumps(observation, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with self._transaction() as conn:
            slot = conn.execute('SELECT * FROM research_slots WHERE slot_id=?', (slot_id,)).fetchone()
            if not slot:
                raise StudyStorageError('UNKNOWN_SLOT')
            row, _ = self._study(conn, slot['study_id'])
            if slot['state'] == 'RECORDED' and slot['outcome'] == outcome and slot['observation_json'] == payload:
                return canonical_hash(json.loads(payload))
            if slot['state'] != 'DISPATCHED':
                raise StudyStorageError('SETTLEMENT_REQUIRES_DISPATCHED_SLOT')
            if row['state'] in TERMINAL:
                raise StudyStorageError('TERMINAL_STUDY')
            budget_ledger.settle_estimate(conn, row['namespace'], slot['call_id'], amount)
            conn.execute("UPDATE research_slots SET state='RECORDED',outcome=?,observation_json=? WHERE slot_id=?", (outcome, payload, slot_id))
            self._event(conn, row['study_id'], 'RECORDED', at, slot_id)
            return canonical_hash(json.loads(payload))

    def retain_liability(self, slot_id, observation, *, at, pause_state='PAUSED_BILLING'):
        """Record an uncertain or contradictory call and keep its full reservation."""
        if not isinstance(observation, dict):
            raise StudyStorageError('INVALID_OBSERVATION')
        if pause_state not in ('PAUSED_BILLING', 'PAUSED_DRIFT'):
            raise StudyStorageError('INVALID_PAUSE_STATE')
        payload = json.dumps(observation, sort_keys=True, separators=(',', ':'), allow_nan=False)
        evidence = canonical_hash(json.loads(payload))
        with self._transaction() as conn:
            slot = conn.execute('SELECT * FROM research_slots WHERE slot_id=?', (slot_id,)).fetchone()
            if not slot:
                raise StudyStorageError('UNKNOWN_SLOT')
            if slot['state'] == 'RECORDED' and slot['observation_json'] == payload:
                return evidence
            if slot['state'] != 'DISPATCHED':
                raise StudyStorageError('OBSERVATION_CANNOT_REWRITE_SLOT')
            row, _ = self._study(conn, slot['study_id'])
            attempt = conn.execute('SELECT usage_estimate,actual_charge,status FROM pilot_attempts WHERE call_id=? AND pilot_id=?', (slot['call_id'], row['namespace'])).fetchone()
            if not attempt or attempt['usage_estimate'] is not None or attempt['actual_charge'] is not None or attempt['status'] != 'unknown_reserved':
                raise StudyStorageError('FULL_LIABILITY_REQUIRED')
            conn.execute("UPDATE research_slots SET state='RECORDED',outcome='FAILED',observation_json=? WHERE slot_id=?", (payload, slot_id))
            self._event(conn, row['study_id'], 'RECORDED', at, slot_id)
            if row['state'] not in TERMINAL and row['state'] != 'CANCEL_REQUESTED':
                self._state(conn, row, pause_state, at, 'AWAITING_VERIFIED_RECONCILIATION')
            return evidence

    def execution_report(self, study_id):
        """Safe polling projection. No payload, admission secret, envelope or observation body."""
        with self._read() as conn:
            row, plan = self._study(conn, study_id)
            view = SafePlanView.model_validate_json(row['safe_json'])
            if view.manifest_hash != plan.manifest_hash:
                raise StudyStorageError('SAFE_VIEW_INTEGRITY_MISMATCH')
            admitted = conn.execute('SELECT 1 FROM research_admissions WHERE study_id=?', (row['study_id'],)).fetchone()
            request = plan.payload['request']
            cost = budget_ledger.summary(conn, row['namespace'], Decimal(request['requested_budget_usd']), request['requested_max_attempts']) if admitted else None
            children = {}
            for child in conn.execute('SELECT c.strategy,c.run_id,r.status FROM research_children c JOIN runs r USING(run_id) WHERE c.study_id=?', (row['study_id'],)):
                children[child['strategy']] = {'run_id': child['run_id'], 'status': child['status'], 'origin': 'source'}
            replays = {}
            for strategy in view.strategy_ids:
                found = conn.execute('SELECT run_id,status,config_json FROM runs WHERE idempotency_key LIKE ? ORDER BY rowid DESC LIMIT 1', (row['study_id'] + ':replay:' + strategy + ':%',)).fetchone()
                if not found:
                    continue
                mode = json.loads(found['config_json']).get('llm_mode')
                if mode == 'replay':
                    replays[strategy] = {'run_id': found['run_id'], 'status': found['status'], 'origin': 'replay'}
            coverage = {}
            for strategy in view.strategy_ids:
                coverage[strategy] = children[strategy]['status'] if strategy in children else 'NOT_RUN'
            evidence = []
            for slot in conn.execute('SELECT call_id,observation_json FROM research_slots WHERE study_id=? AND observation_json IS NOT NULL ORDER BY call_id', (row['study_id'],)):
                evidence.append({'call_id': slot['call_id'], 'evidence_sha256': canonical_hash(json.loads(slot['observation_json']))})
            events = [event['event_type'] for event in conn.execute('SELECT event_type FROM research_events WHERE study_id=? ORDER BY sequence', (row['study_id'],))]
            return {'study_id': row['study_id'], 'state': row['state'], 'reason': row['reason'],
                'plan': view.model_dump(mode='json'), 'cost': cost, 'coverage': coverage,
                'source_runs': children, 'replay_runs': replays, 'attempt_evidence': evidence,
                'event_types': events}
