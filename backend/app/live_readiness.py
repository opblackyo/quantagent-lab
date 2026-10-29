"""Server-held live transport gate. The default app never constructs it.

An overall deadline caps bytes already yielded and each later socket wait.
CPython 3.13 socket.settimeout reads __index__ as whole seconds, so a blocked
read can run until the next whole second. DNS inside socket.create_connection
is outside that cap. Nothing here promises that an upstream stops billing.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .data import canonical_hash
from .http_policy import HTTPTimeoutPolicy

ALLOWED_ENDPOINTS = (
    "https://api.openai.com/v1/responses",
    "https://kuaiapi.net/v1/responses",
)
MAX_RESPONSE_BYTES = 1_048_576
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class LiveRefusal(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ServerLiveApproval(BaseModel):
    """Constructed only by server code. Request models reject this shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    holder: Literal["server"]
    funding_approved: Literal[True]
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")


class LiveAttemptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sent: bool
    retry_allowed: Literal[False] = False
    financial_proof: Literal[False] = False
    status_code: int | None = None
    stopped: str | None = None
    body_text: str | None = None


def endpoint_allowed(endpoint: str) -> bool:
    if endpoint not in ALLOWED_ENDPOINTS:
        return False
    parts = urlsplit(endpoint)
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and parts.query == ""
        and parts.fragment == ""
        and parts.path == "/v1/responses"
    )


def loopback_endpoint(endpoint: str) -> bool:
    """Owned test listener only. Production composition never calls this."""
    parts = urlsplit(endpoint)
    return (
        parts.scheme in {"http", "https"}
        and parts.hostname == "127.0.0.1"
        and parts.username is None
        and parts.password is None
        and parts.port is not None
        and parts.query == ""
        and parts.fragment == ""
        and parts.path == "/v1/responses"
    )


class _RemainingSeconds:
    """Socket wait cap. CPython 3.13 settimeout accepts a real number or __index__.

    A custom __float__ is rejected. __index__ is whole seconds, rounded up, and
    is read again on each later settimeout. The monotonic check before the next
    response chunk still uses the exact deadline. Zero means the deadline has
    passed and the socket wait is non-blocking.
    """

    def __init__(self, deadline_at, ceiling, clock):
        self.deadline_at = deadline_at
        self.ceiling = float(ceiling)
        self.clock = clock

    def _remaining(self):
        remaining = self.deadline_at - self.clock()
        if remaining <= 0:
            return 0.0
        if remaining < self.ceiling:
            return float(remaining)
        return self.ceiling

    def __index__(self):
        remaining = self._remaining()
        if remaining <= 0:
            return 0
        import math
        return int(math.ceil(remaining))

    def __float__(self):
        return self._remaining()


def _install_wait_caps(request, deadline_at, policy, clock):
    if deadline_at is None:
        return
    current = request.extensions.get("timeout") or {}
    request.extensions["timeout"] = {
        name: _RemainingSeconds(deadline_at, current.get(name, getattr(policy, name)), clock)
        for name in ("connect", "read", "write", "pool")
    }


def _timeout_stop(deadline_at, clock):
    if deadline_at is not None and clock() >= deadline_at:
        return "OVERALL_DEADLINE"
    return "PHASE_TIMEOUT"


def send_live_attempt(
    *,
    approval: ServerLiveApproval | None,
    profile_id: str,
    manifest_hash: str,
    endpoint: str,
    loader,
    client: httpx.Client,
    timeout_policy: HTTPTimeoutPolicy,
    deadline_seconds: float | None,
    content: bytes,
    clock=time.monotonic,
) -> LiveAttemptResult:
    """Load a credential only after a matching server approval. Never retries."""
    _require_approval(approval, profile_id, manifest_hash)
    if not endpoint_allowed(endpoint):
        raise LiveRefusal("LIVE_ENDPOINT_REJECTED")
    return _perform_attempt(
        profile_id=profile_id, endpoint=endpoint, loader=loader, client=client,
        timeout_policy=timeout_policy, deadline_seconds=deadline_seconds, content=content, clock=clock)


def send_loopback_attempt(
    *,
    approval: ServerLiveApproval | None,
    profile_id: str,
    manifest_hash: str,
    endpoint: str,
    loader,
    client: httpx.Client,
    timeout_policy: HTTPTimeoutPolicy,
    deadline_seconds: float | None,
    content: bytes,
    clock=time.monotonic,
) -> LiveAttemptResult:
    """Same reader as production, aimed only at an owned 127.0.0.1 listener."""
    _require_approval(approval, profile_id, manifest_hash)
    if not loopback_endpoint(endpoint):
        raise LiveRefusal("LIVE_ENDPOINT_REJECTED")
    return _perform_attempt(
        profile_id=profile_id, endpoint=endpoint, loader=loader, client=client,
        timeout_policy=timeout_policy, deadline_seconds=deadline_seconds, content=content, clock=clock)


def _require_approval(approval, profile_id, manifest_hash):
    if approval is None or approval.holder != "server" or approval.funding_approved is not True:
        raise LiveRefusal("LIVE_APPROVAL_REQUIRED")
    if approval.profile_id != profile_id or approval.manifest_hash != manifest_hash:
        raise LiveRefusal("LIVE_APPROVAL_SCOPE_MISMATCH")


def _perform_attempt(*, profile_id, endpoint, loader, client, timeout_policy, deadline_seconds, content, clock):
    if type(client) is not httpx.Client:
        raise LiveRefusal("LIVE_CLIENT_REJECTED")
    started = clock()
    deadline_at = None if deadline_seconds is None else started + deadline_seconds
    if deadline_at is not None and clock() >= deadline_at:
        return LiveAttemptResult(sent=False, stopped="OVERALL_DEADLINE")
    secret = loader.load(profile_id)
    if not isinstance(secret, str) or not secret or secret.strip() != secret:
        raise LiveRefusal("LIVE_CREDENTIAL_REJECTED")
    request = client.build_request(
        "POST",
        endpoint,
        content=content,
        headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
        timeout=timeout_policy.httpx_timeout(),
    )
    _install_wait_caps(request, deadline_at, timeout_policy, clock)
    try:
        response = client.send(request, stream=True, follow_redirects=False)
    except httpx.TimeoutException:
        return LiveAttemptResult(sent=True, stopped=_timeout_stop(deadline_at, clock))
    except httpx.RequestError:
        # The socket may already have left this process. Do not retry.
        return LiveAttemptResult(sent=True, stopped="TRANSPORT_UNCERTAIN")
    try:
        if response.status_code in REDIRECT_STATUSES:
            return LiveAttemptResult(sent=True, status_code=response.status_code, stopped="REDIRECT_REJECTED")
        chunks = bytearray()
        iterator = response.iter_bytes()
        try:
            while True:
                if deadline_at is not None and clock() >= deadline_at:
                    return LiveAttemptResult(sent=True, status_code=response.status_code, stopped="OVERALL_DEADLINE")
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    return LiveAttemptResult(sent=True, status_code=response.status_code, stopped="RESPONSE_BODY_LIMIT")
        except httpx.TimeoutException:
            return LiveAttemptResult(sent=True, status_code=response.status_code, stopped=_timeout_stop(deadline_at, clock))
        except (httpx.RequestError, httpx.StreamError):
            return LiveAttemptResult(sent=True, status_code=response.status_code, stopped="TRANSPORT_UNCERTAIN")
        text = bytes(chunks).decode("utf-8", errors="replace").replace(secret, "[REDACTED_API_KEY]")
        return LiveAttemptResult(sent=True, status_code=response.status_code, body_text=text)
    finally:
        # A close failure must not replace the recorded attempt with a retryable error.
        try:
            response.close()
        except Exception:
            pass


def _window_ok(document: dict, at) -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    start = datetime.fromisoformat(document["valid_from"])
    end = datetime.fromisoformat(document["expires_at"])
    if start.tzinfo is None or end.tzinfo is None or not start <= at < end:
        return False
    zoned = at.astimezone(ZoneInfo(document["timezone"]))
    return zoned.date().isoformat() == document.get("as_of_date") and document.get("day_count") == 1


def _money_equal(left, right) -> bool:
    try:
        if isinstance(left, bool) or isinstance(right, bool):
            return False
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:
        return False


_PROVENANCE = {
    "synthetic-document-sha256-v1": "synthetic-document",
    "operator-verified-server-binding-v1": "operator-verified-binding",
}


def classify_billing_evidence(profile, document, *, at, key_ref: str, require_coverage: bool = False) -> dict:
    """Compare a document with the frozen profile. No network and no invoice."""
    from .research_service import PROVIDER_ENDPOINTS, assess_preflight

    if not isinstance(document, dict):
        return _closed("BILLING_EVIDENCE_UNSUPPORTED")
    kind = document.get("kind")
    if kind == "synthetic-readonly-billing-v1":
        result = assess_preflight(profile, document, at=at, key_ref=key_ref)
        result["financial_proof"] = False
        result["invoiced_actual_usd"] = None
        return result
    if kind == "production-shaped-usage-v1":
        return _classify_production(profile, document, at=at, key_ref=key_ref, require_coverage=require_coverage)
    if kind != "operator-declared-usage-v1":
        return _closed("BILLING_EVIDENCE_UNSUPPORTED")
    if document.get("production_claim") is True or document.get("tariff_binding") != "not-a-live-tariff":
        return _closed("BILLING_TARIFF_UNKNOWN")
    codes = []
    provenance = document.get("provenance")
    body = {key: value for key, value in document.items() if key != "provenance"}
    if not isinstance(provenance, dict) or provenance.get("method") != "operator-sha256-v1":
        codes.append("PROVENANCE_MISMATCH")
    elif provenance.get("sha256") != canonical_hash(body):
        codes.append("PROVENANCE_MISMATCH")
    if document.get("provider") != profile.provider:
        codes.append("PROVIDER_DRIFT")
    if document.get("model_id") != profile.model_id:
        codes.append("MODEL_DRIFT")
    if document.get("endpoint") != PROVIDER_ENDPOINTS[profile.provider]:
        codes.append("ENDPOINT_DRIFT")
    if document.get("pricing_unit") != profile.pricing_unit:
        codes.append("PRICING_UNIT_DRIFT")
    if document.get("group_ref") != key_ref:
        codes.append("GROUP_DRIFT")
    covered = document.get("covered_call_ids")
    if not isinstance(covered, list) or not covered or len(set(covered)) != len(covered):
        codes.append("COVERAGE_MISMATCH")
    if document.get("delayed") is not False:
        codes.append("BILLING_DELAYED")
    if type(document.get("external_same_key_requests")) is not int or document["external_same_key_requests"] != 0:
        codes.append("EXTERNAL_SAME_KEY_USAGE")
    if document.get("cache_status") != "observed-consistent":
        codes.append("CACHE_UNPROVEN")
    for field in ("input_usd_per_million", "output_usd_per_million", "cache_read_usd_per_million", "cache_write_usd_per_million"):
        if not _money_equal(document.get(field), getattr(profile, field)):
            codes.append("RATE_DRIFT")
            break
    try:
        if not _window_ok(document, at):
            codes.append("DAY_WINDOW_ROLLOVER")
    except Exception:
        codes.append("BILLING_SEMANTICS_UNSUPPORTED")
    return {
        "ok": not codes,
        "pause": None if not codes else "PAUSED_BILLING",
        "codes": sorted(set(codes)),
        "detail": ",".join(sorted(set(codes)))[:200],
        "financial_proof": False,
        "invoiced_actual_usd": None,
    }


def _classify_production(profile, document, *, at, key_ref, require_coverage):
    """Match a production-shaped document. A match is not spend approval."""
    from .research_service import PROVIDER_ENDPOINTS

    codes = []
    if "production_claim" in document:
        codes.append("BILLING_CLAIM_FIELD_REJECTED")
    if document.get("financial_proof") is True or document.get("invoice") is True:
        codes.append("BILLING_NOT_AN_INVOICE")
    if "invoiced_actual_usd" in document and document.get("invoiced_actual_usd") is not None:
        codes.append("BILLING_NOT_AN_INVOICE")
    provenance = document.get("provenance")
    body = {key: value for key, value in document.items() if key != "provenance"}
    method = provenance.get("method") if isinstance(provenance, dict) else None
    evidence_class = _PROVENANCE.get(method)
    if evidence_class is None or not isinstance(provenance, dict) or provenance.get("sha256") != canonical_hash(body):
        codes.append("PROVENANCE_MISMATCH")
        evidence_class = None
    if document.get("provider") != profile.provider:
        codes.append("PROVIDER_DRIFT")
    if document.get("model_id") != profile.model_id:
        codes.append("MODEL_DRIFT")
    if document.get("endpoint") != PROVIDER_ENDPOINTS[profile.provider]:
        codes.append("ENDPOINT_DRIFT")
    if document.get("pricing_unit") != profile.pricing_unit:
        codes.append("PRICING_UNIT_DRIFT")
    runtime_unit = document.get("runtime_unit", document.get("pricing_unit"))
    if runtime_unit != profile.pricing_unit:
        codes.append("UNIT_UNSUPPORTED")
    if "multiplier" in document and not _money_equal(document.get("multiplier"), 1):
        codes.append("MULTIPLIER_DRIFT")
    pricing = next((item for item in profile.private_evidence if item.kind == "pricing"), None)
    if pricing is None or document.get("tariff_id") != pricing.reference_id:
        codes.append("BILLING_TARIFF_UNKNOWN")
    if document.get("group_ref") != key_ref:
        codes.append("GROUP_DRIFT")
    if profile.provider == "kuaiapi":
        binding = next((item for item in profile.private_evidence if item.kind == "binding"), None)
        if binding is None or document.get("binding_ref") != binding.reference_id or document.get("binding_sha256") != binding.sha256:
            codes.append("BINDING_DRIFT")
    for field in ("input_usd_per_million", "output_usd_per_million", "cache_read_usd_per_million", "cache_write_usd_per_million"):
        if not _money_equal(document.get(field), getattr(profile, field)):
            codes.append("RATE_DRIFT")
            break
    if document.get("cache_status") != "observed-consistent":
        codes.append("CACHE_UNPROVEN")
    if document.get("delayed") is not False:
        codes.append("BILLING_DELAYED")
    if type(document.get("external_same_key_requests")) is not int or document.get("external_same_key_requests") != 0:
        codes.append("EXTERNAL_SAME_KEY_USAGE")
    covered = document.get("covered_call_ids")
    if require_coverage:
        if not isinstance(covered, list) or not covered or len(set(covered)) != len(covered):
            codes.append("COVERAGE_MISMATCH")
    elif "covered_call_ids" in document and (not isinstance(covered, list) or len(set(covered)) != len(covered)):
        codes.append("COVERAGE_MISMATCH")
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        start = datetime.fromisoformat(document["valid_from"])
        end = datetime.fromisoformat(document["expires_at"])
        if start.tzinfo is None or end.tzinfo is None or start.utcoffset() is None or end.utcoffset() is None or not start <= at < end:
            codes.append("BILLING_EXPIRED")
        elif at.astimezone(ZoneInfo(document["timezone"])).date().isoformat() != document.get("as_of_date") or document.get("day_count") != 1:
            codes.append("DAY_WINDOW_ROLLOVER")
    except Exception:
        codes.append("BILLING_SEMANTICS_UNSUPPORTED")
    from .research_service import _DRIFT
    pause = "PAUSED_DRIFT" if any(code in _DRIFT for code in codes) else "PAUSED_BILLING" if codes else None
    return {
        "ok": not codes,
        "pause": pause,
        "codes": sorted(set(codes)),
        "detail": ",".join(sorted(set(codes)))[:200],
        "financial_proof": False,
        "invoiced_actual_usd": None,
        "evidence_class": evidence_class,
        "invoice": False,
    }


def _closed(code: str) -> dict:
    return {
        "ok": False,
        "pause": "PAUSED_BILLING",
        "codes": [code],
        "detail": code,
        "financial_proof": False,
        "invoiced_actual_usd": None,
    }
