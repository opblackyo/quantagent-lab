from datetime import timedelta
import importlib.metadata
import sys
from uuid import uuid4

from .agents import AgentWorkflow, replay_decision
from .backtest import Ledger, assess_risk, performance
from .data import ROOT, Dataset, get_calendar, sha256
from .features import snapshots
from .models import DecisionOutput, ENGINE_VERSION, ExperimentConfig, FEATURE_VERSION


def baseline_output(strategy: str, snapshot) -> dict:
    weight = 1.0 if strategy == "buy_hold" or snapshot.ma20 > snapshot.ma60 else 0.0
    return DecisionOutput(target_weight=weight, confidence=1, rationale="Fixed baseline rule.",
                          evidence=("ma20", "ma60") if strategy == "ma_crossover" else ()).model_dump(mode="json")


def schedule(dataset: Dataset, config: ExperimentConfig, features: dict) -> dict:
    cal = get_calendar(config.start_date, config.end_date)
    sessions = [d.date() for d in cal.sessions_in_range(str(config.start_date), str(config.end_date + timedelta(days=7)))]
    in_range = [d for d in sessions if d <= config.end_date]
    if not in_range or in_range[0] < dataset.bars[0].session_date or in_range[-1] > dataset.bars[-1].session_date:
        raise ValueError("requested market sessions fall outside the dataset")
    positions = {bar.session_date: i for i, bar in enumerate(dataset.bars)}
    decisions = {day: sessions[i + 1] for i, day in enumerate(sessions[:-1])
                 if day <= config.end_date and positions.get(day, -1) >= 59 and day.isocalendar()[:2] != sessions[i + 1].isocalendar()[:2]}
    if not any(execution <= config.end_date for execution in decisions.values()):
        raise ValueError("range has no weekly decision with enough warmup and a following execution session")
    return decisions


def run_experiment(config: ExperimentConfig, dataset: Dataset, progress=lambda n: None, source: dict | None = None, provider=None, record_event=lambda kind,payload: None) -> dict:
    features = snapshots(dataset)
    decisions_at = schedule(dataset, config, features)
    cal = get_calendar(config.start_date, config.end_date)
    ledger = Ledger(config.initial_cash)
    bars = [b for b in dataset.bars if config.start_date <= b.session_date <= config.end_date]
    agent_strategy = config.strategy_id in ("single_agent", "multi_agent")
    workflow = AgentWorkflow(config, provider) if agent_strategy and config.llm_mode != "replay" else None
    originals = {record["decision_session"]: record for record in (source or {}).get("decisions", [])}
    decisions, trades, equity = [], [], []
    pending = None
    buy_hold_decided = False
    for index, bar in enumerate(bars):
        day = bar.session_date
        if pending:
            decision, target = pending
            if str(day) != decision["execution_session"]:
                raise RuntimeError("execution calendar mismatch")
            trade = ledger.rebalance(target, bar.open, config.fee_bps, config.slippage_bps, day, decision["decision_id"])
            if trade:
                trade["executed_at"] = cal.session_open(str(day)).isoformat()
                trades.append(trade)
                decision["trade_id"] = trade["trade_id"]
                record_event("trade", trade)
            else:
                decision["reason_codes"].append("TARGET_ALREADY_MET")
            record_event("decision", decision)
            pending = None
        if day in decisions_at and not (config.strategy_id == "buy_hold" and buy_hold_decided):
            snap = features.get(day)
            execution = decisions_at[day]
            record = {"decision_id": str(uuid4()), "decision_session": str(day), "execution_session": str(execution) if execution <= config.end_date else None,
                      "snapshot_id": str(snap.snapshot_id) if snap else None, "snapshot": snap.model_dump(mode="json") if snap else None, "trade_id": None,
                      "analysis_output": None, "critic_output": None, "decision_output": None, "calls": [],
                      "requested_weight": None, "approved_weight": None, "status": "SKIPPED", "reason_codes": []}
            record_event("decision", {**record, "status": "PENDING"})
            if execution > config.end_date:
                record["reason_codes"] = ["NO_EXECUTION_SESSION"]
            elif snap is None:
                record["reason_codes"] = ["INVALID_FEATURES"]
            else:
                if config.llm_mode == "replay":
                    original = originals.get(str(day))
                    record.update(replay_decision(snap, original, baseline=not agent_strategy) if original else {"reason_codes": ["REPLAY_MISSING_DECISION"]})
                elif agent_strategy:
                    record.update(workflow.evaluate(snap, on_call=lambda call: record_event("agent_call", {"decision_id": record["decision_id"], **call})))
                else:
                    record["decision_output"] = baseline_output(config.strategy_id, snap)
                parsed = DecisionOutput.model_validate(record["decision_output"]) if record["decision_output"] else None
                risk = assess_risk(parsed, max_exposure=config.max_exposure)
                record["requested_weight"] = parsed.target_weight if parsed else None
                record["approved_weight"] = risk["approved_weight"]
                record["status"] = risk["status"]
                if not record["reason_codes"]:
                    record["reason_codes"] = risk["reason_codes"]
                if record["status"] == "APPROVED":
                    pending = (record, record["approved_weight"])
                    buy_hold_decided = config.strategy_id == "buy_hold"
            decisions.append(record)
            record_event("decision", record)
        equity.append({"session_date": str(day), "equity": ledger.equity(bar.close), "cash": ledger.cash,
                       "shares": ledger.shares, "close": bar.close})
        record_event("equity", equity[-1])
        if index % 10 == 0 or index == len(bars) - 1:
            progress(min(99, int((index + 1) / len(bars) * 100)))
    metrics = performance(equity, trades, config.initial_cash)
    calls = [call for record in decisions for call in record["calls"]]
    metrics.update(llm_calls=sum(c["mode"] == "api" for c in calls),
                   agent_calls=sum(c["mode"] != "replay" for c in calls), mock_calls=sum(c["mode"] == "mock" for c in calls),
                   llm_cost_usd=sum(c["cost_usd"] for c in calls),
                   uncertain_cost_reserve_usd=sum(c["reserved_usd"] for c in calls if c.get("cost_status") == "unknown_reserved"),
                   llm_input_tokens=sum(c["input_tokens"] for c in calls), llm_output_tokens=sum(c["output_tokens"] for c in calls),
                   llm_latency_ms=sum(c["latency_ms"] for c in calls),
                   reserved_or_actual_usd=workflow.spent if workflow else 0,
                   approved_decisions=sum(d["status"] == "APPROVED" for d in decisions),
                   skipped_decisions=sum(d["status"] == "SKIPPED" for d in decisions),
                   rejected_decisions=sum(d["status"] == "REJECTED" for d in decisions))
    return {"schema_version": 1, "engine_version": ENGINE_VERSION, "feature_version": FEATURE_VERSION,
            "return_basis": "price-only", "decision_origin": (source or {}).get("decision_origin", config.llm_mode) if agent_strategy else "baseline",
            "dataset": dataset.metadata, "decisions": decisions, "trades": trades,
            "model_ids": sorted({c.get("actual_model_id", c["model_id"]) for c in calls}) or ([config.model_id] if agent_strategy and config.model_id else []),
            "environment": {"python": sys.version.split()[0],
                            "packages": {p: importlib.metadata.version(p) for p in ("pydantic", "pandas", "numpy", "langgraph", "exchange-calendars")},
                            "uv_lock_sha256": sha256((ROOT / "uv.lock").read_bytes())},
            "equity": equity, "metrics": metrics, "first_execution_session": next((d["execution_session"] for d in decisions if d["status"] == "APPROVED"), None)}
