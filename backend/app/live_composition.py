"""Server-owned live composition. The browser cannot construct or submit it.

A binding matches one frozen manifest. It does not grant money. The credential
loader runs only inside send_live_attempt, after the durable reservation.
"""
import json
from datetime import datetime
from pathlib import Path

import httpx

from .agents import Reply, redact_secrets, response_request
from .live_readiness import LiveRefusal, endpoint_allowed, send_live_attempt
from .research_service import PROVIDER_ENDPOINTS


_OUTPUTS = {
    "analysis_output": {"regime": "bullish", "thesis": "Synthetic offline composition compares trailing averages.", "evidence": ["ma20", "ma60"]},
    "critic_output": {"risk_level": "low", "risk_flags": ["NO_OBJECTION"], "counterarguments": []},
    "decision_output": {"target_weight": 1, "confidence": 0.6, "rationale": "Synthetic offline composition target.", "evidence": ["ma20"], "acknowledged_risks": []},
}


def load_server_profile(path):
    """Parse a server-local profile file. Reject embedded credentials."""
    from .research_contracts import ServerResearchProfile

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LiveRefusal("LIVE_PROFILE_REJECTED")
    for banned in ("api_key", "secret", "authorization", "credential", "endpoint"):
        if banned in raw:
            raise LiveRefusal("LIVE_PROFILE_REJECTED")
    return ServerResearchProfile.model_validate(raw)


def production_document(profile, key_ref, at, *, overrides=None, covered=None, provenance_method="synthetic-document-sha256-v1"):
    """Synthetic production-shaped document. Provenance is not an invoice."""
    from zoneinfo import ZoneInfo

    from .data import canonical_hash

    binding = next(item for item in profile.private_evidence if item.kind == "binding")
    pricing = next(item for item in profile.private_evidence if item.kind == "pricing")
    evidence_source = (
        "operator-verified-binding"
        if provenance_method == "operator-verified-server-binding-v1"
        else "synthetic-document"
    )
    body = {
        "kind": "production-shaped-usage-v1",
        "provider": profile.provider,
        "model_id": profile.model_id,
        "endpoint": PROVIDER_ENDPOINTS[profile.provider],
        "pricing_unit": profile.pricing_unit,
        "tariff_id": pricing.reference_id,
        "group_ref": key_ref,
        "input_usd_per_million": str(profile.input_usd_per_million),
        "output_usd_per_million": str(profile.output_usd_per_million),
        "cache_read_usd_per_million": str(profile.cache_read_usd_per_million),
        "cache_write_usd_per_million": str(profile.cache_write_usd_per_million),
        "binding_ref": binding.reference_id,
        "binding_sha256": binding.sha256,
        "cache_status": "observed-consistent",
        "delayed": False,
        "external_same_key_requests": 0,
        "valid_from": profile.valid_from.isoformat(),
        "expires_at": profile.expires_at.isoformat(),
        "as_of_date": at.astimezone(ZoneInfo("UTC")).date().isoformat(),
        "timezone": "UTC",
        "day_count": 1,
        "evidence_source": evidence_source,
    }
    if covered is not None:
        body["covered_call_ids"] = list(covered)
    if overrides:
        body.update(overrides)
    clean = {key: value for key, value in body.items() if key != "provenance"}
    body["provenance"] = {"method": provenance_method, "sha256": canonical_hash(clean)}
    return body


class CountingCredentialSource:
    """In-memory synthetic secret. load is the only read."""

    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def load(self, profile_id):
        self.calls.append(profile_id)
        if profile_id not in self.values:
            raise LiveRefusal("LIVE_CREDENTIAL_REJECTED")
        return self.values[profile_id]


def _check_private_file_path(path, *, code="LIVE_KEY_ORIGIN_REJECTED") -> Path:
    """Validate that a local file path is private and untracked without reading its bytes."""
    import subprocess
    from .data import ROOT

    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise LiveRefusal(code)
    raw = Path(path)
    try:
        if raw.is_symlink():
            raise LiveRefusal(code)
        resolved = raw.resolve()
        if not resolved.exists() or not resolved.is_file() or resolved.is_symlink():
            raise LiveRefusal(code)
    except OSError as exc:
        raise LiveRefusal(code) from exc
    if resolved.is_relative_to(ROOT):
        ignored = subprocess.run(["git", "check-ignore", "-q", str(resolved)], cwd=ROOT).returncode == 0
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(resolved)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not ignored or tracked:
            raise LiveRefusal(code)
    return resolved


class ScopedKeyFileLoader:
    """Lazy private key-file loader. Never reads the file until send_live_attempt reaches load."""

    def __init__(self, *, profile_id: str, provider: str, key_file: str | Path, key_ref: str):
        if (
            not isinstance(profile_id, str) or not profile_id.strip()
            or provider not in {"kuaiapi", "openai"}
            or not isinstance(key_ref, str) or not key_ref.strip()
        ):
            raise LiveRefusal("LIVE_KEY_ORIGIN_REJECTED")
        self.profile_id = profile_id
        self.provider = provider
        self.key_ref = key_ref
        self.key_file = _check_private_file_path(key_file, code="LIVE_KEY_ORIGIN_REJECTED")
        self.calls: list[str] = []
        self.loaded_secrets: list[str] = []

    def load(self, profile_id: str) -> str:
        if profile_id != self.profile_id:
            raise LiveRefusal("LIVE_CREDENTIAL_SCOPE_MISMATCH")
        resolved = _check_private_file_path(self.key_file, code="LIVE_KEY_ORIGIN_REJECTED")
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise LiveRefusal("LIVE_CREDENTIAL_REJECTED") from exc
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        expected = "KUAIAPI_API_KEY" if self.provider == "kuaiapi" else "OPENAI_API_KEY"
        if len(lines) != 1:
            raise LiveRefusal("LIVE_CREDENTIAL_REJECTED")
        name, sep, value = lines[0].partition("=")
        if (
            sep != "="
            or name != expected
            or not value
            or any(ch.isspace() for ch in value)
            or value[0] in "\"'"
            or value[-1] in "\"'"
        ):
            raise LiveRefusal("LIVE_CREDENTIAL_REJECTED")
        self.calls.append(profile_id)
        if value not in self.loaded_secrets:
            self.loaded_secrets.append(value)
        return value


class HeldLiveBinding:
    """One exact manifest, profile, tariff reference, expiry and attempt budget."""

    def __init__(self, *, profile, approval, tariff_id, not_before, expires_at, attempt_budget,
                 endpoint, loader, client, grant, deadline_seconds, redactions, note=None,
                 preflight_overrides=None, settlement_overrides=None, fault_factory=None, seen=None,
                 provenance_method="synthetic-document-sha256-v1"):
        if not endpoint_allowed(endpoint):
            raise LiveRefusal("LIVE_ENDPOINT_REJECTED")
        if type(client) is not httpx.Client:
            raise LiveRefusal("LIVE_CLIENT_REJECTED")
        self.profile = profile
        self.approval = approval
        self.tariff_id = tariff_id
        self.not_before = not_before
        self.expires_at = expires_at
        self.attempt_budget = attempt_budget
        self.endpoint = endpoint
        self.loader = loader
        self.client = client
        self.grant = grant
        self.deadline_seconds = deadline_seconds
        self._base_redactions = tuple(redactions)
        self.note = note
        self.preflight_overrides = preflight_overrides
        self.settlement_overrides = settlement_overrides
        self.fault_factory = fault_factory
        self.seen = seen if seen is not None else {"n": 0}
        self.provenance_method = provenance_method

    @property
    def redactions(self):
        extra = getattr(self.loader, "loaded_secrets", ())
        return tuple(dict.fromkeys([*self._base_redactions, *extra]))

    def permits(self, request, profile, manifest_hash, at):
        if request.transport_intent != "live" or request.profile_id != profile.profile_id:
            return "LIVE_APPROVAL_SCOPE_MISMATCH"
        if self.approval.manifest_hash != manifest_hash or self.approval.profile_id != profile.profile_id:
            return "LIVE_APPROVAL_SCOPE_MISMATCH"
        pricing = next((item for item in profile.private_evidence if item.kind == "pricing"), None)
        if pricing is None or pricing.reference_id != self.tariff_id:
            return "LIVE_APPROVAL_SCOPE_MISMATCH"
        if request.requested_max_attempts > self.attempt_budget or request.overall_deadline_seconds != self.deadline_seconds:
            return "LIVE_APPROVAL_SCOPE_MISMATCH"
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None or not self.not_before <= at < self.expires_at:
            return "LIVE_APPROVAL_SCOPE_MISMATCH"
        if self.endpoint != PROVIDER_ENDPOINTS[profile.provider]:
            return "LIVE_ENDPOINT_REJECTED"
        return None

    def preflight(self, at):
        return production_document(
            self.profile, self.grant["key_ref"], at,
            overrides=self.preflight_overrides, provenance_method=self.provenance_method)

    def usage(self, store, study_id, slot_id):
        # This helper creates synthetic accounting evidence for offline tests.
        # A real HTTP client must retain its full reservation until independent
        # gateway usage evidence is reconciled; token estimates are not charges.
        if type(self.client._transport_for_url(httpx.URL(self.endpoint))) is not httpx.MockTransport:
            raise ValueError("VERIFIED_GATEWAY_USAGE_REQUIRED")
        from .live_readiness import classify_billing_evidence
        from .research_runtime import synthetic_aggregate

        at = self.not_before
        slot = next(item for item in store.list_slots(study_id) if item["slot_id"] == slot_id)
        document = production_document(
            self.profile, self.grant["key_ref"], at,
            overrides=self.settlement_overrides, covered=[slot["call_id"]],
            provenance_method=self.provenance_method)
        result = classify_billing_evidence(
            self.profile, document, at=at, key_ref=self.grant["key_ref"], require_coverage=True)
        if not result["ok"] or result["financial_proof"] is not False or result["invoiced_actual_usd"] is not None or result["invoice"] is not False:
            raise ValueError("production-shaped settlement held")
        return synthetic_aggregate(store, study_id, store.load_plan(study_id), only_pending=True)

    def transport(self):
        return ApprovedLiveTransport(self)

    def public_scope(self):
        return {
            "profile_id": self.profile.profile_id,
            "manifest_hash": self.approval.manifest_hash,
            "tariff_id": self.tariff_id,
            "expires_at": self.expires_at.isoformat(),
            "attempt_budget": self.attempt_budget,
        }


class ApprovedLiveTransport:
    """Durable-slot adapter. It posts through send_live_attempt, not the fixture transport."""

    def __init__(self, binding):
        self.binding = binding
        self.model_id = binding.profile.model_id
        self.settings = binding.profile.settings.provider_settings()
        self.posts = 0

    def post(self, stage, payload, schema):
        request = response_request(self.model_id, self.settings, stage, payload, schema)
        content = json.dumps(request, sort_keys=True).encode()
        result = send_live_attempt(
            approval=self.binding.approval,
            profile_id=self.binding.profile.profile_id,
            manifest_hash=self.binding.approval.manifest_hash,
            endpoint=self.binding.endpoint,
            loader=self.binding.loader,
            client=self.binding.client,
            timeout_policy=self.binding.profile.timeout_policy,
            deadline_seconds=self.binding.deadline_seconds,
            content=content,
        )
        if result.sent:
            self.posts += 1
            if self.binding.note is not None:
                self.binding.note()
        if result.stopped:
            if not result.sent:
                raise LiveRefusal(result.stopped)
            if result.stopped in {"PHASE_TIMEOUT", "OVERALL_DEADLINE"}:
                raise httpx.ReadTimeout(result.stopped)
            raise httpx.TransportError(result.stopped)
        try:
            body = json.loads(result.body_text or "")
        except ValueError:
            body = {"malformed_response_body": result.body_text or ""}
        if not isinstance(body, dict):
            body = {"malformed_response_envelope": body}
        body = redact_secrets(body, self.binding.redactions)
        text = ""
        output = body.get("output")
        if body.get("status") == "completed":
            text = "".join(
                part.get("text", "")
                for item in (output if isinstance(output, list) else [])
                if isinstance(item, dict) and item.get("type") == "message" and isinstance(item.get("content"), list)
                for part in item["content"]
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
            )
            text = redact_secrets(text, self.binding.redactions)
        return result.status_code, Reply(text, envelope=body)


def offline_handler(binding):
    """Injected HTTP boundary. It does not open a socket."""

    def handler(request):
        binding.seen["n"] += 1
        body = json.loads(request.content)
        name = body["text"]["format"]["name"]
        raw = json.dumps(_OUTPUTS[name])
        envelope = {
            "id": f"synthetic-offline-{binding.seen['n']}",
            "model": body["model"],
            "status": "completed",
            "service_tier": "auto",
            "output": [{"type": "message", "status": "completed", "content": [{"type": "output_text", "text": raw}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }
        return httpx.Response(200, json=envelope)

    return handler


_FORBIDDEN_SECRET_KEYS = frozenset({
    "api_key", "secret", "authorization", "credential_value", "bearer", "token", "password", "private_key",
})


def _reject_embedded_secrets(node, *, allow_endpoint=False, allow_credential_spec=False):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_SECRET_KEYS:
                raise LiveRefusal("LIVE_PROFILE_REJECTED")
            if key == "endpoint" and not allow_endpoint:
                raise LiveRefusal("LIVE_PROFILE_REJECTED")
            if key == "credential":
                if not allow_credential_spec or not isinstance(value, dict) or value.get("kind") != "scoped-private-key-file-v1":
                    raise LiveRefusal("LIVE_PROFILE_REJECTED")
            _reject_embedded_secrets(value, allow_endpoint=allow_endpoint, allow_credential_spec=allow_credential_spec)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _reject_embedded_secrets(item, allow_endpoint=allow_endpoint, allow_credential_spec=allow_credential_spec)


def freeze_operator_manifest(binding_path: str | Path, *, protocol: str = "acceptance-stage-a-v1", registry=None, context=None) -> dict:
    """Freeze a deterministic PROPOSED_NOT_APPROVED manifest from a verified operator tariff binding."""
    from datetime import date
    from decimal import Decimal
    from .data import ROOT, DatasetRegistry, canonical_hash
    from .live_readiness import classify_billing_evidence
    from .research_contracts import PrivateEvidenceRef, ResearchSettings, ServerResearchProfile, StudyRequest
    from .research_runtime import _source_context
    from .research_service import acceptance_start_refusal, plan_study

    if protocol not in {"acceptance-stage-a-v1", "acceptance-stage-b-v1"}:
        raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")
    attempts = 1 if protocol == "acceptance-stage-a-v1" else 3
    resolved = _check_private_file_path(binding_path, code="LIVE_PROFILE_REJECTED")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LiveRefusal("LIVE_PROFILE_REJECTED") from exc
    if not isinstance(raw, dict):
        raise LiveRefusal("LIVE_PROFILE_REJECTED")
    _reject_embedded_secrets(raw, allow_endpoint=True, allow_credential_spec=False)

    provider = str(raw.get("provider") or "kuaiapi")
    if provider not in PROVIDER_ENDPOINTS:
        raise LiveRefusal("LIVE_PROFILE_REJECTED")
    if "endpoint" in raw and raw.get("endpoint") != PROVIDER_ENDPOINTS[provider]:
        raise LiveRefusal("LIVE_ENDPOINT_REJECTED")

    try:
        valid_from = datetime.fromisoformat(str(raw["valid_from"]))
        expires_at = datetime.fromisoformat(str(raw["expires_at"]))
        budget_usd = Decimal(str(raw.get("requested_budget_usd", "1")))
    except (KeyError, ValueError, Exception) as exc:
        raise LiveRefusal("LIVE_PROFILE_REJECTED") from exc

    context = context or _source_context(valid_from)
    registry = registry or DatasetRegistry([ROOT / "data" / "sample"])
    dataset = registry.load("synthetic-spy-v1")

    profile_id = str(raw.get("profile_id") or ("operator-stage-a" if attempts == 1 else "operator-stage-b"))
    tariff_id = str(raw.get("tariff_id") or "")
    group_ref = str(raw.get("group_ref") or "")
    cohort_ref = str(raw.get("cohort_ref") or f"{group_ref}-cohort")
    binding_ref = str(raw.get("binding_ref") or "")
    binding_sha256 = str(raw.get("binding_sha256") or "")
    provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    document_sha256 = str(provenance.get("sha256") or "")
    tariff_sha256 = str(
        raw.get("tariff_sha256")
        or canonical_hash({
            "tariff_id": tariff_id,
            "provider": provider,
            "model_id": str(raw.get("model_id") or "gpt-6.1-sol"),
            "pricing_unit": "effective-usd-per-million",
            "input_usd_per_million": str(raw.get("input_usd_per_million")),
            "output_usd_per_million": str(raw.get("output_usd_per_million")),
            "cache_read_usd_per_million": str(raw.get("cache_read_usd_per_million")),
            "cache_write_usd_per_million": str(raw.get("cache_write_usd_per_million")),
        })
    )

    try:
        max_attempts = int(raw.get("max_attempts", attempts))
        if isinstance(raw.get("max_attempts"), bool) or max_attempts < attempts:
            raise ValueError("invalid max_attempts")
        profile = ServerResearchProfile(
            profile_id=profile_id,
            revision=1,
            provider=provider,
            model_id=str(raw.get("model_id") or "gpt-6.1-sol"),
            identity_scope="gateway-reported-label-only" if provider == "kuaiapi" else "direct-provider-label",
            dataset_id=dataset.metadata["id"],
            dataset_hash=dataset.hash,
            dataset_kind="synthetic",
            allowed_start=date(2023, 1, 1),
            allowed_end=date(2023, 1, 31),
            max_budget_usd=budget_usd,
            max_attempts=max_attempts,
            attempts_per_role=1,
            input_usd_per_million=Decimal(str(raw["input_usd_per_million"])),
            output_usd_per_million=Decimal(str(raw["output_usd_per_million"])),
            cache_read_usd_per_million=Decimal(str(raw["cache_read_usd_per_million"])),
            cache_write_usd_per_million=Decimal(str(raw["cache_write_usd_per_million"])),
            pricing_unit="effective-usd-per-million",
            settings=ResearchSettings(max_output_tokens=1024),
            valid_from=valid_from,
            expires_at=expires_at,
            expected_source_hash=context.source_hash,
            expected_uv_lock_hash=context.uv_lock_hash,
            expected_frontend_lock_hash=context.frontend_lock_hash,
            private_evidence=(
                PrivateEvidenceRef(kind="pricing", reference_id=tariff_id, sha256=tariff_sha256),
                PrivateEvidenceRef(kind="binding", reference_id=binding_ref, sha256=binding_sha256),
            ),
        )
    except Exception as exc:
        raise LiveRefusal("LIVE_PROFILE_REJECTED") from exc

    billing_doc = {
        k: v for k, v in raw.items()
        if k not in {"profile_id", "cohort_ref", "requested_budget_usd", "max_attempts"}
    }
    billing_doc.setdefault("endpoint", PROVIDER_ENDPOINTS[provider])
    checked = classify_billing_evidence(profile, billing_doc, at=valid_from, key_ref=group_ref)
    if (
        not checked["ok"]
        or checked["evidence_class"] != "operator-verified-binding"
        or checked["financial_proof"] is not False
        or checked["invoice"] is not False
    ):
        raise LiveRefusal("LIVE_TARIFF_BINDING_REJECTED")

    request = StudyRequest(
        profile_id=profile.profile_id,
        dataset_id=dataset.metadata["id"],
        expected_dataset_hash=dataset.hash,
        start_date=date(2023, 1, 27),
        end_date=date(2023, 1, 31),
        requested_budget_usd=budget_usd,
        requested_max_attempts=attempts,
        study_protocol=protocol,
        transport_intent="live",
        overall_deadline_seconds=45,
    )
    plan, view = plan_study(request, profile, registry, context=context)
    refusal = acceptance_start_refusal(view)
    if refusal:
        raise LiveRefusal(refusal)

    body = {
        "kind": "operator-live-freeze-v1",
        "approval_status": "PROPOSED_NOT_APPROVED",
        "funding_approved": False,
        "study_protocol": protocol,
        "manifest_hash": plan.manifest_hash,
        "profile": profile.model_dump(mode="json"),
        "profile_sha256": canonical_hash(profile.model_dump(mode="json")),
        "request": request.model_dump(mode="json"),
        "plan": json.loads(plan.canonical_payload),
        "view": view.model_dump(mode="json"),
        "binding_meta": {
            "tariff_id": tariff_id,
            "key_ref": group_ref,
            "cohort_ref": cohort_ref,
            "cap_usd": str(budget_usd),
            "attempt_budget": attempts,
            "binding_sha256": binding_sha256,
            "tariff_sha256": tariff_sha256,
            "document_sha256": document_sha256,
        },
    }
    body["provenance"] = {"method": "operator-live-freeze-sha256-v1", "sha256": canonical_hash(body)}
    return body


def build_operator_approval_bundle(items: list[tuple[dict, str | Path]]) -> dict:
    """Assemble a server-side operator bundle from frozen manifests and private key file paths."""
    from copy import deepcopy
    from .data import canonical_hash

    bindings = []
    for raw_freeze, key_file in items:
        freeze = deepcopy(raw_freeze)
        meta = freeze["binding_meta"]
        profile = freeze["profile"]
        bindings.append({
            "freeze": freeze,
            "approval": {
                "holder": "server",
                "funding_approved": True,
                "manifest_hash": freeze["manifest_hash"],
                "profile_id": profile["profile_id"],
                "tariff_id": meta["tariff_id"],
                "key_ref": meta["key_ref"],
                "cohort_ref": meta["cohort_ref"],
                "cap_usd": meta["cap_usd"],
                "attempt_budget": meta["attempt_budget"],
                "not_before": profile["valid_from"],
                "expires_at": profile["expires_at"],
            },
            "credential": {
                "kind": "scoped-private-key-file-v1",
                "provider": profile["provider"],
                "key_ref": meta["key_ref"],
                "key_file": str(key_file),
            },
        })
    body = {"kind": "operator-live-bundle-v1", "bindings": bindings}
    body["provenance"] = {"method": "operator-live-bundle-sha256-v1", "sha256": canonical_hash(body)}
    return body


def load_operator_bindings(bundle_path: str | Path, *, runtime, transport_factory=None) -> list[HeldLiveBinding]:
    """Load server-approved operator bindings into runtime without reading secret files."""
    from decimal import Decimal
    from .data import canonical_hash
    from .live_readiness import ServerLiveApproval
    from .research_contracts import ServerResearchProfile, StudyRequest
    from .research_runtime import _source_context
    from .research_service import acceptance_start_refusal, plan_study

    resolved_bundle = _check_private_file_path(bundle_path, code="LIVE_PROFILE_REJECTED")
    try:
        raw = json.loads(resolved_bundle.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LiveRefusal("LIVE_PROFILE_REJECTED") from exc
    if not isinstance(raw, dict) or raw.get("kind") != "operator-live-bundle-v1":
        raise LiveRefusal("LIVE_PROFILE_REJECTED")
    _reject_embedded_secrets(raw, allow_endpoint=False, allow_credential_spec=True)

    entries = raw.get("bindings")
    if not isinstance(entries, list) or not entries:
        raise LiveRefusal("LIVE_APPROVAL_REQUIRED")

    checked_times = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("freeze"), dict):
            raise LiveRefusal("LIVE_APPROVAL_REQUIRED")
        source_block = (entry["freeze"].get("plan") or {}).get("source") or {}
        if "checked_at" not in source_block:
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")
        checked_times.add(str(source_block["checked_at"]))
    if len(checked_times) != 1:
        raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")
    import time
    checked_at = datetime.fromisoformat(next(iter(checked_times)))
    runtime.context = _source_context(checked_at)
    runtime._started = checked_at
    runtime._last = checked_at
    runtime._monotonic = time.monotonic()

    installed: list[HeldLiveBinding] = []
    seen_bindings: set[tuple[str, str]] = set()
    seen_profiles: dict[str, str] = {}
    for entry in entries:
        freeze = entry.get("freeze")
        approval_raw = entry.get("approval")
        cred_raw = entry.get("credential")
        if not isinstance(freeze, dict) or freeze.get("kind") != "operator-live-freeze-v1":
            raise LiveRefusal("LIVE_APPROVAL_REQUIRED")
        freeze_clean = {k: v for k, v in freeze.items() if k != "provenance"}
        freeze_prov = freeze.get("provenance") if isinstance(freeze.get("provenance"), dict) else {}
        if (
            freeze_prov.get("method") != "operator-live-freeze-sha256-v1"
            or freeze_prov.get("sha256") != canonical_hash(freeze_clean)
        ):
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")
        if not isinstance(approval_raw, dict) or approval_raw.get("funding_approved") is not True:
            raise LiveRefusal("LIVE_APPROVAL_REQUIRED")
        try:
            approval = ServerLiveApproval(
                holder=approval_raw.get("holder"),
                funding_approved=approval_raw.get("funding_approved"),
                manifest_hash=approval_raw.get("manifest_hash"),
                profile_id=approval_raw.get("profile_id"),
            )
        except Exception as exc:
            raise LiveRefusal("LIVE_APPROVAL_REQUIRED") from exc

        try:
            profile = ServerResearchProfile.model_validate(freeze["profile"])
            request = StudyRequest.model_validate(freeze["request"])
        except Exception as exc:
            raise LiveRefusal("LIVE_PROFILE_REJECTED") from exc

        key_pair = (profile.profile_id, approval.manifest_hash)
        if key_pair in seen_bindings:
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")
        seen_bindings.add(key_pair)

        profile_hash = canonical_hash(profile.model_dump(mode="json"))
        if profile_hash != freeze.get("profile_sha256"):
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")
        if seen_profiles.setdefault(profile.profile_id, profile_hash) != profile_hash:
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")

        expected_attempts = 1 if request.study_protocol == "acceptance-stage-a-v1" else 3 if request.study_protocol == "acceptance-stage-b-v1" else None
        if (
            expected_attempts is None
            or request.transport_intent != "live"
            or request.requested_max_attempts != expected_attempts
            or profile.max_attempts < expected_attempts
            or profile.attempts_per_role != 1
            or approval_raw.get("attempt_budget") != expected_attempts
        ):
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")

        frozen, view = plan_study(request, profile, runtime.registry, context=runtime.context)
        refusal = acceptance_start_refusal(view)
        if refusal:
            raise LiveRefusal(refusal)
        if frozen.manifest_hash != freeze.get("manifest_hash") or frozen.manifest_hash != approval.manifest_hash:
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")

        pricing = next((item for item in profile.private_evidence if item.kind == "pricing"), None)
        tariff_id = str(approval_raw.get("tariff_id") or "")
        key_ref = str(approval_raw.get("key_ref") or "")
        cohort_ref = str(approval_raw.get("cohort_ref") or "")
        cap_usd = Decimal(str(approval_raw.get("cap_usd") or "0"))
        not_before = datetime.fromisoformat(str(approval_raw.get("not_before")))
        expires_at = datetime.fromisoformat(str(approval_raw.get("expires_at")))
        if (
            pricing is None
            or pricing.reference_id != tariff_id
            or not_before != profile.valid_from
            or expires_at != profile.expires_at
            or not (not_before <= runtime.clock() < expires_at)
            or cap_usd != request.requested_budget_usd
            or cap_usd < view.estimate.complete_reservation_usd
            or not key_ref
            or not cohort_ref
        ):
            raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")

        if not isinstance(cred_raw, dict) or cred_raw.get("kind") != "scoped-private-key-file-v1":
            raise LiveRefusal("LIVE_KEY_ORIGIN_REJECTED")
        if cred_raw.get("provider") != profile.provider or cred_raw.get("key_ref") != key_ref:
            raise LiveRefusal("LIVE_CREDENTIAL_SCOPE_MISMATCH")
        loader = ScopedKeyFileLoader(
            profile_id=profile.profile_id,
            provider=profile.provider,
            key_file=cred_raw.get("key_file", ""),
            key_ref=key_ref,
        )

        holder: dict = {}
        if transport_factory is None:
            client = httpx.Client(http2=False, follow_redirects=False)
        else:
            client = httpx.Client(transport=transport_factory(holder), http2=False, follow_redirects=False)

        endpoint = PROVIDER_ENDPOINTS[profile.provider]
        binding = HeldLiveBinding(
            profile=profile,
            approval=approval,
            tariff_id=tariff_id,
            not_before=not_before,
            expires_at=expires_at,
            attempt_budget=expected_attempts,
            endpoint=endpoint,
            loader=loader,
            client=client,
            grant={
                "authorization_id": f"grant-{profile.profile_id}-{frozen.manifest_hash[:12]}",
                "key_ref": key_ref,
                "cohort_ref": cohort_ref,
                "cap_usd": cap_usd,
                "max_attempts": expected_attempts,
                "evidence_sha256": canonical_hash({
                    "kind": "operator-live-grant-v1",
                    "manifest_hash": frozen.manifest_hash,
                    "profile_id": profile.profile_id,
                    "tariff_id": tariff_id,
                }),
            },
            deadline_seconds=request.overall_deadline_seconds,
            redactions=(
                endpoint,
                key_ref,
                cohort_ref,
                tariff_id,
                str(resolved_bundle),
                str(Path(bundle_path)),
                str(loader.key_file),
                str(Path(cred_raw.get("key_file", ""))),
            ),
            provenance_method="operator-verified-server-binding-v1",
            seen={"n": 0},
        )
        holder["binding"] = binding
        runtime.install_offline_binding(binding)
        installed.append(binding)
    return installed
