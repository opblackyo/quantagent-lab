import json
import os
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .data import ROOT, canonical_hash
from .models import ExperimentConfig


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = None
        self.keeper = None
        self.database_lock = threading.RLock()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(".lock").open("a+b")
        self.lock.seek(0, 2)
        if self.lock.tell() == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise RuntimeError("another QuantAgent worker owns this database") from None
        try:
            # Keep WAL open across short worker-thread connections. Closing the
            # last connection after each event forces a checkpoint every time.
            self.keeper = sqlite3.connect(self.path, check_same_thread=False)
            self.keeper.execute("PRAGMA journal_mode=WAL").fetchone()
            with self.connect() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, progress INTEGER NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error_message TEXT, code_commit TEXT, code_dirty INTEGER, source_hash TEXT, result_json TEXT, idempotency_key TEXT UNIQUE)")
                conn.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, event_type TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id,event_type,event_id))")
                conn.execute("CREATE INDEX IF NOT EXISTS events_run ON events(run_id,sequence)")
                conn.execute("UPDATE runs SET status='FAILED', error_message='SERVICE_RESTARTED', finished_at=? WHERE status IN ('PENDING','RUNNING')", (now(),))
        except BaseException:
            self.close()
            raise

    @contextmanager
    def connect(self):
        # Serialize active transactions/checkpoints across worker/API threads.
        # This also avoids the WAL-reset race on older bundled SQLite builds.
        with self.database_lock:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def close(self):
        if self.keeper:
            self.keeper.close()
            self.keeper = None
        if self.lock:
            self.lock.close()
            self.lock = None

    def record_event(self, run_id, kind, payload):
        keys = {"agent_call":"call_id", "decision":"decision_id", "trade":"trade_id", "equity":"session_date"}
        event_id = str(payload[keys[kind]])
        stamp = now()
        serialized = json.dumps(payload, allow_nan=False)
        with self.connect() as conn:
            status = conn.execute("SELECT status FROM runs WHERE run_id=?",(str(run_id),)).fetchone()
            if not status or status["status"] not in ("PENDING","RUNNING"):
                raise ValueError("settled experiment evidence is immutable")
            conn.execute("INSERT INTO events(run_id,event_type,event_id,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,event_type,event_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                         (str(run_id),kind,event_id,serialized,stamp,stamp))

    def events(self, run_id):
        run = self.get(run_id)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence",(str(run_id),)).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            item["payload"]=json.loads(item.pop("payload_json"))
            if run["status"] == "FAILED" and item["payload"].get("status") in ("PENDING","RUNNING"):
                item["payload"].update(status="INTERRUPTED",error_code=run["error_message"])
            result.append(item)
        return result

    def create(self, config: ExperimentConfig, idempotency_key=None):
        cfg = config.model_dump(mode="json")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                row = conn.execute("SELECT * FROM runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row:
                    if json.loads(row["config_json"]) != cfg:
                        raise ValueError("idempotency key already used with different settings")
                    return row["run_id"], False
            run_id = str(uuid4())
            try:
                commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
                dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
            except (OSError, subprocess.SubprocessError):
                commit, dirty = None, True
            source_hash = canonical_hash({str(p.relative_to(ROOT)): p.read_bytes().hex() for p in sorted((ROOT / "backend/app").glob("*.py"))})
            conn.execute("INSERT INTO runs (run_id,status,progress,config_json,created_at,code_commit,code_dirty,source_hash,idempotency_key) VALUES (?,'PENDING',0,?,?,?,?,?,?)",
                         (run_id, json.dumps(cfg, allow_nan=False), now(), commit, int(dirty), source_hash, idempotency_key))
            return run_id, True

    def update(self, run_id, *, status=None, progress=None, result=None, error=None):
        fields, values = [], []
        for key, value in (("status", status), ("progress", progress), ("result_json", json.dumps(result, allow_nan=False) if result is not None else None), ("error_message", error)):
            if value is not None:
                fields.append(key + "=?")
                values.append(value)
        if status == "RUNNING":
            fields.append("started_at=?")
            values.append(now())
        if status in ("COMPLETED", "FAILED"):
            fields.append("finished_at=?")
            values.append(now())
        with self.connect() as conn:
            conn.execute("UPDATE runs SET " + ",".join(fields) + " WHERE run_id=? AND status IN ('PENDING','RUNNING')", [*values, run_id])

    @staticmethod
    def decode(row, include_result=False):
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        saved = result.pop("result_json")
        saved = json.loads(saved) if saved else None
        result["metrics"] = saved.get("metrics") if saved else None
        result["dataset"] = saved.get("dataset") if saved else None
        result["decision_origin"] = saved.get("decision_origin") if saved else None
        result["code_dirty"] = bool(result["code_dirty"])
        result.pop("idempotency_key")
        if include_result:
            result["result"] = saved
        return result

    def get(self, run_id, include_result=False):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
        if not row:
            raise KeyError("unknown run")
        return self.decode(row, include_result)

    def list(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 100").fetchall()
        return [self.decode(row) for row in rows]
