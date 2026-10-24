from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.agents import AgentWorkflow, MockProvider
from app.data import DatasetRegistry
from app.features import snapshots
from app.main import create_app
from app.models import ExperimentConfig
from app.storage import Store


def test_call_input_and_reservation_are_durable_before_provider(tmp_path):
    store=Store(tmp_path/"calls.sqlite3")
    store.initialize()
    config=ExperimentConfig(strategy_id="single_agent",llm_mode="api")
    run_id,_=store.create(config)
    class ObservedProvider(MockProvider):
        def reserve_cost(self,*args):
            return .1
        def reply(self,*args):
            event=store.events(run_id)[-1]
            assert event["event_type"]=="agent_call"
            assert event["payload"]["status"]=="RUNNING"
            assert event["payload"]["reserved_usd"]==.1
            assert event["payload"]["input_hash"]
            return super().reply(*args)
    snapshot=snapshots(DatasetRegistry().load("synthetic-spy-v1"))[date(2024,1,5)]
    workflow=AgentWorkflow(config,ObservedProvider())
    result=workflow.evaluate(snapshot,on_call=lambda call:store.record_event(run_id,"agent_call",call))
    events=store.events(run_id)
    assert len(events)==1
    assert events[0]["payload"]["status"]=="VALIDATED"
    assert events[0]["payload"]["raw_response"]==result["calls"][0]["raw_response"]
    store.update(run_id,status="COMPLETED",progress=100)
    with pytest.raises(ValueError,match="immutable"):
        store.record_event(run_id,"agent_call",result["calls"][0])
    store.close()


def test_restart_preserves_inflight_call_and_diagnostic_export(tmp_path):
    path=tmp_path/"restart.sqlite3"
    app=create_app(path)
    with TestClient(app):
        run_id,_=app.state.store.create(ExperimentConfig())
        app.state.store.update(run_id,status="RUNNING")
        app.state.store.record_event(run_id,"agent_call",{"call_id":"inflight","status":"RUNNING","mode":"api",
                                    "input":{"features":{"ma20":100}},"reserved_usd":.2,"cost_usd":0,"cost_status":"unknown_reserved"})
    with TestClient(create_app(path)) as client:
        assert client.get(f"/api/v1/experiments/{run_id}").json()["status"]=="FAILED"
        events=client.get(f"/api/v1/experiments/{run_id}/events").json()
        assert events[0]["payload"]["status"]=="INTERRUPTED"
        assert events[0]["payload"]["cost_status"]=="unknown_reserved"
        assert events[0]["payload"]["reserved_usd"]==.2
        exported=client.get(f"/api/v1/experiments/{run_id}/export")
        assert exported.status_code==200
        assert exported.json()["result"] is None
        assert exported.json()["events"]==events
        replay=client.post("/api/v1/experiments",json={"llm_mode":"replay","replay_source_run_id":run_id})
        assert replay.status_code==422
