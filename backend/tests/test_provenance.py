import json
from pathlib import Path

import httpx
import pytest

from app.agents import AgentWorkflow, MockProvider, OpenAIProvider
from app.data import DatasetRegistry
from app.features import snapshots
from app.models import ExperimentConfig


def test_mutated_dataset_is_rejected(tmp_path):
    registry=DatasetRegistry()
    path, meta=registry.entries()[0]
    source=path.parent/meta["file"]
    (tmp_path/meta["file"]).write_bytes(source.read_bytes()+b"\n")
    (tmp_path/"changed.metadata.json").write_text(json.dumps(meta),encoding="utf-8")
    with pytest.raises(ValueError,match="SHA-256 mismatch"):
        DatasetRegistry([tmp_path]).load(meta["id"])


def test_unknown_api_usage_keeps_reservation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY","private-fixture-key")
    monkeypatch.setenv("LLM_MODEL","fixture-model")
    monkeypatch.setenv("LLM_INPUT_USD_PER_MILLION","1")
    monkeypatch.setenv("LLM_OUTPUT_USD_PER_MILLION","2")
    dataset=DatasetRegistry().load("synthetic-spy-v1")
    snapshot=next(iter(snapshots(dataset).values()))
    raw=MockProvider().reply("decision",{"features":snapshot.numeric_input()},{}).raw
    def response(request):
        return httpx.Response(200,json={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":raw}]}]})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        workflow=AgentWorkflow(ExperimentConfig(llm_mode="api",strategy_id="single_agent"),OpenAIProvider(client))
        result=workflow.evaluate(snapshot)
    call=result["calls"][0]
    assert result["decision_output"] is not None
    assert call["cost_status"] == "unknown_reserved"
    assert workflow.spent == call["reserved_usd"] > 0
    assert "private-fixture-key" not in json.dumps(result)


def test_comparison_rejects_different_model_versions(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.experiments import run_experiment
    class OtherMock(MockProvider):
        model_id="another-fixture-version"
    dataset=DatasetRegistry().load("synthetic-spy-v1")
    app=create_app(tmp_path/"compare.sqlite3")
    with TestClient(app) as client:
        run_ids=[]
        for strategy,provider in [("single_agent",MockProvider()),("multi_agent",OtherMock())]:
            config=ExperimentConfig(strategy_id=strategy,start_date="2024-01-01",end_date="2024-01-31",dataset_hash=dataset.hash)
            run_id,_=app.state.store.create(config)
            app.state.store.update(run_id,status="COMPLETED",progress=100,result=run_experiment(config,dataset,provider=provider))
            run_ids.append(run_id)
        response=client.post("/api/v1/experiments/compare",json={"run_ids":run_ids})
    assert response.status_code==422
    assert "model version" in response.json()["detail"]


@pytest.mark.parametrize("field,value",[("initial_cash",True),("fee_bps","5"),("slippage_bps",False),("max_llm_calls",True),("max_llm_budget_usd","2")])
def test_experiment_numeric_controls_reject_boolean_and_text(field,value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ExperimentConfig(**{field:value})
