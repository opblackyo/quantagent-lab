"""Bounded Agent contracts. This module has no access to orders or a ledger."""
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import TypedDict
from uuid import uuid4

import httpx
from langgraph.graph import END, START, StateGraph

from .data import canonical_hash
from .models import AnalysisOutput, CriticOutput, DecisionOutput, ExperimentConfig, FeatureSnapshot, PROMPT_VERSION
from .http_policy import HTTPTimeoutPolicy,transport_policy,timeout_category

from .agent_contracts import SCHEMAS, INSTRUCTIONS, strict_schema, AgentFailure

@dataclass
class Reply:
    raw: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0
    envelope: dict = field(default_factory=dict)
    cost_known: bool = True


class MockProvider:
    model_id = "mock-rules-v1"
    settings = {"deterministic": True}

    def reserve_cost(self, stage, payload, schema):
        return 0.0

    def reply(self, stage, payload, schema):
        features = payload["features"]
        up = features["ma20"] > features["ma60"]
        volatile = features["realized_vol20"] > .25
        flags = ["VOLATILITY_HIGH"] if volatile else ["NO_OBJECTION"]
        if stage == "analysis":
            output = {"regime": "bullish" if up else "bearish", "thesis": "Mock: compare the trailing MA20 and MA60.", "evidence": ["ma20", "ma60"]}
        elif stage == "critic":
            output = {"risk_level": "high" if volatile else "low", "risk_flags": flags, "counterarguments": ["Mock: elevated trailing volatility reduces exposure."] if volatile else []}
        else:
            weight = 1.0 if up else 0.0
            if payload.get("critic", {}).get("risk_level") == "high" and up:
                weight = .5
            output = {"target_weight": weight, "confidence": .6, "rationale": "Mock rule fixture; this is not an LLM prediction.", "evidence": ["ma20", "ma60", "realized_vol20"], "acknowledged_risks": flags if "critic" in payload else []}
        return Reply(json.dumps(output), envelope={"mock": True, "fixture_version": self.model_id})


class OpenAIProvider:
    provider_id = "openai"
    endpoint = "https://api.openai.com/v1/responses"
    key_env = "OPENAI_API_KEY"
    settings = {"max_output_tokens": 1024, "store": False}

    def __init__(self, client: httpx.Client | None = None, *, model_id=None, input_rate=None, output_rate=None, settings=None, timeout_policy=None):
        if isinstance(timeout_policy,HTTPTimeoutPolicy):timeout_policy=timeout_policy.model_dump()
        self.timeout_policy=HTTPTimeoutPolicy.model_validate(timeout_policy if timeout_policy is not None else {})
        self.key = os.environ.get(self.key_env, "")
        self.model_id = model_id or os.environ.get("LLM_MODEL", "")
        try:
            self.input_rate = float(input_rate if input_rate is not None else os.environ.get("LLM_INPUT_USD_PER_MILLION", "nan"))
            self.output_rate = float(output_rate if output_rate is not None else os.environ.get("LLM_OUTPUT_USD_PER_MILLION", "nan"))
        except ValueError:
            self.input_rate = self.output_rate = float("nan")
        if not self.key or not self.model_id or not all(math.isfinite(v) and v > 0 for v in (self.input_rate, self.output_rate)):
            raise ValueError("API mode requires a model, API key, and explicit positive input/output price assumptions")
        self.client = client
        self.settings = {**type(self).settings, **(settings or {})}
        if set(self.settings) - {"max_output_tokens", "store", "reasoning", "temperature", "service_tier"} or self.settings["store"] is not False:
            raise ValueError("unsupported provider settings")

    def payload(self, stage, payload, schema):
        return response_request(self.model_id, self.settings, stage, payload, schema)

    def reserve_cost(self, stage, payload, schema):
        # Deliberately conservative byte bound plus schema/protocol allowance.
        bound = len(json.dumps(self.payload(stage, payload, schema)).encode()) + 4096
        return (bound * self.input_rate + self.settings["max_output_tokens"] * self.output_rate) / 1e6

    def reply(self, stage, payload, schema):
        endpoints = {'openai':('https://api.openai.com/v1/responses','OPENAI_API_KEY'),
                     'kuaiapi':('https://kuaiapi.net/v1/responses','KUAIAPI_API_KEY')}
        if endpoints.get(self.provider_id) != (self.endpoint,self.key_env):
            raise AgentFailure('PROVIDER_ENDPOINT_MISMATCH')
        request = self.payload(stage, payload, schema)
        kwargs = {"json": request, "headers": {"Authorization": "Bearer " + self.key}, "timeout": self.timeout_policy.httpx_timeout(), "follow_redirects": False}
        if self.client:
            response = self.client.post(self.endpoint, **kwargs)
        else:
            with httpx.Client() as client:
                response = client.post(self.endpoint, **kwargs)
        response.raise_for_status()
        # A provider/proxy must never be able to echo the backend key into
        # persisted raw response evidence, including successful envelopes.
        try:
            body = response.json()
        except ValueError:
            body = {'malformed_response_body':response.text}
        if not isinstance(body,dict):
            body = {'malformed_response_envelope':body}
        serialized = json.dumps(body)
        serialized = serialized.replace(self.key, "[REDACTED_API_KEY]")
        for name in ("OPENAI_API_KEY", "KUAIAPI_API_KEY"):
            if os.environ.get(name):
                serialized = serialized.replace(os.environ[name], "[REDACTED_API_KEY]")
        envelope = json.loads(serialized)
        input_tokens, output_tokens, known = self.usage(envelope.get("usage"))
        output = envelope.get('output')
        raw = "".join(part['text'] for item in (output if isinstance(output,list) else [])
                      if isinstance(item,dict) and item.get('type')=='message' and isinstance(item.get('content'),list)
                      for part in item['content'] if isinstance(part,dict) and part.get('type')=='output_text' and isinstance(part.get('text'),str))
        if envelope.get("status") != "completed":
            raw = ""  # Still return usage/envelope so paid incomplete calls are recorded.
        return Reply(raw, input_tokens, output_tokens, (input_tokens*self.input_rate + output_tokens*self.output_rate)/1e6, envelope, known)

    @staticmethod
    def usage(usage):
        if not isinstance(usage, dict):
            return 0, 0, False
        values = [usage.get(k) for k in ("input_tokens", "output_tokens")]
        valid = all(type(v) is int and v >= 0 for v in values)
        if valid and "total_tokens" in usage:
            valid = type(usage['total_tokens']) is int and usage['total_tokens'] == sum(values)
        return (*values, True) if valid else (0, 0, False)


def response_request(model_id, settings, stage, payload, schema):
    """Same Responses body the legacy adapter sends. No client and no key."""
    return {"model": model_id, "instructions": INSTRUCTIONS[stage], "input": json.dumps(payload, sort_keys=True),
            "text": {"format": {"type": "json_schema", "name": stage + "_output", "strict": True, "schema": strict_schema(schema)}},
            **settings}


INPUT_TOKEN_BOUND = 32768
PROTOCOL_ALLOWANCE = 4096
CLASSIFIER_VERSION = "stage-envelope-v2"
DOCUMENTED_TIERS = {"auto", "default", "flex", "scale", "priority", "fast", "ultrafast"}
OBSERVATION_TIERS = {"auto", "default"}


def request_exceeds_input_bound(request):
    return len(json.dumps(request).encode()) + PROTOCOL_ALLOWANCE > INPUT_TOKEN_BOUND


def redact_secrets(value, secrets):
    """Replace injected fixture secrets. Does not read the process environment."""
    text = value if isinstance(value, str) else json.dumps(value)
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED_API_KEY]")
    return text if isinstance(value, str) else json.loads(text)


def _usage_pair(provider_id, usage):
    if provider_id == "kuaiapi":
        from .gateway import KuaiAPIProvider
        return KuaiAPIProvider.usage(usage)
    return OpenAIProvider.usage(usage)


def classify_stage_envelope(stage, envelope, raw_response, expected):
    """Versioned stage-aware gate. It runs before risk and fills.

    Missing optional observability is a warning. Explicit contradictions,
    refusals, incomplete or malformed output, schema violations, tool or
    identity drift, and bad usage are rejections. JSON is never repaired
    and a partial target is never returned as eligible. auto/default tier
    labels do not prove Fast, price, or cause.
    """
    failures, stops, warnings = [], [], []
    if stage not in SCHEMAS:
        stops.append("UNKNOWN_STAGE")
    if not isinstance(envelope, dict) or "malformed_response_body" in envelope or "malformed_response_envelope" in envelope:
        stops.append("TRANSPORT_ENVELOPE_INVALID")
        envelope = envelope if isinstance(envelope, dict) else {}
    http_status = expected.get("http_status")
    if http_status is None:
        warnings.append("HTTP_STATUS_NOT_RECORDED")
    elif type(http_status) is not int or not 200 <= http_status < 300:
        stops.append("TRANSPORT_HTTP_ERROR")
    if expected.get("transport_error"):
        stops.append("TRANSPORT_OR_ENDPOINT_FAILURE")
    if 'model' not in envelope:
        stops.append('MODEL_LABEL_MISSING')
    elif envelope.get("model") != expected.get("model_id"):
        stops.append("MODEL_LABEL_DRIFT")
    if "provider" not in envelope:
        warnings.append("PROVIDER_NOT_REPORTED")
    elif envelope.get("provider") != expected.get("provider_id"):
        stops.append("PROVIDER_DRIFT")
    if "endpoint" not in envelope:
        warnings.append("ENDPOINT_NOT_REPORTED")
    elif envelope.get("endpoint") != expected.get("endpoint"):
        stops.append("ENDPOINT_DRIFT")
    if envelope.get("error") is not None:
        stops.append("PROVIDER_ERROR")
    output = envelope.get("output", [])
    if not isinstance(output, list):
        output = []
        stops.append("TRANSPORT_ENVELOPE_INVALID")
    if any(not isinstance(item, dict) for item in output):
        stops.append('TRANSPORT_ENVELOPE_INVALID')
    if any(isinstance(item, dict) and item.get("type") not in ("reasoning", "message") for item in output):
        stops.append("UNREQUESTED_TOOL_OR_OUTPUT_TYPE")
    messages = [item for item in output if isinstance(item, dict) and item.get("type") == "message"]
    parts = []
    for message in messages:
        content = message.get("content", [])
        if not isinstance(content, list):
            stops.append("TRANSPORT_ENVELOPE_INVALID")
        else:
            if any(not isinstance(part, dict) for part in content):
                stops.append('TRANSPORT_ENVELOPE_INVALID')
            if any(isinstance(part, dict) and part.get('type') not in ('output_text', 'refusal') for part in content):
                stops.append('UNREQUESTED_TOOL_OR_OUTPUT_TYPE')
            parts.extend(part for part in content if isinstance(part, dict))
    envelope_text = "".join(part.get("text", "") for part in parts if part.get("type") == "output_text" and isinstance(part.get("text"), str))
    expected_text = envelope_text if envelope.get("status") == "completed" else ""
    if raw_response != expected_text:
        stops.append("ADAPTER_TEXT_INTEGRITY_MISMATCH")
    if envelope.get("status") != "completed":
        failures.append("RESPONSE_NOT_COMPLETED")
    if envelope.get("incomplete_details") is not None:
        failures.append("INCOMPLETE_DETAILS_PRESENT")
    if not messages:
        failures.append("MESSAGE_MISSING")
    for message in messages:
        status = message.get("status")
        if status is None:
            warnings.append("MESSAGE_STATUS_NOT_REPORTED")
        elif envelope.get("status") == "completed" and status != "completed":
            failures.append("MESSAGE_STATUS_CONTRADICTS_RESPONSE")
    if any(part.get("type") == "refusal" for part in parts):
        failures.append("REFUSAL_NOT_DECISION")
    text_config = envelope.get("text")
    fmt = text_config.get("format") if isinstance(text_config, dict) else None
    if not isinstance(fmt, dict):
        warnings.append("FORMAT_ECHO_NOT_REPORTED")
    elif fmt.get("type") != "json_schema" or fmt.get("strict") is False:
        failures.append("FORMAT_ECHO_CONTRADICTS_REQUEST")
    elif (fmt.get('name', stage+'_output') != stage+'_output' or
          ('schema' in fmt and stage in SCHEMAS and fmt['schema'] != strict_schema(SCHEMAS[stage].model_json_schema()))):
        failures.append('FORMAT_ECHO_CONTRADICTS_REQUEST')
    requested_tier = expected.get("service_tier")
    tier = envelope.get("service_tier")
    if tier is None:
        warnings.append("TIER_MAPPING_NOT_ESTABLISHED")
        tier_scope = "unsupported-or-absent-observability"
    elif not isinstance(tier, str) or tier not in DOCUMENTED_TIERS:
        warnings.append("TIER_MAPPING_NOT_ESTABLISHED")
        tier_scope = "unsupported-or-absent-observability"
    else:
        tier_scope = "documented-observation"
        if tier not in OBSERVATION_TIERS:
            warnings.append("TIER_MAPPING_NOT_ESTABLISHED")
    if requested_tier is not None and tier is not None and tier != requested_tier:
        failures.append("SERVICE_TIER_CONTRADICTS_REQUEST")
    usage = envelope.get("usage")
    cache_keys = [key for key in ("cache_read_tokens", "cache_write_tokens", "cached_tokens", "cache_creation_tokens") if isinstance(usage, dict) and key in usage]
    if not cache_keys:
        warnings.append("CACHE_TOKENS_NOT_REPORTED")
    input_tokens, output_tokens, known = _usage_pair(expected.get("provider_id"), usage)
    # Responses uses nested detail objects; aliases must agree with those totals.
    if isinstance(usage, dict):
        for name, bound in (("input_tokens_details", input_tokens), ("output_tokens_details", output_tokens)):
            detail = usage.get(name)
            if name not in usage:
                continue
            if not isinstance(detail, dict) or any(type(v) is not int or v < 0 or v > bound for v in detail.values()):
                known = False
                stops.append("INCONSISTENT_USAGE")
            elif name == "input_tokens_details":
                for alias in ("cached_tokens", "cache_read_tokens", "cache_write_tokens"):
                    if alias in detail and alias in usage and detail[alias] != usage[alias]:
                        known = False
                        stops.append("INCONSISTENT_USAGE")
    if isinstance(usage, dict) and cache_keys:
        for key in cache_keys:
            value = usage.get(key)
            if type(value) is not int or value < 0 or (known and value > input_tokens):
                known = False
                stops.append("INCONSISTENT_USAGE")
    read_values, write_values = [], []
    if isinstance(usage, dict) and known:
        detail = usage.get('input_tokens_details') or {}
        read_values = [source[key] for source, key in ((usage, 'cache_read_tokens'), (usage, 'cached_tokens'), (detail, 'cached_tokens'), (detail, 'cache_read_tokens')) if key in source]
        write_values = [source[key] for source, key in ((usage, 'cache_write_tokens'), (usage, 'cache_creation_tokens'), (detail, 'cache_write_tokens'), (detail, 'cache_creation_tokens')) if key in source]
        if len(set(read_values)) > 1 or len(set(write_values)) > 1 or (read_values and write_values and read_values[0]+write_values[0] > input_tokens):
            known = False
            stops.append('INCONSISTENT_USAGE')
    if not known:
        stops.append("UNKNOWN_OR_INCONSISTENT_USAGE")
    max_input = expected.get("max_input_tokens", INPUT_TOKEN_BOUND)
    max_output = expected.get("max_output_tokens")
    if known and (type(max_output) is not int or input_tokens > max_input or output_tokens > max_output):
        known = False
        stops.append("USAGE_BOUND_EXCEEDED")
    parsed = None
    schema_errors = []
    if not stops and "RESPONSE_NOT_COMPLETED" not in failures and "REFUSAL_NOT_DECISION" not in failures and "INCOMPLETE_DETAILS_PRESENT" not in failures and "FORMAT_ECHO_CONTRADICTS_REQUEST" not in failures and "MESSAGE_STATUS_CONTRADICTS_RESPONSE" not in failures:
        try:
            parsed = validate_agent_output(stage, raw_response).model_dump(mode="json")
        except Exception as exc:
            failures.append("SCHEMA_OR_JSON_INVALID")
            schema_errors = [{"type": error["type"], "location": list(error["loc"])} for error in exc.errors()] if hasattr(exc, "errors") else [{"type": type(exc).__name__}]
    contradictory = any(code in failures for code in ("MESSAGE_STATUS_CONTRADICTS_RESPONSE", "FORMAT_ECHO_CONTRADICTS_REQUEST", "SERVICE_TIER_CONTRADICTS_REQUEST"))
    transport = any(code in stops for code in ("TRANSPORT_ENVELOPE_INVALID", "TRANSPORT_HTTP_ERROR", "TRANSPORT_OR_ENDPOINT_FAILURE", "PROVIDER_ERROR", "ADAPTER_TEXT_INTEGRITY_MISMATCH"))
    usage_bad = any(code in stops for code in ("UNKNOWN_OR_INCONSISTENT_USAGE", "USAGE_BOUND_EXCEEDED", "INCONSISTENT_USAGE"))
    drift = any(code in stops for code in ("MODEL_LABEL_DRIFT", "PROVIDER_DRIFT", "ENDPOINT_DRIFT", "UNREQUESTED_TOOL_OR_OUTPUT_TYPE"))
    release = known and not usage_bad and not transport and not contradictory and not drift
    eligible = bool(parsed) and release and not failures and not stops
    return {"classifier_version": CLASSIFIER_VERSION, "eligible_for_fill": eligible, "validated_output": parsed if eligible else None,
            "schema_parsed": parsed, "release_reservation": release, "pause_study": bool(stops) or usage_bad or contradictory or drift,
            "failure_codes": sorted(set(failures)), "hard_stop_codes": sorted(set(stops)), "observation_warnings": sorted(set(warnings)),
            "schema_errors": schema_errors, "usage_known": known and not usage_bad, "input_tokens": input_tokens if known else 0,
            "output_tokens": output_tokens if known else 0, "service_tier_observation": {"value": tier, "scope": tier_scope, "proves_fast_price_or_model_change": False},
            "cache_read_tokens": read_values[0] if known and read_values else None,
            "cache_write_tokens": write_values[0] if known and write_values else None,
            "no_partial_target": not eligible, "no_json_repair": True}


class InjectedFixtureTransport:
    """Posts only through an injected client. It never reads keys or opens a network transport."""

    def __init__(self, client, *, provider_id, endpoint, model_id, settings, timeout_policy, authorization, secrets=()):
        transport = getattr(client, "_transport", None)
        if type(client) is not httpx.Client or type(transport) is not httpx.MockTransport or any(
                mounted is not None for mounted in getattr(client, "_mounts", {}).values()):
            raise ValueError("injected fixture transport required")
        if client.event_hooks.get("request") or client.event_hooks.get("response"):
            raise ValueError("injected fixture transport cannot have hooks")
        if not isinstance(authorization, str) or not authorization:
            raise ValueError("explicit synthetic authorization required")
        self.client = client
        self.provider_id = provider_id
        self.endpoint = endpoint
        self.model_id = model_id
        self.settings = settings
        self.timeout_policy = timeout_policy
        self.authorization = authorization
        self.secrets = (authorization, *[secret for secret in secrets if isinstance(secret, str) and secret])
        self.posts = 0

    def post(self, stage, payload, schema):
        request = response_request(self.model_id, self.settings, stage, payload, schema)
        if request_exceeds_input_bound(request):
            raise AgentFailure("PILOT_INPUT_BOUND_EXCEEDED")
        self.posts += 1
        response = self.client.post(self.endpoint, json=request, headers={"Authorization": "Bearer " + self.authorization},
                                    timeout=self.timeout_policy.httpx_timeout(), follow_redirects=False)
        try:
            body = response.json()
        except ValueError:
            body = {"malformed_response_body": response.text}
        if not isinstance(body, dict):
            body = {"malformed_response_envelope": body}
        body = redact_secrets(body, self.secrets)
        text = ""
        output = body.get("output")
        if body.get("status") == "completed":
            text = "".join(part.get("text", "") for item in (output if isinstance(output, list) else []) if isinstance(item, dict) and item.get("type") == "message" and isinstance(item.get("content"), list)
                           for part in item["content"] if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str))
            text = redact_secrets(text, self.secrets)
        return response.status_code, Reply(text, envelope=body)


def validate_agent_output(stage, raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Agent JSON key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("nonfinite Agent JSON value")
    return SCHEMAS[stage].model_validate(json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite))


def api_capabilities() -> dict:
    try:
        provider = OpenAIProvider()
        return {"available": True, "provider": "openai", "model_id": provider.model_id}
    except ValueError:
        return {"available": False, "provider": None, "model_id": None}


class WorkflowState(TypedDict, total=False):
    features: dict
    analysis: dict
    critic: dict
    decision: dict


class AgentWorkflow:
    def __init__(self, config: ExperimentConfig, provider=None, budget=None):
        self.config = config
        self.provider = provider or (OpenAIProvider() if config.llm_mode == "api" else MockProvider())
        self.attempts = 0
        self.spent = 0.0
        self.actual_cost = 0.0
        self.all_calls: list[dict] = []
        self.budget = budget
        self.provider_contract_hash = self._provider_contract_hash()
        if config.llm_mode == "api" and config.model_id and config.model_id != self.provider.model_id:
            raise ValueError("requested model differs from the backend-configured model")
        if config.llm_mode == "api" and config.model_provider and config.model_provider != getattr(self.provider, 'provider_id', 'openai'):
            raise ValueError("requested provider differs from configured provider")

    def _provider_contract_hash(self):
        return canonical_hash({'model':self.provider.model_id,'settings':self.provider.settings,
            'endpoint':getattr(self.provider,'endpoint',None),
            'attribution':getattr(self.provider,'attribution',None),
            'pricing':getattr(self.provider,'pricing_attribution',None),
            'transport_timeout_policy':transport_policy(self.provider),
            'rates':[getattr(self.provider,'input_rate',None),getattr(self.provider,'output_rate',None)]})

    def _call(self, stage: str, payload: dict, calls: list, snapshot: FeatureSnapshot, on_call):
        schema = SCHEMAS[stage].model_json_schema()
        prompt_hash = canonical_hash({"version": PROMPT_VERSION, "instructions": INSTRUCTIONS[stage], "schema": schema})
        for attempt in range(self.config.max_attempts_per_role):
            if self._provider_contract_hash() != self.provider_contract_hash:
                raise AgentFailure('PROVIDER_CONFIGURATION_CHANGED')
            reserve = self.provider.reserve_cost(stage, payload, schema)
            if self.attempts >= self.config.max_llm_calls or self.spent + reserve > self.config.max_llm_budget_usd:
                raise AgentFailure("BUDGET_EXHAUSTED")
            call = {"call_id": str(uuid4()), "stage": stage, "attempt": attempt + 1, "mode": self.config.llm_mode,
                    "model_id": self.provider.model_id, "model_settings": self.provider.settings,
                    "input": payload, "input_hash": canonical_hash(payload), "prompt_hash": prompt_hash,
                    "prompt_version": PROMPT_VERSION, "schema_hash": canonical_hash(schema), "instructions": INSTRUCTIONS[stage],
                    "cache_key": canonical_hash({"data": snapshot.dataset_hash, "feature_version": snapshot.feature_version,
                                                "model": self.provider.model_id, "settings": self.provider.settings,
                                                "provider": getattr(self.provider, 'attribution', getattr(self.provider, 'provider_id', 'mock')),
                                                "prompt": prompt_hash, "input": payload}),
                    "raw_response": None, "validated_output": None, "status": "RUNNING", "cost_usd": 0,
                    "input_tokens": 0, "output_tokens": 0, "reserved_usd": reserve}
            call['provider_attribution'] = getattr(self.provider, 'attribution', {
                'provider': getattr(self.provider, 'provider_id', 'mock'),
                'endpoint': getattr(self.provider, 'endpoint', None), 'group': None})
            call['provider_attribution'] = json.loads(json.dumps(call['provider_attribution']))
            call['model_settings'] = json.loads(json.dumps(call['model_settings']))
            call['transport_timeout_policy']=transport_policy(self.provider)
            call["cost_status"] = "unknown_reserved" if self.config.llm_mode == "api" else "mock_zero"
            if isinstance(self.provider, OpenAIProvider):
                call["price_assumptions_usd_per_million"] = {"input": self.provider.input_rate, "output": self.provider.output_rate}
                call['pricing_attribution'] = getattr(self.provider, 'pricing_attribution', {'unit':'USD per 1M tokens', 'invoiced_actual_usd':None})
                call['pricing_attribution'] = json.loads(json.dumps(call['pricing_attribution']))
            if self.budget:
                self.budget.reserve(call["call_id"], reserve)
            self.attempts += 1
            self.spent += reserve
            calls.append(call)
            self.all_calls.append(call)
            on_call(call)  # Persist reservation/input before any possibly billable request.
            started = time.monotonic()
            retryable = False
            try:
                reply = self.provider.reply(stage, payload, schema)
                call.update(raw_response=reply.raw, raw_provider_response=reply.envelope,
                            input_tokens=reply.input_tokens, output_tokens=reply.output_tokens, cost_usd=reply.cost_usd,
                            cost_status=("known" if reply.cost_known else "unknown_reserved") if self.config.llm_mode == "api" else "mock_zero",
                            actual_model_id=reply.envelope.get("model", self.provider.model_id))
                if reply.cost_known:
                    self.spent += reply.cost_usd - reserve
                self.actual_cost += reply.cost_usd
                if self.spent > self.config.max_llm_budget_usd:
                    call["status"] = "FAILED"
                    call["error_code"] = "BUDGET_EXCEEDED"
                    raise AgentFailure("BUDGET_EXCEEDED")
                if self.budget and self.budget.over_limit(call["call_id"], reply.cost_usd if reply.cost_known else None):
                    call.update(status="FAILED", error_code="PILOT_BUDGET_EXCEEDED")
                    raise AgentFailure("PILOT_BUDGET_EXCEEDED")
                if getattr(self.provider, "require_exact_model", False) and reply.envelope.get("model") != self.provider.model_id:
                    call.update(status="FAILED", error_code="MODEL_VERSION_MISMATCH")
                    raise AgentFailure("MODEL_VERSION_MISMATCH")
                parsed = validate_agent_output(stage, reply.raw)
                call.update(validated_output=parsed.model_dump(mode="json"), status="VALIDATED")
                return call["validated_output"]
            except AgentFailure as exc:
                if call['status']=='RUNNING':
                    call.update(status='FAILED',error_code=str(exc))
                raise
            except httpx.TimeoutException as exc:
                call["status"] = "FAILED"
                call["error_code"] = "PROVIDER_TIMEOUT"
                phase,exception_class=timeout_category(exc)
                call['timeout_diagnostic']={'phase':phase,'exception_class':exception_class,
                    'policy':call['transport_timeout_policy'],'semantics':'HTTPX phase/chunk inactivity limits; not a total wall-clock deadline'}
                # A timeout may already have been billed. Never automatically resend.
                retryable = False
            except httpx.HTTPStatusError as exc:
                call["status"] = "FAILED"
                call["error_code"] = "PROVIDER_HTTP_ERROR"
                safe=exc.response.text
                for secret in (getattr(self.provider,'key',None),os.environ.get('OPENAI_API_KEY'),os.environ.get('KUAIAPI_API_KEY')):
                    if secret:
                        safe=safe.replace(secret,'[REDACTED_API_KEY]')
                call['http_status']=exc.response.status_code
                call['raw_provider_response']={'http_status':exc.response.status_code,'error_body':safe[:4096]}
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            except httpx.HTTPError:
                call["status"] = "FAILED"
                call["error_code"] = "PROVIDER_NETWORK_ERROR"
            except (ValueError, TypeError):
                call["status"] = "FAILED"
                call["error_code"] = "INVALID_AGENT_OUTPUT"
            except Exception:
                call["status"] = "FAILED"
                call["error_code"] = "PROVIDER_ERROR"
            finally:
                call["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
                if 'timeout_diagnostic' in call:call['timeout_diagnostic']['attempt_elapsed_ms']=call['latency_ms']
                if self.budget:
                    self.budget.settle(call["call_id"], call["cost_usd"] if call["cost_status"] == "known" else None)
                on_call(call)
            if not retryable or attempt == self.config.max_attempts_per_role - 1:
                raise AgentFailure(call["error_code"])

    def evaluate(self, snapshot: FeatureSnapshot, on_call=lambda call: None) -> dict:
        calls = []

        def node(stage):
            def execute(state):
                payload = {"features": state["features"]}
                if stage in ("critic", "decision") and "analysis" in state:
                    payload["analysis"] = state["analysis"]
                if stage == "decision" and "critic" in state:
                    payload["critic"] = state["critic"]
                return {stage: self._call(stage, payload, calls, snapshot, on_call)}
            return execute

        graph = StateGraph(WorkflowState)
        stages = ["analysis", "critic", "decision"] if self.config.strategy_id == "multi_agent" else ["decision"]
        previous = START
        for stage in stages:
            graph.add_node(stage, node(stage))
            graph.add_edge(previous, stage)
            previous = stage
        graph.add_edge(previous, END)
        result = {}
        error = []
        try:
            result = graph.compile().invoke({"features": snapshot.numeric_input()})
        except AgentFailure as exc:
            error = [str(exc)]
        # Preserve validated predecessor output even if a later node failed.
        validated = {call["stage"]: call["validated_output"] for call in calls if call["status"] == "VALIDATED"}
        return {"analysis_output": validated.get("analysis"), "critic_output": validated.get("critic"),
                "decision_output": result.get("decision"), "calls": calls, "reason_codes": error}


def replay_decision(snapshot: FeatureSnapshot, original: dict, *, baseline: bool = False, expected_provider=None) -> dict:
    outputs = {"analysis_output": None, "critic_output": None, "decision_output": None, "calls": [], "reason_codes": []}
    state = {"features": snapshot.numeric_input()}
    try:
        if original["snapshot"]["dataset_hash"] != snapshot.dataset_hash or canonical_hash({k: original["snapshot"][k] for k in snapshot.numeric_input()}) != canonical_hash(snapshot.numeric_input()):
            outputs["reason_codes"] = ["REPLAY_INPUT_MISMATCH"]
            return outputs
        if baseline:
            outputs["decision_output"] = DecisionOutput.model_validate(original["decision_output"]).model_dump(mode="json")
            return outputs
        for source_call in original.get("calls", []):
            if expected_provider is not None and source_call.get('provider_attribution') != expected_provider:
                raise ValueError('Replay provider attribution mismatch')
            stage = source_call["stage"]
            payload = {"features": state["features"]}
            if stage in ("critic", "decision") and "analysis" in state:
                payload["analysis"] = state["analysis"]
            if stage == "decision" and "critic" in state:
                payload["critic"] = state["critic"]
            schema = SCHEMAS[stage].model_json_schema()
            expected_prompt = canonical_hash({"version": PROMPT_VERSION, "instructions": INSTRUCTIONS[stage], "schema": schema})
            if canonical_hash(payload) != source_call["input_hash"] or expected_prompt != source_call["prompt_hash"]:
                raise ValueError("mismatch")
            if source_call["status"] == "VALIDATED":
                parsed = validate_agent_output(stage, source_call["raw_response"]).model_dump(mode="json")
                if parsed != source_call["validated_output"]:
                    raise ValueError("validated output mismatch")
                state[stage] = parsed
                outputs[stage + "_output"] = parsed
            outputs["calls"].append({**source_call, "call_id": str(uuid4()), "replayed_from_call_id": source_call["call_id"],
                                     "mode": "replay", "source_cost_usd": source_call["cost_usd"], "cost_usd": 0,
                                     "input_tokens": 0, "output_tokens": 0, "reserved_usd": 0, "latency_ms": 0, "cost_status": "replay_zero"})
        if outputs["decision_output"] != original.get("decision_output"):
            raise ValueError("decision mismatch")
        if original.get("status") != "APPROVED":
            outputs["reason_codes"] = original.get("reason_codes") or ["REPLAY_SOURCE_SKIPPED"]
        elif outputs["decision_output"] is None:
            raise ValueError("no decision")
    except (ValueError, KeyError, TypeError):
        outputs["decision_output"] = None
        outputs["reason_codes"] = ["REPLAY_CONTRACT_MISMATCH"]
    return outputs
