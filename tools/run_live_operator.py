"""Operator CLI entry for freezing manifests, checking approved bundles, and serving the live operator runtime.

This CLI never reads a provider API key during freeze, bundle assembly, bundle validation, or server startup.
Ports 3002 and 8002 are permanently refused.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.data import canonical_hash  # noqa: E402
from app.live_composition import (  # noqa: E402
    build_operator_approval_bundle,
    freeze_operator_manifest,
    load_operator_bindings,
)
from app.research_runtime import build_fixture_runtime  # noqa: E402

FORBIDDEN_PORTS = frozenset({3002, 8002})


def _free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
            if port not in FORBIDDEN_PORTS and 1024 <= port <= 65535:
                return port


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("OPENAI_", "KUAIAPI_", "LLM_")) or key == "QUANTAGENT_DB_PATH":
            env.pop(key, None)
    env.update(extra)
    return env


def _wait_http(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last = "not-ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    return
                last = str(resp.status)
        except Exception as exc:
            last = type(exc).__name__
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url} ({last})")


def _cmd_freeze(args: argparse.Namespace) -> int:
    freeze = freeze_operator_manifest(args.binding, protocol=args.protocol)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(freeze, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "approval_status": freeze["approval_status"],
        "funding_approved": freeze["funding_approved"],
        "study_protocol": freeze["study_protocol"],
        "manifest_hash": freeze["manifest_hash"],
        "profile_sha256": freeze["profile_sha256"],
        "reservation_usd_per_attempt": freeze["view"]["estimate"]["reservation_usd_per_attempt"],
        "total_nominal_calls": freeze["view"]["estimate"]["total_nominal_calls"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def _cmd_approve_bundle(args: argparse.Namespace) -> int:
    items: list[tuple[dict, str]] = []
    for path_str in args.freeze:
        freeze = json.loads(Path(path_str).read_text(encoding="utf-8"))
        items.append((freeze, args.key_file))
    bundle = build_operator_approval_bundle(items)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "kind": bundle["kind"],
        "bindings": len(bundle["bindings"]),
        "manifest_hashes": [b["approval"]["manifest_hash"] for b in bundle["bindings"]],
        "key_reads": 0,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def _cmd_check_bundle(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runtime = build_fixture_runtime(Path(tmp) / "check-bundle.sqlite3")
        try:
            bindings = load_operator_bindings(args.bundle, runtime=runtime)
            assert all(b.loader.calls == [] and b.loader.loaded_secrets == [] for b in bindings)
            summary = {
                "checked": True,
                "key_reads": 0,
                "bindings": [b.public_scope() for b in bindings],
            }
        finally:
            runtime.close()
    print(json.dumps(summary, sort_keys=True))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.port in FORBIDDEN_PORTS or not (1024 <= args.port <= 65535):
        raise ValueError("REFUSED_OPERATOR_PORT")
    if args.host != "127.0.0.1":
        raise ValueError("LOOPBACK_HOST_REQUIRED")
    if args.web_port is not None:
        if args.web_port in FORBIDDEN_PORTS or not (1024 <= args.web_port <= 65535) or args.web_port == args.port:
            raise ValueError("REFUSED_OPERATOR_PORT")
    origins = f"http://127.0.0.1:{args.web_port}" if args.web_port is not None else args.origins
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "KUAIAPI_", "LLM_")) or key == "QUANTAGENT_DB_PATH":
            os.environ.pop(key, None)
    os.environ["QUANTAGENT_FIXTURE_DB"] = str(Path(args.db).resolve())
    os.environ["QUANTAGENT_LIVE_OPERATOR_FILE"] = str(Path(args.bundle).resolve())
    os.environ["QUANTAGENT_LOCAL_ORIGINS"] = origins

    web_proc = None
    try:
        if args.web_port is not None:
            next_bin = ROOT / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next"
            web_proc = subprocess.Popen(
                ["node", str(next_bin), "dev", "--hostname", "127.0.0.1", "--port", str(args.web_port)],
                cwd=ROOT / "frontend",
                env=_clean_env({"BACKEND_URL": f"http://127.0.0.1:{args.port}"}),
            )
        uvicorn.run(
            "app.research_runtime:create_operator_live_from_environ",
            factory=True,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
        )
    finally:
        if web_proc is not None and web_proc.poll() is None:
            web_proc.terminate()
    return 0


def _cmd_e2e_test(args: argparse.Namespace) -> int:
    api_port = _free_port()
    web_port = _free_port()
    while web_port == api_port:
        web_port = _free_port()
    origin = f"http://127.0.0.1:{web_port}"
    next_bin = ROOT / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next"
    playwright_cli = ROOT / "frontend" / "node_modules" / "@playwright" / "test" / "cli.js"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_dir = Path(tmp)
        key_file = tmp_dir / "synthetic-operator.key"
        key_file.write_text("KUAIAPI_API_KEY=synthetic-offline-secret-marker\n", encoding="utf-8")

        now = datetime.now(timezone.utc).replace(microsecond=0)
        valid_from = (now - timedelta(hours=1)).isoformat()
        expires_at = (now + timedelta(days=7)).isoformat()
        as_of_date = (now - timedelta(hours=1)).date().isoformat()

        def make_binding(group_ref: str, cohort_ref: str) -> Path:
            doc = {
                "kind": "production-shaped-usage-v1",
                "profile_id": "synthetic-live",
                "max_attempts": 3,
                "provider": "kuaiapi",
                "model_id": "synthetic-model-v1",
                "endpoint": "https://kuaiapi.net/v1/responses",
                "pricing_unit": "effective-usd-per-million",
                "multiplier": "1",
                "tariff_id": "synthetic-live-price-ref",
                "group_ref": group_ref,
                "cohort_ref": cohort_ref,
                "requested_budget_usd": "1",
                "input_usd_per_million": "0.2",
                "output_usd_per_million": "0.8",
                "cache_read_usd_per_million": "0.01",
                "cache_write_usd_per_million": "0.3",
                "binding_ref": "synthetic-live-binding-ref",
                "binding_sha256": "f" * 64,
                "cache_status": "observed-consistent",
                "delayed": False,
                "external_same_key_requests": 0,
                "valid_from": valid_from,
                "expires_at": expires_at,
                "as_of_date": as_of_date,
                "timezone": "UTC",
                "day_count": 1,
                "evidence_source": "operator-verified-binding",
            }
            clean = {
                k: v for k, v in doc.items()
                if k not in {"profile_id", "cohort_ref", "requested_budget_usd", "max_attempts", "provenance"}
            }
            doc["provenance"] = {"method": "operator-verified-server-binding-v1", "sha256": canonical_hash(clean)}
            path = tmp_dir / f"binding-{group_ref}.json"
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return path

        freeze_a = freeze_operator_manifest(
            make_binding("synthetic-live-a-group", "synthetic-live-a-cohort"),
            protocol="acceptance-stage-a-v1",
        )
        freeze_b = freeze_operator_manifest(
            make_binding("synthetic-live-b-group", "synthetic-live-b-cohort"),
            protocol="acceptance-stage-b-v1",
        )
        bundle = build_operator_approval_bundle([(freeze_a, key_file), (freeze_b, key_file)])
        bundle_path = tmp_dir / "operator-bundle.json"
        bundle_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        db_path = tmp_dir / "operator-e2e.sqlite3"

        api_proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "app.research_runtime:create_operator_live_from_environ",
                "--factory", "--host", "127.0.0.1", "--port", str(api_port), "--app-dir", "backend",
            ],
            cwd=ROOT,
            env=_clean_env({
                "QUANTAGENT_FIXTURE_DB": str(db_path),
                "QUANTAGENT_LIVE_OPERATOR_FILE": str(bundle_path),
                "QUANTAGENT_LOCAL_ORIGINS": origin,
                "QUANTAGENT_OPERATOR_SYNTHETIC_TRANSPORT": "offline-mock-v1",
            }),
        )
        web_proc = subprocess.Popen(
            ["node", str(next_bin), "dev", "--hostname", "127.0.0.1", "--port", str(web_port)],
            cwd=ROOT / "frontend",
            env=_clean_env({"BACKEND_URL": f"http://127.0.0.1:{api_port}"}),
        )
        try:
            _wait_http(f"http://127.0.0.1:{api_port}/api/v1/research/readiness", 30.0)
            _wait_http(f"{origin}/research", 90.0)
            res = subprocess.run(
                ["node", str(playwright_cli), "test", "-c", "playwright.live.config.ts"],
                cwd=ROOT / "frontend",
                env=_clean_env({"LIVE_WEB_URL": origin}),
            )
            return res.returncode
        finally:
            for proc in (web_proc, api_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuantAgent Lab operator live freeze and startup entry")
    sub = parser.add_subparsers(dest="command", required=True)

    p_freeze = sub.add_parser("freeze", help="Freeze a deterministic PROPOSED_NOT_APPROVED manifest from a tariff binding")
    p_freeze.add_argument("--binding", required=True, help="Path to private operator tariff binding JSON")
    p_freeze.add_argument("--output", required=True, help="Output path for freeze manifest JSON")
    p_freeze.add_argument(
        "--protocol",
        default="acceptance-stage-a-v1",
        choices=("acceptance-stage-a-v1", "acceptance-stage-b-v1"),
    )
    p_freeze.set_defaults(func=_cmd_freeze)

    p_approve = sub.add_parser("approve-bundle", help="Assemble a server-approved operator bundle after Owner funding confirmation")
    p_approve.add_argument("--freeze", action="append", required=True, help="Path(s) to frozen manifest JSON")
    p_approve.add_argument("--key-file", required=True, help="Path to private provider key file")
    p_approve.add_argument("--output", required=True, help="Output path for operator approval bundle JSON")
    p_approve.set_defaults(func=_cmd_approve_bundle)

    p_check = sub.add_parser("check-bundle", help="Validate an operator bundle offline with zero key reads")
    p_check.add_argument("--bundle", required=True, help="Path to private operator approval bundle JSON")
    p_check.set_defaults(func=_cmd_check_bundle)

    p_serve = sub.add_parser("serve", help="Run the operator live ASGI service (and optional Next.js UI) on owned loopback ports")
    p_serve.add_argument("--db", required=True, help="Fresh non-existent SQLite path for the study store")
    p_serve.add_argument("--bundle", required=True, help="Path to private operator approval bundle JSON")
    p_serve.add_argument("--port", type=int, default=18421, help="Owned loopback API port (never 3002 or 8002)")
    p_serve.add_argument("--web-port", type=int, default=None, help="Optional owned loopback Next.js UI port (never 3002 or 8002)")
    p_serve.add_argument("--host", default="127.0.0.1", help="Loopback host (must be 127.0.0.1)")
    p_serve.add_argument("--origins", default="http://127.0.0.1:4311", help="Comma-separated loopback UI origins")
    p_serve.set_defaults(func=_cmd_serve)

    p_e2e = sub.add_parser("e2e-test", help="Run the Playwright live UI test against create_operator_live_from_environ with synthetic transport")
    p_e2e.set_defaults(func=_cmd_e2e_test)

    parsed = parser.parse_args(argv)
    return parsed.func(parsed)


if __name__ == "__main__":
    raise SystemExit(main())
