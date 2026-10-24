import json
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents import AgentWorkflow, MockProvider, OpenAIProvider, Reply, replay_decision
from app.data import DatasetRegistry
from app.experiments import run_experiment
from app.features import snapshots
from app.main import create_app
from app.models import ExperimentConfig
from app.storage import Store


@pytest.fixture(scope="module")
def dataset():
    return DatasetRegistry().load("synthetic-spy-v1")


@pytest.fixture(scope="module")
def snapshot(dataset):
    return snapshots(dataset)[date(2024, 1, 5)]


def test_graph_has_numeric_only_inputs_and_trace(snapshot):
    workflow = AgentWorkflow(ExperimentConfig())
    result = workflow.evaluate(snapshot)
    assert [c["stage"] for c in result["calls"]] == ["analysis", "critic", "decision"]
    assert result["decision_output"]["target_weight"] in (0, .5, 1)
    for call in result["calls"]:
        assert "SPY" not in json.dumps(call["input"])
        assert "2024" not in json.dumps(call["input"])
        assert call["status"] == "VALIDATED"
        assert call["prompt_hash"] and call["raw_response"] and call["schema_hash"]


@pytest.mark.parametrize("raw", ['{"target_weight": 1}', 'not-json', '{"target_weight": 1,"confidence":0.8,"rationale":"test","evidence":["future_return"]}'])
def test_malformed_and_unknown_evidence_skip(snapshot, raw):
    class InvalidProvider(MockProvider):
        def reply(self, *args):
            return Reply(raw)
    result = AgentWorkflow(ExperimentConfig(strategy_id="single_agent"), InvalidProvider()).evaluate(snapshot)
    assert result["decision_output"] is None
    assert result["reason_codes"] == ["INVALID_AGENT_OUTPUT"]
    assert result["calls"][0]["raw_response"] == raw


def test_timeout_and_budget_are_bounded(snapshot):
    class TimeoutProvider(MockProvider):
        def reply(self, *args):
            raise httpx.ReadTimeout("timeout")
    workflow = AgentWorkflow(ExperimentConfig(), TimeoutProvider())
    result = workflow.evaluate(snapshot)
    assert len(result["calls"]) == 2
    assert result["reason_codes"] == ["PROVIDER_TIMEOUT"]
    result = AgentWorkflow(ExperimentConfig(max_llm_calls=1)).evaluate(snapshot)
    assert result["decision_output"] is None
    assert result["analysis_output"] is not None
    assert result["reason_codes"] == ["BUDGET_EXHAUSTED"]


def test_openai_fixture_validates_structure_cost_and_does_not_expose_key(snapshot, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-private-key")
    monkeypatch.setenv("LLM_MODEL", "fixture-model")
    monkeypatch.setenv("LLM_INPUT_USD_PER_MILLION", "1")
    monkeypatch.setenv("LLM_OUTPUT_USD_PER_MILLION", "2")

    def handle(request):
        payload = json.loads(request.content)
        assert payload["store"] is False
        schema = payload["text"]["format"]["schema"]
        assert set(schema["required"]) == set(schema["properties"])
        raw = json.dumps({"target_weight": .5, "confidence": .7, "rationale": "Fixture result.", "evidence": ["ma20"], "acknowledged_risks": []})
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": raw}]}], "usage": {"input_tokens": 100, "output_tokens": 20}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        workflow = AgentWorkflow(ExperimentConfig(strategy_id="single_agent", llm_mode="api"), OpenAIProvider(client))
        result = workflow.evaluate(snapshot)
    assert result["decision_output"]["target_weight"] == .5
    assert result["calls"][0]["cost_usd"] == pytest.approx(.00014)
    assert "test-private-key" not in json.dumps(result)


def test_holiday_schedule_shared_start_and_next_open(dataset):
    results = []
    for strategy in ("buy_hold", "ma_crossover", "single_agent", "multi_agent"):
        config = ExperimentConfig(strategy_id=strategy, start_date="2024-03-25", end_date="2024-04-08")
        results.append(run_experiment(config, dataset))
    assert {r["first_execution_session"] for r in results} == {"2024-04-01"}
    for result in results:
        assert result["decisions"][0]["decision_session"] == "2024-03-28"  # Good Friday closure
        for trade in result["trades"]:
            decision = next(d for d in result["decisions"] if d["decision_id"] == trade["decision_id"])
            assert trade["execution_session"] > decision["decision_session"]
            bar = next(b for b in dataset.bars if str(b.session_date) == trade["execution_session"])
            assert trade["reference_open"] == bar.open


def test_agent_failure_retains_position_and_replay_never_calls_provider(dataset, monkeypatch):
    config = ExperimentConfig(strategy_id="multi_agent", start_date="2024-01-01", end_date="2024-02-01", max_llm_calls=3)
    original = run_experiment(config, dataset)
    assert original["metrics"]["skipped_decisions"] > 0
    assert len(original["trades"]) <= 1
    if original["trades"]:
        assert original["equity"][-1]["shares"] == original["trades"][0]["shares_after"]
    monkeypatch.setattr(MockProvider, "reply", lambda *a: (_ for _ in ()).throw(AssertionError("Replay called provider")))
    replay = run_experiment(config.model_copy(update={"llm_mode": "replay"}), dataset, source=original)
    assert [p["equity"] for p in replay["equity"]] == [p["equity"] for p in original["equity"]]
    assert replay["metrics"]["llm_calls"] == 0
    assert replay["metrics"]["llm_cost_usd"] == 0


def wait_completed(client, run_id):
    for _ in range(200):
        row = client.get(f"/api/v1/experiments/{run_id}").json()
        if row["status"] in ("COMPLETED", "FAILED"):
            assert row["status"] == "COMPLETED", row
            return row
        time.sleep(.02)
    raise AssertionError("worker did not complete")


def test_api_lifecycle_replay_export_and_comparison(tmp_path):
    with TestClient(create_app(tmp_path / "test.sqlite3")) as client:
        cfg = {"start_date": "2024-01-01", "end_date": "2024-02-29", "strategy_id": "multi_agent"}
        response = client.post("/api/v1/experiments", json=cfg, headers={"X-Idempotency-Key": "same-request"})
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_completed(client, run_id)
        assert client.post("/api/v1/experiments", json=cfg, headers={"X-Idempotency-Key": "same-request"}).json()["run_id"] == run_id
        decisions = client.get(f"/api/v1/experiments/{run_id}/decisions").json()
        assert decisions[0]["snapshot"]["as_of"]
        assert len(decisions[0]["calls"]) == 3
        replay_id = client.post("/api/v1/experiments", json={**cfg, "llm_mode": "replay", "replay_source_run_id": run_id}).json()["run_id"]
        replay = wait_completed(client, replay_id)
        assert replay["metrics"]["llm_calls"] == 0
        compared = client.post("/api/v1/experiments/compare", json={"run_ids": [run_id, replay_id]})
        assert compared.status_code == 200
        assert compared.json()["curves"][0]["equity"] == compared.json()["curves"][1]["equity"]
        exported = client.get(f"/api/v1/experiments/{run_id}/export")
        assert exported.json()["export_version"] == 1
        assert exported.json()["result"]["decisions"] == decisions
        other_id = client.post("/api/v1/experiments", json={**cfg, "fee_bps": 10}).json()["run_id"]
        wait_completed(client, other_id)
        assert client.post("/api/v1/experiments/compare", json={"run_ids": [run_id, other_id]}).status_code == 422


def test_restart_repairs_runs_and_worker_lock(tmp_path):
    path = tmp_path / "test.sqlite3"
    store = Store(path)
    store.initialize()
    run_id, _ = store.create(ExperimentConfig())
    second = Store(path)
    with pytest.raises(RuntimeError, match="another QuantAgent"):
        second.initialize()
    store.close()
    second.initialize()
    assert second.get(run_id)["status"] == "FAILED"
    assert second.get(run_id)["error_message"] == "SERVICE_RESTARTED"
    second.close()


def test_invalid_features_and_last_session_never_create_an_order(dataset):
    zero = type(dataset)(dataset.metadata, tuple(b.model_copy(update={"volume":0}) for b in dataset.bars))
    result = run_experiment(ExperimentConfig(start_date="2024-01-01",end_date="2024-01-19"), zero)
    assert result["trades"] == []
    assert result["metrics"]["llm_calls"] == 0
    assert result["metrics"]["mock_calls"] == 0
    assert result["decisions"][0]["reason_codes"] == ["INVALID_FEATURES"]
    assert result["decisions"][-1]["reason_codes"] == ["NO_EXECUTION_SESSION"]
    assert result["metrics"]["final_equity"] == 100000


def test_idempotency_is_atomic_for_concurrent_requests(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store = Store(tmp_path / "concurrent.sqlite3")
    store.initialize()
    barrier = Barrier(2)
    def create():
        barrier.wait()
        return store.create(ExperimentConfig(), "same")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        outcomes = [future.result() for future in futures]
    assert outcomes[0][0] == outcomes[1][0]
    assert sum(fresh for _,fresh in outcomes) == 1
    store.close()


def test_mock_counts_are_not_real_llm_calls(dataset):
    result = run_experiment(ExperimentConfig(start_date="2024-01-01",end_date="2024-01-31"),dataset)
    assert result["metrics"]["llm_calls"] == 0
    assert result["metrics"]["mock_calls"] > 0
    assert result["decision_origin"] == "mock"


@pytest.mark.parametrize("strategy",["buy_hold","ma_crossover"])
def test_baseline_replay_uses_saved_decisions(dataset,monkeypatch,strategy):
    config=ExperimentConfig(strategy_id=strategy,start_date="2024-01-01",end_date="2024-06-30")
    original=run_experiment(config,dataset)
    monkeypatch.setattr("app.experiments.baseline_output",lambda *args: (_ for _ in ()).throw(AssertionError("Replay recomputed the strategy")))
    replay=run_experiment(config.model_copy(update={"llm_mode":"replay"}),dataset,source=original)
    assert replay["equity"]==original["equity"]
    assert [d["approved_weight"] for d in replay["decisions"]]==[d["approved_weight"] for d in original["decisions"]]
