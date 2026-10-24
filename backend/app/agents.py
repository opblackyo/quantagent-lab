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

SCHEMAS = {"analysis": AnalysisOutput, "critic": CriticOutput, "decision": DecisionOutput}
INSTRUCTIONS = {
    "analysis": "Analyze the numeric market features. Classify the regime and cite only provided feature field names. Do not invent news, dates, events, forecasts or external evidence.",
    "critic": "Review the numeric features and validated analysis. Identify contrary evidence and risks. Use only VOLATILITY_HIGH, TREND_CONFLICT, WEAK_EVIDENCE or NO_OBJECTION; NO_OBJECTION must stand alone. Do not invent external facts.",
    "decision": "Using only the supplied numeric features and any validated predecessor outputs, propose a long-only target weight of 0, 0.5 or 1. Cite only valid numeric feature names. Confidence is an uncalibrated self-assessment, not a profit probability. Do not invent external facts.",
}


class AgentFailure(Exception):
    pass


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


def strict_schema(schema: dict) -> dict:
    schema = json.loads(json.dumps(schema))

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["required"] = list(node.get("properties", {}))
                node["additionalProperties"] = False
            for value in list(node.values()):
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


class OpenAIProvider:
    settings = {"max_output_tokens": 1024, "store": False}

    def __init__(self, client: httpx.Client | None = None):
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.model_id = os.environ.get("LLM_MODEL", "")
        try:
            self.input_rate = float(os.environ.get("LLM_INPUT_USD_PER_MILLION", "nan"))
            self.output_rate = float(os.environ.get("LLM_OUTPUT_USD_PER_MILLION", "nan"))
        except ValueError:
            self.input_rate = self.output_rate = float("nan")
        if not self.key or not self.model_id or not all(math.isfinite(v) and v > 0 for v in (self.input_rate, self.output_rate)):
            raise ValueError("API mode requires a model, API key, and explicit positive input/output price assumptions")
        self.client = client

    def payload(self, stage, payload, schema):
        return {"model": self.model_id, "instructions": INSTRUCTIONS[stage], "input": json.dumps(payload, sort_keys=True),
                "text": {"format": {"type": "json_schema", "name": stage + "_output", "strict": True, "schema": strict_schema(schema)}},
                **self.settings}

    def reserve_cost(self, stage, payload, schema):
        # Deliberately conservative byte bound plus schema/protocol allowance.
        bound = len(json.dumps(self.payload(stage, payload, schema)).encode()) + 4096
        return (bound * self.input_rate + self.settings["max_output_tokens"] * self.output_rate) / 1e6

    def reply(self, stage, payload, schema):
        request = self.payload(stage, payload, schema)
        kwargs = {"json": request, "headers": {"Authorization": "Bearer " + self.key}, "timeout": 30.0}
        if self.client:
            response = self.client.post("https://api.openai.com/v1/responses", **kwargs)
        else:
            with httpx.Client() as client:
                response = client.post("https://api.openai.com/v1/responses", **kwargs)
        response.raise_for_status()
        envelope = response.json()
        usage = envelope.get("usage") or {}
        known = all(isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0 for k in ("input_tokens", "output_tokens"))
        input_tokens, output_tokens = (usage["input_tokens"], usage["output_tokens"]) if known else (0, 0)
        raw = "".join(part.get("text", "") for item in envelope.get("output", []) if item.get("type") == "message" for part in item.get("content", []) if part.get("type") == "output_text")
        if envelope.get("status") != "completed":
            raw = ""  # Still return usage/envelope so paid incomplete calls are recorded.
        return Reply(raw, input_tokens, output_tokens, (input_tokens*self.input_rate + output_tokens*self.output_rate)/1e6, envelope, known)


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
    def __init__(self, config: ExperimentConfig, provider=None):
        self.config = config
        self.provider = provider or (OpenAIProvider() if config.llm_mode == "api" else MockProvider())
        self.attempts = 0
        self.spent = 0.0
        self.actual_cost = 0.0
        self.all_calls: list[dict] = []
        if config.llm_mode == "api" and config.model_id and config.model_id != self.provider.model_id:
            raise ValueError("requested model differs from the backend-configured model")

    def _call(self, stage: str, payload: dict, calls: list, snapshot: FeatureSnapshot, on_call):
        schema = SCHEMAS[stage].model_json_schema()
        prompt_hash = canonical_hash({"version": PROMPT_VERSION, "instructions": INSTRUCTIONS[stage], "schema": schema})
        for attempt in range(2):
            reserve = self.provider.reserve_cost(stage, payload, schema)
            if self.attempts >= self.config.max_llm_calls or self.spent + reserve > self.config.max_llm_budget_usd:
                raise AgentFailure("BUDGET_EXHAUSTED")
            self.attempts += 1
            self.spent += reserve
            call = {"call_id": str(uuid4()), "stage": stage, "attempt": attempt + 1, "mode": self.config.llm_mode,
                    "model_id": self.provider.model_id, "model_settings": self.provider.settings,
                    "input": payload, "input_hash": canonical_hash(payload), "prompt_hash": prompt_hash,
                    "prompt_version": PROMPT_VERSION, "schema_hash": canonical_hash(schema), "instructions": INSTRUCTIONS[stage],
                    "cache_key": canonical_hash({"data": snapshot.dataset_hash, "feature_version": snapshot.feature_version,
                                                "model": self.provider.model_id, "settings": self.provider.settings,
                                                "prompt": prompt_hash, "input": payload}),
                    "raw_response": None, "validated_output": None, "status": "RUNNING", "cost_usd": 0,
                    "input_tokens": 0, "output_tokens": 0, "reserved_usd": reserve}
            call["cost_status"] = "unknown_reserved" if self.config.llm_mode == "api" else "mock_zero"
            if isinstance(self.provider, OpenAIProvider):
                call["price_assumptions_usd_per_million"] = {"input": self.provider.input_rate, "output": self.provider.output_rate}
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
                parsed = SCHEMAS[stage].model_validate_json(reply.raw)
                call.update(validated_output=parsed.model_dump(mode="json"), status="VALIDATED")
                return call["validated_output"]
            except AgentFailure:
                raise
            except httpx.TimeoutException:
                call["status"] = "FAILED"
                call["error_code"] = "PROVIDER_TIMEOUT"
                retryable = True
            except httpx.HTTPStatusError as exc:
                call["status"] = "FAILED"
                call["error_code"] = "PROVIDER_HTTP_ERROR"
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
                on_call(call)
            if not retryable or attempt == 1:
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


def replay_decision(snapshot: FeatureSnapshot, original: dict, *, baseline: bool = False) -> dict:
    outputs = {"analysis_output": None, "critic_output": None, "decision_output": None, "calls": [], "reason_codes": []}
    if original.get("status") != "APPROVED":
        outputs["reason_codes"] = original.get("reason_codes") or ["REPLAY_SOURCE_SKIPPED"]
        outputs["analysis_output"] = original.get("analysis_output")
        outputs["critic_output"] = original.get("critic_output")
        outputs["calls"] = [{**c, "mode": "replay", "call_id": str(uuid4()), "replayed_from_call_id": c["call_id"],
                             "source_cost_usd": c["cost_usd"], "cost_usd": 0, "input_tokens": 0, "output_tokens": 0,
                             "reserved_usd": 0, "latency_ms": 0, "cost_status": "replay_zero"} for c in original.get("calls", [])]
        return outputs
    state = {"features": snapshot.numeric_input()}
    try:
        if original["snapshot"]["dataset_hash"] != snapshot.dataset_hash or canonical_hash({k: original["snapshot"][k] for k in snapshot.numeric_input()}) != canonical_hash(snapshot.numeric_input()):
            outputs["reason_codes"] = ["REPLAY_INPUT_MISMATCH"]
            return outputs
        if baseline:
            outputs["decision_output"] = DecisionOutput.model_validate(original["decision_output"]).model_dump(mode="json")
            return outputs
        for source_call in original.get("calls", []):
            if source_call["status"] != "VALIDATED":
                continue
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
            parsed = SCHEMAS[stage].model_validate_json(source_call["raw_response"]).model_dump(mode="json")
            state[stage] = parsed
            outputs[stage + "_output"] = parsed
            outputs["calls"].append({**source_call, "call_id": str(uuid4()), "replayed_from_call_id": source_call["call_id"],
                                     "mode": "replay", "source_cost_usd": source_call["cost_usd"], "cost_usd": 0,
                                     "input_tokens": 0, "output_tokens": 0, "reserved_usd": 0, "latency_ms": 0, "cost_status": "replay_zero"})
        if outputs["decision_output"] is None:
            raise ValueError("no decision")
    except (ValueError, KeyError, TypeError):
        outputs["decision_output"] = None
        outputs["reason_codes"] = ["REPLAY_CONTRACT_MISMATCH"]
    return outputs
