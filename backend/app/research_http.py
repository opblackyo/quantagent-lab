"""Narrow research HTTP routes. The default runtime cannot spend or send."""
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated

from .public_projection import project_result, project_study, _sanitize
from .research_contracts import StudyRequest
from .research_report import build_study_report
from .research_service import ExecutionError, PlanningError
from .research_store import StudyStorageError

STRATEGIES = ("buy_hold", "ma_crossover", "single_agent", "multi_agent")
_STATUS = {
    "UNKNOWN_STUDY": 404,
    "UNKNOWN_SOURCE": 404,
    "PROFILE_NOT_ALLOWLISTED": 404,
    "STALE_MANIFEST": 409,
    "FROZEN_PLAN_DRIFT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "WORK_QUEUE_FULL": 409,
    "WORKER_BUSY": 409,
    "REPLAY_NOT_COMPLETE": 409,
    "RESUME_NOT_ELIGIBLE": 409,
    "APPROVAL_OR_COHORT_ALREADY_ADMITTED": 409,
    "AUTHORIZATION_REUSE_CONFLICT": 409,
    "SERVICE_DISABLED": 409,
    "LIVE_APPROVAL_REQUIRED": 409,
    "LIVE_APPROVAL_SCOPE_MISMATCH": 409,
    "LIVE_ENDPOINT_REJECTED": 409,
    "AUTHORIZATION_UNAVAILABLE": 409,
    "NOT_ELIGIBLE": 409,
    "NO_VERIFIED_UNSTARTED_CONTINUATION": 409,
    "TERMINAL_STUDY": 409,
}


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]
    request: StudyRequest


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DisabledResearch:
    """No database, provider, grant, or worker. Preview is not funding."""

    fixture = False

    def secret_values(self):
        return ()

    def readiness(self):
        return {"service": "disabled", "live_provider": False, "real_spending_authorized": False,
                "execution_authorized": False, "funding": None, "fixture_transport_posts": 0,
                "profiles": [], "labels": ["MOCK_REPLAY_ONLY", "LIVE_PROVIDER_DISABLED"], "queue_limit": 1}

    def preview(self, request):
        raise PlanningError("SERVICE_DISABLED")

    def submit(self, request, manifest_hash, idempotency_key):
        raise PlanningError("SERVICE_DISABLED")

    def public_report(self, study_id):
        raise StudyStorageError("UNKNOWN_STUDY")

    def cancel(self, study_id):
        raise PlanningError("SERVICE_DISABLED")

    def reconcile(self, study_id):
        raise PlanningError("SERVICE_DISABLED")

    def resume(self, study_id):
        raise PlanningError("SERVICE_DISABLED")

    def replay(self, study_id):
        raise PlanningError("SERVICE_DISABLED")


def _study_id(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 36:
        from fastapi import HTTPException
        raise HTTPException(404, "UNKNOWN_STUDY")
    try:
        return str(UUID(value))
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(404, "UNKNOWN_STUDY") from None


def register_research(app: FastAPI, research):
    runtime = research or DisabledResearch()

    def secrets():
        return runtime.secret_values()

    def guard(fn):
        try:
            return fn()
        except (PlanningError, StudyStorageError, ExecutionError, ValueError) as exc:
            from .public_projection import public_detail
            code = public_detail(exc, secrets())
            return JSONResponse({"detail": code}, status_code=_STATUS.get(code, 422))

    def report(study_id):
        projected = guard(lambda: project_study(runtime.public_report(study_id), secrets()))
        return projected

    @app.get("/api/v1/research/readiness")
    def readiness():
        return _sanitize(runtime.readiness(), secrets())

    @app.post("/api/v1/research/preview")
    def preview(body: StudyRequest):
        view = guard(lambda: runtime.preview(body))
        if isinstance(view, JSONResponse):
            return view
        ready = False
        checker = getattr(runtime, "server_live_ready", None)
        if checker is not None and body.transport_intent == "live":
            ready = bool(checker(body, view.manifest_hash))
        body_out = {"plan": view.model_dump(mode="json"), "execution_authorized": False,
                    "real_spending_authorized": False, "preview_is_not_funding": True,
                    "server_live_ready": ready,
                    "labels": list(runtime.readiness().get("labels") or [])}
        return _sanitize(body_out, secrets())

    @app.post("/api/v1/research/studies", status_code=202)
    def start(body: StartBody):
        study_id = guard(lambda: runtime.submit(body.request, body.manifest_hash, body.idempotency_key))
        if isinstance(study_id, JSONResponse):
            return study_id
        payload = report(study_id)
        if isinstance(payload, JSONResponse):
            return payload
        return JSONResponse(payload, status_code=202)

    @app.get("/api/v1/research/studies")
    def studies():
        rows = [] if not runtime.fixture else runtime.store.list_studies()
        return _sanitize({"studies": rows, "service": runtime.readiness()["service"]}, secrets())

    @app.get("/api/v1/research/studies/{study_id}")
    def study(study_id: str):
        return report(_study_id(study_id))

    @app.post("/api/v1/research/studies/{study_id}/cancel")
    def cancel(study_id: str, body: EmptyBody):
        parsed = _study_id(study_id)
        failed = guard(lambda: runtime.cancel(parsed))
        if isinstance(failed, JSONResponse):
            return failed
        return report(parsed)

    @app.post("/api/v1/research/studies/{study_id}/reconcile")
    def reconcile(study_id: str, body: EmptyBody):
        parsed = _study_id(study_id)
        status = guard(lambda: runtime.reconcile(parsed))
        if isinstance(status, JSONResponse):
            return status
        if status == "NOT_ELIGIBLE":
            return JSONResponse({"detail": "NOT_ELIGIBLE"}, status_code=409)
        return report(parsed)

    @app.post("/api/v1/research/studies/{study_id}/resume")
    def resume(study_id: str, body: EmptyBody):
        parsed = _study_id(study_id)
        queued = guard(lambda: runtime.resume(parsed))
        if isinstance(queued, JSONResponse):
            return queued
        payload = report(parsed)
        if isinstance(payload, JSONResponse):
            return payload
        return JSONResponse(payload, status_code=202)

    @app.post("/api/v1/research/studies/{study_id}/replay")
    def replay(study_id: str, body: EmptyBody):
        parsed = _study_id(study_id)
        payload = guard(lambda: runtime.replay(parsed))
        if isinstance(payload, JSONResponse):
            return payload
        return project_study(payload, secrets())

    @app.get("/api/v1/research/studies/{study_id}/export")
    def export(study_id: str):
        parsed = _study_id(study_id)
        payload = report(parsed)
        if isinstance(payload, JSONResponse):
            return payload
        store = getattr(runtime, "store", None)
        if store is None:
            return JSONResponse(payload, headers={"Content-Disposition": 'attachment; filename="research-study.json"'})
        transport = "synthetic-fixture-not-live-api" if getattr(runtime, "fixture", False) else "unavailable"
        payload = dict(payload)
        payload["report"] = build_study_report(store, parsed, secrets(), transport_status=transport)
        return JSONResponse(_sanitize(payload, secrets()), headers={"Content-Disposition": 'attachment; filename="research-study.json"'})

    @app.get("/api/v1/research/studies/{study_id}/sources/{strategy}")
    def source(study_id: str, strategy: str):
        if strategy not in STRATEGIES:
            return JSONResponse({"detail": "UNKNOWN_SOURCE"}, status_code=404)
        parsed = _study_id(study_id)
        current = guard(lambda: runtime.public_report(parsed))
        if isinstance(current, JSONResponse):
            return current
        selected = (current.get("plan") or {}).get("strategy_ids") or STRATEGIES
        if strategy not in selected:
            return JSONResponse({"detail": "NOT_IN_STUDY_PROTOCOL", "included_in_denominator": False}, status_code=404)
        child = (current.get("source_runs") or {}).get(strategy)
        labels = [item for item in current.get("labels") or [] if isinstance(item, str)]
        if not child:
            body = {"strategy": strategy, "status": "NOT_RUN", "origin": "source", "partial": True,
                    "complete": False, "run_id": None, "decisions": [], "trades": [], "equity": [],
                    "metrics": None, "labels": labels, "return_basis": "price-only",
                    "price_only_not_total_return": True}
            return _sanitize(body, secrets())
        run = runtime.store.get(child["run_id"], True)
        result = project_result(run.get("result"), secrets()) or {}
        complete = run.get("status") == "COMPLETED"
        body = {"strategy": strategy, "status": run.get("status"), "origin": "source",
                "partial": not complete, "complete": complete, "run_id": str(child["run_id"]),
                "coverage": (current.get("coverage") or {}).get(strategy),
                "decisions": result.get("decisions") or [], "trades": result.get("trades") or [],
                "equity": result.get("equity") or [], "metrics": result.get("metrics") if complete else result.get("metrics"),
                "labels": labels, "return_basis": "price-only", "price_only_not_total_return": True}
        if not complete:
            body["not_complete"] = True
        return _sanitize(body, secrets())
