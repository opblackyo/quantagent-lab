"""One research store, one serial worker, and an explicit synthetic fixture.

Importing this module does not open a database, construct a provider, or bind a
port. The default application does not call build_fixture_runtime.
"""
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from .data import ROOT, DatasetRegistry, canonical_hash, sha256
from .models import ENGINE_VERSION, FEATURE_VERSION, PROMPT_VERSION
from .executor import BoundedExecutor
from .research_contracts import ResearchSettings, ServerResearchProfile, SourceContext, StudyRequest
from .research_service import (
    PROVIDER_ENDPOINTS, PlanningError, acceptance_start_refusal, execute_study, plan_study, poll_study, replay_study, resolve_profile, resume_study,
)
from .research_store import PlanningInputs, ResearchStore, ServerAuthorization, StudyStorageError, namespace_for

FIXTURE_LABELS = (
    "SYNTHETIC_FIXTURE",
    "NOT_LIVE_VERIFIED_API",
    "PRICE_ONLY_DIVIDENDS_EXCLUDED",
    "GATEWAY_LABEL_NOT_UPSTREAM_IDENTITY",
    "REQUESTED_CAP_IS_NOT_FUNDING",
)
SCENARIOS = ("success", "delayed", "timeout", "drift", "cancel", "acceptance-a", "acceptance-b")
_OUTPUTS = {
    "analysis_output": {"regime": "bullish", "thesis": "Synthetic fixture compares trailing averages.", "evidence": ["ma20", "ma60"]},
    "critic_output": {"risk_level": "low", "risk_flags": ["NO_OBJECTION"], "counterarguments": []},
    "decision_output": {"target_weight": 1, "confidence": 0.6, "rationale": "Synthetic fixture target.", "evidence": ["ma20"], "acknowledged_risks": []},
}
_TOKEN = "synthetic-fixture-token"


class ResearchRuntime:
    """Serial fixture executor. A start click does not create real spending authority."""

    def __init__(self, store, profiles, grants, context, registry, clients, token=_TOKEN):
        self.store = store
        self.profiles = dict(profiles)
        self.grants = dict(grants)
        self.context = context
        self.registry = registry
        self.clients = clients
        self.token = token
        self.fixture = True
        self.executor = None
        self._gate = threading.RLock()
        self._inflight = set()
        self._replays = {}
        self._posts = {}
        self._unscoped_posts = 0
        self._local = threading.local()
        self._evidence_open = set()
        self._started = context.checked_at
        self._monotonic = time.monotonic()
        self._last = context.checked_at
        self.live_bindings = {}
        self.executor = BoundedExecutor(thread_name_prefix="research-fixture")

    def clock(self):
        with self._gate:
            value = self._started + timedelta(seconds=time.monotonic() - self._monotonic)
            if value < self._last:
                raise PlanningError("INVALID_EXECUTION_CLOCK")
            self._last = value
            return value

    def secret_values(self):
        values = [self.token, *PROVIDER_ENDPOINTS.values()]
        for grant in self.grants.values():
            values.extend((grant["key_ref"], grant["cohort_ref"], grant["authorization_id"]))
        for profile in self.profiles.values():
            for item in profile.private_evidence:
                values.append(item.reference_id)
        for binding in self.live_bindings.values():
            values.extend(binding.redactions)
        return tuple(dict.fromkeys(values))

    def total_posts(self):
        with self._gate:
            return self._unscoped_posts + sum(self._posts.values())

    def posts_for(self, study_id):
        with self._gate:
            return self._posts.get(str(study_id), 0)

    def note_post(self):
        study = getattr(self._local, "study", None)
        with self._gate:
            if study is None:
                self._unscoped_posts += 1
            else:
                self._posts[study] = self._posts.get(study, 0) + 1

    def readiness(self):
        profiles = []
        for scenario in SCENARIOS:
            profile = self.profiles[scenario]
            profiles.append({"profile_id": profile.profile_id, "provider": profile.provider, "model_id": profile.model_id,
                             "identity_scope": profile.identity_scope, "dataset_id": profile.dataset_id, "dataset_kind": profile.dataset_kind,
                             "allowed_start": profile.allowed_start.isoformat(), "allowed_end": profile.allowed_end.isoformat(),
                             "label": "synthetic-fixture-" + scenario, "scenario": scenario})
        seen = {item["profile_id"] for item in profiles}
        for binding in self.live_bindings.values():
            profile = binding.profile
            if profile.profile_id in seen:
                continue
            seen.add(profile.profile_id)
            profiles.append({"profile_id": profile.profile_id, "provider": profile.provider, "model_id": profile.model_id,
                             "identity_scope": profile.identity_scope, "dataset_id": profile.dataset_id, "dataset_kind": profile.dataset_kind,
                             "allowed_start": profile.allowed_start.isoformat(), "allowed_end": profile.allowed_end.isoformat(),
                             "label": "synthetic-offline-live", "scenario": "offline-live"})
        labels = list(FIXTURE_LABELS)
        if self.live_bindings:
            labels.append("OFFLINE_COMPOSITION_NOT_SPEND_APPROVAL")
        return {"service": "synthetic-fixture", "live_provider": False, "real_spending_authorized": False, "execution_authorized": False,
                "funding": "synthetic-fixture-not-real-money", "fixture_transport_posts": self.total_posts(), "profiles": profiles,
                "held_approvals": [binding.public_scope() for binding in self.live_bindings.values()],
                "labels": labels, "queue_limit": 1}

    def preview(self, request: StudyRequest):
        profile = resolve_profile(request.profile_id, {item.profile_id: item for item in self.profiles.values()})
        _frozen, view = plan_study(request, profile, self.registry, context=self.context)
        return view

    def _inputs(self, request):
        profile = resolve_profile(request.profile_id, {item.profile_id: item for item in self.profiles.values()})
        return PlanningInputs(request, profile, self.registry, self.context), profile

    def _scenario(self, profile):
        for name, item in self.profiles.items():
            if item.profile_id == profile.profile_id:
                return name
        raise PlanningError("PROFILE_NOT_ALLOWLISTED")

    def server_live_ready(self, request, manifest_hash):
        """True only when a server-held binding matches this exact plan. Not a spend grant."""
        try:
            profile = resolve_profile(request.profile_id, {item.profile_id: item for item in self.profiles.values()})
        except PlanningError:
            return False
        binding = self.live_bindings.get((profile.profile_id, manifest_hash))
        if binding is None:
            return False
        return binding.permits(request, profile, manifest_hash, self.clock()) is None

    def install_offline_binding(self, binding):
        """Register one server-held binding. There is no HTTP route for this."""
        self.profiles.setdefault("offline-" + binding.profile.profile_id, binding.profile)
        self.live_bindings[(binding.profile.profile_id, binding.approval.manifest_hash)] = binding
        binding.note = self.note_post

    def submit(self, request: StudyRequest, manifest_hash: str, idempotency_key: str):
        inputs, profile = self._inputs(request)
        frozen, view = plan_study(request, profile, self.registry, context=self.context)
        refusal = acceptance_start_refusal(view)
        if refusal:
            raise PlanningError(refusal)
        if frozen.manifest_hash != manifest_hash:
            raise PlanningError("STALE_MANIFEST")
        binding = None
        if request.transport_intent == "live":
            binding = self.live_bindings.get((profile.profile_id, frozen.manifest_hash))
            if binding is None:
                raise PlanningError("LIVE_APPROVAL_REQUIRED")
            mismatch = binding.permits(request, profile, frozen.manifest_hash, self.clock())
            if mismatch:
                raise PlanningError(mismatch)
        with self._gate:
            existing = self.store.find_study(idempotency_key)
            if existing and existing["manifest_hash"] != manifest_hash:
                raise StudyStorageError("IDEMPOTENCY_CONFLICT")
            if existing and existing["state"] not in {'DRAFT', 'FROZEN', 'QUEUED'}:
                return str(existing["study_id"])
            if existing and str(existing["study_id"]) in self._inflight:
                return str(existing["study_id"])
            if self._busy_other():
                raise PlanningError("WORK_QUEUE_FULL")
            with self.executor.admission() as submit:
                study_id = str(self._admit(inputs, profile, frozen, idempotency_key, None if binding is None else binding.grant))
                self._inflight.add(study_id)
                try:
                    submit(self._run, study_id, inputs, None if binding else self._scenario(profile), binding)
                except BaseException:
                    self._inflight.discard(study_id)
                    raise
        return study_id

    def _busy_other(self, study_id=None):
        return any(item != study_id and self.store.get_study(item)['state'] in {'QUEUED','RUNNING','CANCEL_REQUESTED'}
                   for item in self._inflight)

    def _ack_cancel(self, study_id):
        if study_id not in self._inflight and self.store.get_study(study_id)['state'] == 'CANCEL_REQUESTED':
            self.store.finish(study_id, 'CANCELLED', at=self.clock())

    def _admit(self, inputs, profile, frozen, idempotency_key, grant=None):
        at = self.clock()
        study_id = self.store.register(frozen, inputs, idempotency_key=idempotency_key)
        state = self.store.get_study(study_id)["state"]
        if state == "DRAFT":
            self.store.freeze(study_id, inputs, at=at)
            state = "FROZEN"
        if state == "FROZEN":
            if grant is None:
                grant = self.grants[self._scenario(profile)]
            if request_exceeds(inputs.request, grant):
                raise PlanningError("AUTHORIZATION_UNAVAILABLE")
            authorization_id = "a" + str(study_id).replace("-", "")
            authorization = ServerAuthorization(authorization_id=authorization_id, study_id=study_id, manifest_hash=frozen.manifest_hash,
                budget_namespace=namespace_for(study_id, frozen.manifest_hash), profile_id=profile.profile_id, profile_revision=profile.revision,
                source_hash=self.context.source_hash, key_ref=grant["key_ref"], cohort_ref=grant["cohort_ref"],
                cap_usd=inputs.request.requested_budget_usd, max_attempts=inputs.request.requested_max_attempts,
                valid_from=profile.valid_from, expires_at=profile.expires_at, evidence_sha256=grant["evidence_sha256"])
            self.store.record_authorization(authorization, at=at)
            self.store.start(study_id, authorization.authorization_id, inputs, at=at)
        return study_id

    def _run(self, study_id, inputs, scenario, binding=None):
        study_id = str(study_id)
        self._local.study = study_id
        try:
            if self.store.get_study(study_id)["state"] != "QUEUED":
                return
            if binding is None:
                execute_study(self.store, study_id, inputs, at=self.clock(), service_enabled=True, client=self.clients[scenario],
                              authorization=self.token, billing=lambda at: billing_document(inputs.profile, self.grants[scenario]["key_ref"], at),
                              fault=self._fault(study_id, scenario), secrets=self.secret_values(), usage_evidence=lambda store, study, slot: self._usage(store, study, slot, scenario),
                              clock=self.clock)
            else:
                execute_study(self.store, study_id, inputs, at=self.clock(), service_enabled=True, client=binding.client,
                              billing=binding.preflight, fault=None if binding.fault_factory is None else binding.fault_factory(self.store, study_id),
                              secrets=binding.redactions, usage_evidence=binding.usage, clock=self.clock, live_binding=binding)
        except Exception:
            self._pause_quietly(study_id)
        finally:
            self._local.study = None
            with self._gate:
                self._inflight.discard(study_id)
                self._ack_cancel(study_id)

    def _pause_quietly(self, study_id):
        """Keep the ledger and record a stable pause. Exception text is not stored."""
        try:
            if self.store.get_study(study_id)["state"] in {"QUEUED", "RUNNING"}:
                self.store.pause(study_id, "PAUSED_BILLING", at=self.clock(), detail="EXECUTION_FAILED")
        except Exception:
            return

    def _fault(self, study_id, scenario):
        if scenario != "cancel":
            return None
        state = {"seen": False}

        def fault(name):
            if name != "BEFORE_ROLE" or state["seen"]:
                return
            state["seen"] = True
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.store.get_study(study_id)["state"] in {"CANCEL_REQUESTED", "CANCELLED"}:
                    return
                time.sleep(0.05)
        return fault

    def _usage(self, store, study_id, slot_id, scenario):
        if scenario == "delayed" and str(study_id) not in self._evidence_open:
            raise ValueError("synthetic billing evidence is not ready")
        plan = store.load_plan(study_id)
        return synthetic_aggregate(store, study_id, plan, only_pending=True)

    def cancel(self, study_id):
        study_id = str(study_id)
        self.store.request_cancel(study_id, at=self.clock())
        with self._gate:
            self._ack_cancel(study_id)
        return poll_study(self.store, study_id)

    def reconcile(self, study_id):
        """Apply server-held synthetic evidence. The browser cannot supply a bill."""
        report = poll_study(self.store, study_id)
        scenario = self._scenario_of(report)
        if scenario is None:
            return "NOT_ELIGIBLE"
        plan = self.store.load_plan(study_id)
        if scenario == "delayed":
            if not str(report["state"]).startswith("PAUSED"):
                return "NOT_ELIGIBLE"
            self._evidence_open.add(str(study_id))
        evidence = synthetic_aggregate(self.store, study_id, plan, only_pending=False)
        if not evidence["covered_call_ids"]:
            return "NOT_ELIGIBLE"
        from .pilot_billing import reconcile_study_usage
        reconcile_study_usage(self.store, study_id, evidence)
        return "RECONCILED"

    def resume(self, study_id):
        study_id = str(study_id)
        report = poll_study(self.store, study_id)
        scenario = self._scenario_of(report)
        stored = self.store.load_plan(study_id)
        request = StudyRequest.model_validate(stored.payload["request"])
        inputs, profile = self._inputs(request)
        binding = self.live_bindings.get((profile.profile_id, stored.manifest_hash)) if request.transport_intent == "live" else None
        with self._gate:
            state = self.store.get_study(study_id)["state"]
            if study_id in self._inflight and state in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
                return study_id
            if self._busy_other(study_id):
                raise PlanningError("WORK_QUEUE_FULL")
            eligibility = self.store.resume_eligibility(study_id, inputs, at=self.clock())
            if study_id in self._inflight:
                # The original worker can still be inside its finally after a pause.
                # A second resume is idempotent only when this study is already eligible.
                raise PlanningError('WORKER_BUSY' if eligibility['can_queue'] else 'RESUME_NOT_ELIGIBLE')
            if not eligibility["can_queue"]:
                raise PlanningError("RESUME_NOT_ELIGIBLE")
            with self.executor.admission() as submit:
                self._inflight.add(study_id)
                try:
                    submit(self._resume, study_id, inputs, scenario, binding)
                except BaseException:
                    self._inflight.discard(study_id)
                    raise
        return study_id

    def _resume(self, study_id, inputs, scenario, binding=None):
        self._local.study = study_id
        try:
            if binding is None:
                resume_study(self.store, study_id, inputs, at=self.clock(), service_enabled=True, client=self.clients[scenario],
                             authorization=self.token, billing=lambda at: billing_document(inputs.profile, self.grants[scenario]["key_ref"], at),
                             fault=self._fault(study_id, scenario), secrets=self.secret_values(),
                             usage_evidence=lambda store, study, slot: self._usage(store, study, slot, scenario), clock=self.clock)
            else:
                resume_study(self.store, study_id, inputs, at=self.clock(), service_enabled=True, client=binding.client,
                             billing=binding.preflight, fault=None if binding.fault_factory is None else binding.fault_factory(self.store, study_id),
                             secrets=binding.redactions, usage_evidence=binding.usage, clock=self.clock, live_binding=binding)
        except Exception:
            self._pause_quietly(study_id)
        finally:
            self._local.study = None
            with self._gate:
                self._inflight.discard(study_id)
                self._ack_cancel(study_id)

    def replay(self, study_id):
        study_id = str(study_id)
        stored = self.store.load_plan(study_id)
        request = StudyRequest.model_validate(stored.payload["request"])
        inputs, _profile = self._inputs(request)
        with self._gate:
            future = self._replays.get(study_id)
            if future is None:
                if study_id in self._inflight or self.store.get_study(study_id)['state'] in {'QUEUED', 'RUNNING', 'CANCEL_REQUESTED'}:
                    raise PlanningError('WORKER_BUSY')
                future = self.executor.submit(self._replay, study_id, inputs)
                self._replays[study_id] = future
                self._inflight.add(study_id)
                future.add_done_callback(lambda item: self._finish_replay(study_id, item))
        return future.result()

    def _finish_replay(self, study_id, future):
        with self._gate:
            if self._replays.get(study_id) is future:
                self._replays.pop(study_id)
                self._inflight.discard(study_id)
                self._ack_cancel(study_id)

    def _replay(self, study_id, inputs):
        before = self.posts_for(study_id)
        result = replay_study(self.store, study_id, inputs, at=self.clock())
        if self.posts_for(study_id) != before or result["posts"] != 0:
            raise PlanningError("REPLAY_SENT")
        report = self.public_report(study_id)
        report["replay_posts"] = 0
        report["replay_equal"] = {name: bool(item["equal"]) for name, item in result["replays"].items()}
        return report

    def _scenario_of(self, report):
        profile_id = report["plan"]["profile_id"]
        # A held live approval takes precedence even if its profile ID matches
        # a fixture name. Never route a live study to synthetic reconciliation.
        if any(binding.profile.profile_id == profile_id for binding in self.live_bindings.values()):
            return None
        for name, profile in self.profiles.items():
            if profile.profile_id == profile_id and name in SCENARIOS:
                return name
        raise PlanningError("PROFILE_NOT_ALLOWLISTED")

    def public_report(self, study_id):
        report = poll_study(self.store, study_id)
        report["progress"] = self.store.slot_progress(study_id)
        report["fixture_transport_posts"] = self.posts_for(study_id)
        report["labels"] = list(FIXTURE_LABELS)
        with self._gate:
            report['worker_busy'] = str(study_id) in self._inflight
        return report

    def close(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
        for client in self.clients.values():
            client.close()
        for binding in self.live_bindings.values():
            binding.client.close()
        self.store.close()


def request_exceeds(request, grant):
    return request.requested_budget_usd > grant["cap_usd"] or request.requested_max_attempts > grant["max_attempts"]


def billing_document(profile, key_ref, at):
    from zoneinfo import ZoneInfo
    binding = next(item for item in profile.private_evidence if item.kind == "binding")
    return {"kind": "synthetic-readonly-billing-v1", "semantics": "token-effective-usd", "provider": profile.provider,
            "model_id": profile.model_id, "endpoint": PROVIDER_ENDPOINTS[profile.provider], "pricing_unit": profile.pricing_unit,
            "multiplier": "1", "service_tier": profile.settings.service_tier or "auto",
            "input_usd_per_million": str(profile.input_usd_per_million), "output_usd_per_million": str(profile.output_usd_per_million),
            "cache_read_usd_per_million": str(profile.cache_read_usd_per_million), "cache_write_usd_per_million": str(profile.cache_write_usd_per_million),
            "binding_ref": binding.reference_id, "binding_sha256": binding.sha256, "group_ref": key_ref,
            "valid_from": profile.valid_from.isoformat(), "expires_at": profile.expires_at.isoformat(),
            "as_of_date": at.astimezone(ZoneInfo("UTC")).date().isoformat(), "timezone": "UTC", "day_count": 1,
            "external_same_key_requests": 0, "delayed": False}


def synthetic_aggregate(store, study_id, plan, only_pending=False):
    slots = [slot for slot in store.list_slots(study_id) if slot["outcome"] in ("SUCCEEDED", "FAILED") and slot["observation_json"] and '"sent":true' in slot["observation_json"]]
    covered, hashes = [], {}
    incoming = outgoing = 0
    with store._read() as conn:
        held = {row["call_id"] for row in conn.execute("SELECT call_id FROM pilot_attempts WHERE status='usage_estimated_held'")}
        estimated = {row["call_id"] for row in conn.execute("SELECT call_id FROM pilot_attempts WHERE status IN ('usage_estimated','usage_estimated_held')")}
    for slot in slots:
        if only_pending and slot["call_id"] not in held:
            continue
        if not only_pending and slot["call_id"] not in estimated:
            continue
        observed = json.loads(slot["observation_json"])
        if observed.get("usage_estimate_usd") is None:
            continue
        covered.append(slot["call_id"])
        hashes[slot["call_id"]] = canonical_hash(observed)
        incoming += observed["input_tokens"]
        outgoing += observed["output_tokens"]
    profile = plan.payload["server_profile"]
    actual = (Decimal(incoming) * Decimal(profile["input_usd_per_million"]) + Decimal(outgoing) * Decimal(profile["output_usd_per_million"])) / Decimal(1000000)
    return {"kind": "synthetic-aggregate-usage-v1", "unit": "USD", "delayed": False, "external_requests": 0, "day_count": 1,
            "covered_call_ids": covered, "attempt_evidence_sha256": hashes, "input_tokens": incoming, "output_tokens": outgoing,
            "requests": len(covered), "actual_charge_usd": str(actual), "manifest_hash": plan.manifest_hash,
            "dataset_hash": plan.payload["request"]["expected_dataset_hash"], "source_hash": plan.payload["source"]["source_hash"]}


def fixture_client(runtime, scenario):
    seen = {"n": 0}

    def handler(request):
        runtime.note_post()
        seen["n"] += 1
        if scenario == "timeout":
            raise httpx.ReadTimeout("synthetic inactivity")
        name = json.loads(request.content)["text"]["format"]["name"]
        raw = json.dumps(_OUTPUTS[name])
        usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        envelope = {"id": f"synthetic-response-{scenario}-{seen['n']}", "model": "other-model" if scenario == "drift" else "synthetic-model-v1",
                    "status": "completed", "service_tier": "auto", "output": [{"type": "message", "status": "completed", "content": [{"type": "output_text", "text": raw}]}],
                    "usage": usage}
        return httpx.Response(200, json=envelope)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _git_commit():
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        commit = "0" * 40
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        return "0" * 40
    return commit


def _source_context(at):
    import importlib.metadata
    files = sorted((ROOT / "backend" / "app").glob("*.py"))
    source = canonical_hash({str(path.relative_to(ROOT)).replace("\\", "/"): path.read_bytes().hex() for path in files})
    names = ("exchange-calendars", "fastapi", "httpx", "langgraph", "numpy", "pandas", "pydantic", "uvicorn")
    versions = tuple((name, importlib.metadata.version(name)) for name in names)
    return SourceContext(source_commit=_git_commit(), source_hash=source, uv_lock_hash=sha256((ROOT / "uv.lock").read_bytes()),
                         frontend_lock_hash=sha256((ROOT / "frontend" / "package-lock.json").read_bytes()),
                         dependency_versions=versions, checked_at=at, engine_version=ENGINE_VERSION,
                         feature_version=FEATURE_VERSION, prompt_version=PROMPT_VERSION)


def build_fixture_runtime(db_path: Path):
    """Create a new Phase2 database and synthetic allowlist. An existing file is refused."""
    path = Path(db_path)
    if path.exists():
        raise StudyStorageError("FIXTURE_DB_EXISTS")
    checked = datetime.now(timezone.utc).replace(microsecond=0)
    context = _source_context(checked)
    registry = DatasetRegistry([ROOT / "data" / "sample"])
    dataset = registry.load("synthetic-spy-v1")
    if dataset.metadata.get("kind") != "synthetic":
        raise PlanningError("DATASET_INVALID_OR_UNAVAILABLE")
    profiles, grants = {}, {}
    for scenario in SCENARIOS:
        profile = ServerResearchProfile(profile_id="fixture-" + scenario, revision=1, provider="kuaiapi", model_id="synthetic-model-v1",
            identity_scope="gateway-reported-label-only", dataset_id=dataset.metadata["id"], dataset_hash=dataset.hash, dataset_kind="synthetic",
            allowed_start="2023-01-01", allowed_end="2023-01-31", max_budget_usd="1", max_attempts=32, attempts_per_role=1,
            input_usd_per_million="0.2", output_usd_per_million="0.8", cache_read_usd_per_million="0.01", cache_write_usd_per_million="0.3",
            settings=ResearchSettings(max_output_tokens=1024), valid_from=checked - timedelta(hours=1), expires_at=checked + timedelta(days=7),
            expected_source_hash=context.source_hash, expected_uv_lock_hash=context.uv_lock_hash, expected_frontend_lock_hash=context.frontend_lock_hash,
            private_evidence=({"kind": "pricing", "reference_id": "fixture-price-ref", "sha256": "e" * 64},
                              {"kind": "binding", "reference_id": "fixture-binding-ref", "sha256": "f" * 64}))
        profiles[scenario] = profile
        grants[scenario] = {"authorization_id": "grant-fixture-" + scenario, "key_ref": "fixture-" + scenario + "-key",
                            "cohort_ref": "fixture-" + scenario + "-cohort", "cap_usd": Decimal("1"), "max_attempts": 32,
                            "evidence_sha256": canonical_hash({"kind": "synthetic-fixture-grant", "profile_id": profile.profile_id})}
    store = ResearchStore.create_new(path)
    runtime = ResearchRuntime(store, profiles, grants, context, registry, {})
    runtime.clients = {scenario: fixture_client(runtime, scenario) for scenario in SCENARIOS}
    return runtime


def create_offline_live_from_environ():
    """Fixture app plus two server-held synthetic approvals. Does not read provider keys."""
    import os
    from datetime import date

    from .live_composition import CountingCredentialSource, HeldLiveBinding
    from .live_readiness import ServerLiveApproval
    from .local_http import loopback_origins
    from .research_contracts import ServerResearchProfile

    raw = os.environ.get("QUANTAGENT_FIXTURE_DB", "")
    spec = os.environ.get("QUANTAGENT_OFFLINE_LIVE_FILE", "")
    if not raw or not spec:
        raise RuntimeError("QUANTAGENT_FIXTURE_DB and QUANTAGENT_OFFLINE_LIVE_FILE are required")
    origins = loopback_origins(os.environ.get("QUANTAGENT_LOCAL_ORIGINS", "").split(","))
    runtime = build_fixture_runtime(Path(raw))
    payload = json.loads(Path(spec).read_text(encoding="utf-8"))
    credential = payload.get("credential")
    profile = ServerResearchProfile.model_validate(payload["profile"])
    if not isinstance(credential, str) or not credential or credential.strip() != credential:
        raise RuntimeError("synthetic credential rejected")
    loader = CountingCredentialSource({profile.profile_id: credential})
    runtime.offline_loader = loader
    dataset = runtime.registry.load(profile.dataset_id)
    pricing = next(item for item in profile.private_evidence if item.kind == "pricing")
    endpoint = PROVIDER_ENDPOINTS[profile.provider]
    for protocol, attempts, key_ref, cohort in (
        ("acceptance-stage-a-v1", 1, "synthetic-live-a-group", "synthetic-live-a-cohort"),
        ("acceptance-stage-b-v1", 3, "synthetic-live-b-group", "synthetic-live-b-cohort"),
    ):
        request = StudyRequest(
            profile_id=profile.profile_id, dataset_id=profile.dataset_id, expected_dataset_hash=dataset.hash,
            start_date=date(2023, 1, 27), end_date=date(2023, 1, 31), requested_budget_usd=Decimal("1"),
            requested_max_attempts=attempts, study_protocol=protocol, transport_intent="live", overall_deadline_seconds=45)
        frozen, _view = plan_study(request, profile, runtime.registry, context=runtime.context)
        approval = ServerLiveApproval(holder="server", funding_approved=True, manifest_hash=frozen.manifest_hash, profile_id=profile.profile_id)
        holder = {}

        def handler(http_request, holder=holder):
            from .live_composition import offline_handler
            return offline_handler(holder["binding"])(http_request)

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, http2=False)
        binding = HeldLiveBinding(
            profile=profile, approval=approval, tariff_id=pricing.reference_id, not_before=profile.valid_from,
            expires_at=profile.expires_at, attempt_budget=attempts, endpoint=endpoint, loader=loader, client=client,
            grant={"authorization_id": "grant-" + protocol, "key_ref": key_ref, "cohort_ref": cohort,
                   "cap_usd": Decimal("1"), "max_attempts": attempts,
                   "evidence_sha256": canonical_hash({"kind": "synthetic-offline-grant", "protocol": protocol})},
            deadline_seconds=45, redactions=(credential, endpoint, key_ref, cohort), seen={"n": 0})
        holder["binding"] = binding
        runtime.install_offline_binding(binding)
    from .main import create_app
    app = create_app(research=runtime, local_origins=origins)
    app.state.research = runtime
    return app


def create_operator_live_from_environ(*, transport_factory=None):
    """Operator live ASGI factory. Binds approved manifests and lazy file credentials without reading keys at startup."""
    import os
    from .live_composition import load_operator_bindings, offline_handler
    from .local_http import loopback_origins

    raw = os.environ.get("QUANTAGENT_FIXTURE_DB", "")
    spec = os.environ.get("QUANTAGENT_LIVE_OPERATOR_FILE", "")
    if not raw or not spec:
        raise RuntimeError("QUANTAGENT_FIXTURE_DB and QUANTAGENT_LIVE_OPERATOR_FILE are required")
    if transport_factory is None and os.environ.get("QUANTAGENT_OPERATOR_SYNTHETIC_TRANSPORT") == "offline-mock-v1":
        transport_factory = lambda holder: httpx.MockTransport(lambda req: offline_handler(holder["binding"])(req))
    origins = loopback_origins(os.environ.get("QUANTAGENT_LOCAL_ORIGINS", "").split(","))
    runtime = build_fixture_runtime(Path(raw))
    try:
        load_operator_bindings(Path(spec), runtime=runtime, transport_factory=transport_factory)
    except BaseException:
        runtime.close()
        raise
    from .main import create_app
    app = create_app(research=runtime, local_origins=origins)
    app.state.research = runtime
    return app


def create_from_environ():
    """Uvicorn factory. Reads only the fixture database path and loopback origins."""
    import os
    raw = os.environ.get("QUANTAGENT_FIXTURE_DB", "")
    if not raw:
        raise RuntimeError("QUANTAGENT_FIXTURE_DB is required")
    from .local_http import loopback_origins
    origins = loopback_origins(os.environ.get("QUANTAGENT_LOCAL_ORIGINS", "").split(","))
    runtime = build_fixture_runtime(Path(raw))
    from .main import create_app
    app = create_app(research=runtime, local_origins=origins)
    app.state.research = runtime
    return app
