import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .agents import MockProvider, OpenAIProvider, api_capabilities
from .data import ROOT, DatasetRegistry
from .experiments import run_experiment, schedule
from .features import feature_frame, snapshots
from .models import ENGINE_VERSION, FEATURE_VERSION, ExperimentConfig
from .storage import Store


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[UUID] = Field(min_length=2, max_length=8)


def create_app(db_path: Path | None = None, registry: DatasetRegistry | None = None):
    store = Store(db_path or Path(os.environ.get("QUANTAGENT_DB_PATH", ROOT / "artifacts/research.sqlite3")))
    registry = registry or DatasetRegistry()

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quantagent")
        app.state.executor = executor
        try:
            yield
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            store.close()

    app = FastAPI(title="QuantAgent Lab", version="0.1.0", lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"detail": "Unknown experiment"})

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "engine_version": ENGINE_VERSION}

    @app.get("/api/v1/system/capabilities")
    def capabilities():
        api = api_capabilities()
        return {"modes": ["mock", "replay"] + (["api"] if api["available"] else []), "api": api,
                "engine_version": ENGINE_VERSION, "feature_version": FEATURE_VERSION, "research_only": True,
                "return_basis": "price-only", "calendar": "XNYS", "broker_enabled": False}

    @app.get("/api/v1/datasets")
    def datasets():
        return registry.list()

    @app.get("/api/v1/market/bars")
    def bars(dataset_id: str = "synthetic-spy-v1", start_date: date | None = None, end_date: date | None = None):
        dataset = registry.load(dataset_id)
        frame = feature_frame(dataset)
        output = []
        for _, row in frame.iterrows():
            if (start_date and row.session_date < start_date) or (end_date and row.session_date > end_date):
                continue
            point = {key: getattr(row, key) for key in ("session_date", "open", "high", "low", "close", "volume")}
            point["session_date"] = str(point["session_date"])
            for key in ("ma20", "ma60", "rsi14", "realized_vol20"):
                value = float(row[key])
                point[key] = value if value == value else None
            output.append(point)
        return {"dataset": dataset.metadata, "bars": output}

    def execute(run_id, config, dataset, source):
        try:
            store.update(run_id, status="RUNNING")
            result = run_experiment(config, dataset, lambda n: store.update(run_id, progress=n), source,
                                    record_event=lambda kind,payload: store.record_event(run_id,kind,payload))
            store.update(run_id, status="COMPLETED", progress=100, result=result)
        except Exception:
            # Provider/raw validation details live in decision traces; never store exception text containing credentials.
            store.update(run_id, status="FAILED", error="EXPERIMENT_FAILED")

    @app.post("/api/v1/experiments", status_code=202)
    def create(config: ExperimentConfig, idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key", max_length=100)):
        dataset = registry.load(config.dataset_id)
        if config.dataset_hash and config.dataset_hash != dataset.hash:
            raise ValueError("configured dataset hash does not match selected dataset")
        config = config.model_copy(update={"dataset_hash": dataset.hash})
        schedule(dataset, config, snapshots(dataset))
        source = None
        if config.llm_mode == "mock" and config.strategy_id in ("single_agent", "multi_agent"):
            config = config.model_copy(update={"model_provider": None, "model_id": MockProvider.model_id})
        if config.llm_mode == "api" and config.strategy_id in ("single_agent", "multi_agent"):
            provider = OpenAIProvider()
            if config.model_id and config.model_id != provider.model_id:
                raise ValueError("requested model differs from backend configuration")
            config = config.model_copy(update={"model_provider": "openai", "model_id": provider.model_id})
        if config.llm_mode == "replay":
            source_run = store.get(config.replay_source_run_id, True)
            if source_run["status"] != "COMPLETED":
                raise ValueError("Replay source must be completed")
            previous = source_run["config"]
            if any(previous[key] != getattr(config, key) for key in ("strategy_id", "dataset_id", "dataset_hash", "prompt_version")):
                raise ValueError("Replay source strategy, dataset and prompt must match")
            source = source_run["result"]
            if source["feature_version"] != FEATURE_VERSION:
                raise ValueError("Replay feature version mismatch")
            config = config.model_copy(update={"model_provider": previous["model_provider"], "model_id": previous["model_id"]})
        run_id, fresh = store.create(config, idempotency_key)
        if fresh:
            app.state.executor.submit(execute, run_id, config, dataset, source)
        return {"run_id": run_id, "status": store.get(run_id)["status"]}

    @app.get("/api/v1/experiments")
    def runs():
        return store.list()

    @app.post("/api/v1/experiments/compare")
    def compare(request: ComparisonRequest):
        if len(set(request.run_ids)) != len(request.run_ids):
            raise ValueError("comparison requires distinct run IDs")
        rows = [store.get(run_id, True) for run_id in request.run_ids]
        keys = ("dataset_hash", "start_date", "end_date", "initial_cash", "fee_bps", "slippage_bps", "max_exposure")
        first = rows[0]
        for row in rows:
            if row["status"] != "COMPLETED":
                raise ValueError("all compared runs must be completed")
            if any(row["config"][key] != first["config"][key] for key in keys) or any(row["result"][key] != first["result"][key] for key in ("engine_version", "feature_version", "return_basis")):
                raise ValueError("runs must share dataset, period, capital, costs and engine")
        agent_runs = [row for row in rows if row["config"]["strategy_id"] in ("single_agent", "multi_agent")]
        if agent_runs:
            reference = agent_runs[0]
            for row in agent_runs[1:]:
                if row["config"]["prompt_version"] != reference["config"]["prompt_version"] or any(row["result"].get(key) != reference["result"].get(key) for key in ("decision_origin", "model_ids")):
                    raise ValueError("agent comparisons must share decision origin, model version and prompt version")
        return {"runs": [{k: v for k, v in row.items() if k != "result"} for row in rows],
                "curves": [{"run_id": row["run_id"], "strategy_id": row["config"]["strategy_id"], "equity": row["result"]["equity"]} for row in rows],
                "return_basis": "price-only"}

    @app.get("/api/v1/experiments/{run_id}")
    def run(run_id: UUID):
        return store.get(run_id)

    def result(run_id):
        run = store.get(run_id, True)
        if run["status"] != "COMPLETED":
            raise HTTPException(409, "Experiment has not completed")
        return run["result"]

    @app.get("/api/v1/experiments/{run_id}/decisions")
    def decisions(run_id: UUID):
        return result(run_id)["decisions"]

    @app.get("/api/v1/experiments/{run_id}/trades")
    def trades(run_id: UUID):
        return result(run_id)["trades"]

    @app.get("/api/v1/experiments/{run_id}/equity")
    def equity(run_id: UUID):
        return result(run_id)["equity"]

    @app.get("/api/v1/experiments/{run_id}/metrics")
    def metrics(run_id: UUID):
        return result(run_id)["metrics"]

    @app.get("/api/v1/experiments/{run_id}/events")
    def events(run_id: UUID):
        return store.events(run_id)

    @app.get("/api/v1/experiments/{run_id}/export")
    def export(run_id: UUID):
        run = store.get(run_id, True)
        if run["status"] not in ("COMPLETED", "FAILED"):
            raise HTTPException(409, "Experiment is still running")
        return JSONResponse({"export_version": 1, **run, "events": store.events(run_id)}, headers={"Content-Disposition": f'attachment; filename="quantagent-{run_id}.json"'})

    return app


app = create_app()
