import os
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .agents import MockProvider
from .gateway import application_launch_block
from .data import ROOT, DatasetRegistry
from .experiments import run_experiment, schedule
from .features import feature_frame, snapshots
from .local_http import LocalHttpMiddleware, loopback_origins
from .models import ENGINE_VERSION, FEATURE_VERSION, ExperimentConfig
from .public_projection import (
    project_bars, project_comparison, project_cost, project_dataset, project_decision,
    project_equity, project_events, project_metrics, project_run, project_trade, project_capabilities, public_detail,
)
from .research_http import register_research
from .storage import Store
from .executor import BoundedExecutor, WorkQueueFull
from .pilot import PilotBudget


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[UUID] = Field(min_length=2, max_length=8)


def create_app(db_path: Path | None = None, registry: DatasetRegistry | None = None, research=None, local_origins=()):
    if research is None:
        store = Store(db_path or Path(os.environ.get("QUANTAGENT_DB_PATH", ROOT / "artifacts/research.sqlite3")))
        registry = registry or DatasetRegistry()
    else:
        store = research.store
        registry = research.registry

    @asynccontextmanager
    async def lifespan(app):
        if research is None:
            store.initialize()
            executor = BoundedExecutor(thread_name_prefix="quantagent")
            app.state.executor = executor
            try:
                yield
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                store.close()
        else:
            # One already-open research store and its single serial worker. No second executor and no startup repair.
            app.state.executor = research.executor
            app.state.research = research
            try:
                yield
            finally:
                research.close()

    app = FastAPI(title="QuantAgent Lab", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.submission_gate = threading.RLock()

    def secrets():
        return () if research is None else research.secret_values()

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse(status_code=422, content={"detail": public_detail(exc, secrets())})

    @app.exception_handler(WorkQueueFull)
    async def full_queue(request, exc):
        return JSONResponse(status_code=409, content={'detail': 'WORK_QUEUE_FULL'})

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"detail": "Unknown experiment"})

    @app.exception_handler(RequestValidationError)
    async def rejected_body(request, exc):
        return JSONResponse(status_code=422, content={"detail": "REQUEST_BODY_REJECTED"})

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, str) else "REQUEST_REJECTED"
            return JSONResponse(status_code=exc.status_code, content={"detail": public_detail(detail, secrets())})
        return JSONResponse(status_code=500, content={"detail": "INTERNAL_ERROR"})

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "engine_version": ENGINE_VERSION}

    @app.get("/api/v1/system/capabilities")
    def capabilities():
        # Do not construct a provider or read a key. Live launch stays unavailable.
        return project_capabilities({"modes": ["mock", "replay"], "engine_version": ENGINE_VERSION, "feature_version": FEATURE_VERSION})

    @app.get("/api/v1/datasets")
    def datasets():
        return [project_dataset(item, secrets()) for item in registry.list()]

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
        return project_bars({"dataset": dataset.metadata, "bars": output}, secrets())

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
            raise ValueError(application_launch_block(config.model_provider))
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
            if config.research_group_id is not None and config.research_group_id != previous.get("research_group_id"):
                raise ValueError("Replay research group must match its source")
            config = config.model_copy(update={"model_provider": previous["model_provider"], "model_id": previous["model_id"],
                                               "research_group_id": previous.get("research_group_id")})
        with app.state.submission_gate:
            existing = None
            if idempotency_key:
                with store.read() as conn:
                    existing = conn.execute('SELECT run_id FROM runs WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if existing:
                # create() validates immutable config without creating a new row.
                run_id, _ = store.create(config, idempotency_key)
            else:
                with app.state.executor.admission() as submit:
                    run_id, fresh = store.create(config, idempotency_key)
                    if fresh:
                        submit(execute, run_id, config, dataset, source)
        return {"run_id": run_id, "status": store.get(run_id)["status"]}

    @app.get("/api/v1/experiments")
    def runs():
        return [project_run(row, secrets()) for row in store.list()]

    @app.get("/api/v1/pilots/{pilot_id}/budget")
    def pilot_budget(pilot_id: str):
        import json
        with store.read() as conn:
            if not conn.execute("SELECT name FROM sqlite_master WHERE name='pilot_budgets'").fetchone():
                raise HTTPException(404, "No paid pilot budget has been initialized")
            row = conn.execute('SELECT manifest_json FROM pilot_budgets WHERE pilot_id=?',(pilot_id,)).fetchone()
            revision=None
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_revisions'").fetchone():
                revision=conn.execute('SELECT receipt_json FROM pilot_revisions WHERE pilot_id=?',(pilot_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown pilot")
        manifest=json.loads(revision[0])['revision_manifest'] if revision else json.loads(row[0])
        return project_cost(PilotBudget.read_summary(store,manifest,pilot_id if revision else None), secrets())

    @app.post("/api/v1/experiments/compare")
    def compare(request: ComparisonRequest):
        if len(set(request.run_ids)) != len(request.run_ids):
            raise ValueError("comparison requires distinct run IDs")
        rows = [store.get(run_id, True) for run_id in request.run_ids]
        keys = ("dataset_hash", "start_date", "end_date", "initial_cash", "fee_bps", "slippage_bps", "max_exposure", "research_group_id")
        first = rows[0]
        for row in rows:
            if row["status"] != "COMPLETED":
                raise ValueError("all compared runs must be completed")
            if any(row["config"].get(key) != first["config"].get(key) for key in keys) or any(row["result"][key] != first["result"][key] for key in ("engine_version", "feature_version", "return_basis")):
                raise ValueError("runs must share dataset, period, capital, costs and engine")
        agent_runs = [row for row in rows if row["config"]["strategy_id"] in ("single_agent", "multi_agent")]
        if agent_runs:
            reference = agent_runs[0]
            for row in agent_runs[1:]:
                if row["config"].get('model_provider') != reference['config'].get('model_provider') or row["config"]["prompt_version"] != reference["config"]["prompt_version"] or any(row["result"].get(key) != reference["result"].get(key) for key in ("decision_origin", "model_ids", "provider_settings", "provider_attribution")):
                    raise ValueError("agent comparisons must share decision origin, model version and prompt version")
        return project_comparison({"runs": [{k: v for k, v in row.items() if k != "result"} for row in rows],
                "curves": [{"run_id": row["run_id"], "strategy_id": row["config"]["strategy_id"], "equity": row["result"]["equity"]} for row in rows],
                "return_basis": "price-only"}, secrets())

    @app.get("/api/v1/experiments/{run_id}")
    def run(run_id: UUID):
        return project_run(store.get(run_id), secrets())

    def result(run_id):
        run = store.get(run_id, True)
        if run["status"] != "COMPLETED":
            raise HTTPException(409, "Experiment has not completed")
        return run["result"]

    @app.get("/api/v1/experiments/{run_id}/decisions")
    def decisions(run_id: UUID):
        return [project_decision(item, secrets()) for item in result(run_id)["decisions"]]

    @app.get("/api/v1/experiments/{run_id}/trades")
    def trades(run_id: UUID):
        return [project_trade(item, secrets()) for item in result(run_id)["trades"]]

    @app.get("/api/v1/experiments/{run_id}/equity")
    def equity(run_id: UUID):
        return [project_equity(item, secrets()) for item in result(run_id)["equity"]]

    @app.get("/api/v1/experiments/{run_id}/metrics")
    def metrics(run_id: UUID):
        return project_metrics(result(run_id)["metrics"], secrets())

    @app.get("/api/v1/experiments/{run_id}/events")
    def events(run_id: UUID):
        return project_events(store.events(run_id), secrets())

    @app.get("/api/v1/experiments/{run_id}/export")
    def export(run_id: UUID):
        run = store.get(run_id, True)
        if run["status"] not in ("COMPLETED", "FAILED"):
            raise HTTPException(409, "Experiment is still running")
        body = project_run(run, secrets(), include_result=True)
        body["export_version"] = 1
        body["events"] = project_events(store.events(run_id), secrets())
        return JSONResponse(body, headers={"Content-Disposition": 'attachment; filename="quantagent-run.json"'})

    app.add_middleware(LocalHttpMiddleware, origins=tuple(local_origins))
    register_research(app, research)
    return app


app = create_app(local_origins=loopback_origins(os.environ.get("QUANTAGENT_LOCAL_ORIGINS", "").split(",")))
