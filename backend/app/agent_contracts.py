"""Pure role contracts shared by preview and execution; no agent runtime import."""
import json

from .models import AnalysisOutput, CriticOutput, DecisionOutput

SCHEMAS = {"analysis": AnalysisOutput, "critic": CriticOutput, "decision": DecisionOutput}
INSTRUCTIONS = {
    "analysis": "Analyze the numeric market features. Classify the regime and cite only provided feature field names. Do not invent news, dates, events, forecasts or external evidence.",
    "critic": "Review the numeric features and validated analysis. Identify contrary evidence and risks. Use only VOLATILITY_HIGH, TREND_CONFLICT, WEAK_EVIDENCE or NO_OBJECTION; NO_OBJECTION must stand alone. Do not invent external facts.",
    "decision": "Using only the supplied numeric features and any validated predecessor outputs, propose a long-only target weight of 0, 0.5 or 1. Cite only valid numeric feature names. Confidence is an uncalibrated self-assessment, not a profit probability. Do not invent external facts.",
}


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


class AgentFailure(Exception):
    pass
