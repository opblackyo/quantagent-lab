"""Write one synthetic server profile for the offline composition server.

The file carries source hashes from this checkout. It is not an owner profile
and it contains no provider key.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.data import ROOT, DatasetRegistry
from app.research_contracts import ServerResearchProfile
from app.research_runtime import _source_context


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: write_offline_live_profile.py OUTPUT")
    at = datetime.now(timezone.utc).replace(microsecond=0)
    context = _source_context(at)
    dataset = DatasetRegistry([ROOT / "data" / "sample"]).load("synthetic-spy-v1")
    profile = ServerResearchProfile(
        profile_id="synthetic-live", revision=1, provider="kuaiapi", model_id="synthetic-model-v1",
        identity_scope="gateway-reported-label-only", dataset_id=dataset.metadata["id"], dataset_hash=dataset.hash,
        dataset_kind="synthetic", allowed_start="2023-01-01", allowed_end="2023-01-31", max_budget_usd="1",
        max_attempts=32, attempts_per_role=1, input_usd_per_million="0.2", output_usd_per_million="0.8",
        cache_read_usd_per_million="0.01", cache_write_usd_per_million="0.3",
        settings={"max_output_tokens": 1024}, valid_from=at - timedelta(hours=1), expires_at=at + timedelta(days=7),
        expected_source_hash=context.source_hash, expected_uv_lock_hash=context.uv_lock_hash,
        expected_frontend_lock_hash=context.frontend_lock_hash,
        private_evidence=({"kind": "pricing", "reference_id": "synthetic-live-price-ref", "sha256": "e" * 64},
                          {"kind": "binding", "reference_id": "synthetic-live-binding-ref", "sha256": "f" * 64}))
    payload = {"profile": profile.model_dump(mode="json"), "credential": "synthetic-offline-secret-marker"}
    Path(sys.argv[1]).write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    main()
