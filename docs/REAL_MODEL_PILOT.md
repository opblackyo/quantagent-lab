# Private real-model pilot

The initial alpha source is public. This development adds a private pilot path;
it does not authorize publishing new code, licensed market data or results.
Live pilot completion is recorded in private receipts. Execution approvals, selected
models/groups, keys and observed charges belong in private records. Check those
records before requesting choices or billing evidence that already exists.

The direct runner uses one dated OpenAI model snapshot with identical settings for
Single and Multi. [KuaiAPI integration](KUAIAPI_INTEGRATION.md) adds a separately
configured gateway route with explicit group, price units and label-only
identity limitations. Execution requires the applicable private authorization
and satisfaction of its billing/group conditions.
Baselines never construct or call a provider. The default
period is 2023-01-01 through 2023-01-31, outside the 2025 project holdout.
It requires the selected historical raw SPY dataset's exact byte hash and
validates the full XNYS session calendar and warmup. It refuses synthetic data.

Private configuration may include `timeout_policy` with finite positive
`connect`, `read`, `write` and `pool` seconds (each at most 300; defaults 30).
This server-side policy is frozen in new configuration/manifests and attempt
traces, separate from the model payload. HTTPX read/write limits govern chunks,
not a total wall-clock deadline. Timeout categories and elapsed attempt duration
are recorded without exception messages or URLs. Timeouts are not automatically
retried and retain unknown-charge reservations. Changing policy requires a new
reviewed experiment; do not mutate or resume an old frozen study to apply it.

## Private configuration and dry-run

Run from the checkout root after `uv sync --locked`:

```sh
uv run --no-sync python tools/run_pilot.py --init
```

Edit the ignored `artifacts/pilot/config.json`. Select an exact dated model,
positive input/output USD per million tokens, an explicit **total pilot** USD
limit, the official price source URL and the date prices were verified. The
template deliberately leaves these choices null. `max_calls` includes retries;
the default ceiling is 32 attempts, with at most two attempts per role.
`max_output_tokens`, reasoning effort and optional temperature are frozen too.
Select only settings supported by the chosen model; there is no paid discovery
call or automatic fallback to another model.

For example, the [official GPT-5.4 Mini page](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
was checked on 2026-10-24: it lists snapshot `gpt-5.4-mini-2026-03-17`, standard
input/output prices of USD 0.75/4.50 per million tokens, and reasoning effort
`none`. This is a reviewable option, **not a configured or authorized default**.
With a 32,768-input-token reservation and 2,048 output-token ceiling, 16 nominal
attempts reserve USD 0.540672; 32 attempts reserve USD 1.081344. Actual usage is
usually smaller; neither amount is an invoice. Cached input is conservatively
priced as uncached. No tools, regional endpoints, Batch or priority tier are used.

```sh
uv run --no-sync python tools/run_pilot.py
```

This command makes **zero provider calls**. It saves a private dry-run manifest
containing numeric input hashes, exact decision/next-open sessions, warmup,
prompts/schemas/hashes, data hash, source fingerprint, engine/feature versions,
model/settings, price assumptions, missing configuration and call/cost bounds.
January 2023 has four eligible weeks: Jan 6→9, Jan 13→17 (MLK closure), Jan 20→23
and Jan 27→30. Single needs four successful calls; Multi needs twelve. There is
no separate smoke request: the first Single decision is already part of the pilot.

A manifest file is immutable. If configuration/source changes before execution,
use a new `--manifest artifacts/pilot/dry-run-v2.json`; review it before execution.
Do not create a new pilot to reset a consumed budget without new spending approval.
After any paid attempt, price corrections must use the explicit anchored migration
in `KUAIAPI_INTEGRATION.md`; never change the pilot ID or repeat its smoke call.

Configure a **separate OpenAI API key**, never a ChatGPT/Business subscription
credential, using backend environment `OPENAI_API_KEY` or hidden terminal entry:

```sh
uv run --no-sync python tools/run_pilot.py --set-key
```

The latter writes only the ignored `.env.pilot` file and refuses to overwrite an
existing file. It never prints the key. Do not paste keys into chat. The runner
reads a key file only when explicitly named with `--key-file`; it never evaluates
shell code or searches profiles/projects. The application still does not load
general `.env` files automatically. Keep local file access restricted to the owner.

## Execution and review

After the model, price assumptions and total spending limit are approved:

```sh
uv run --no-sync python tools/run_pilot.py --execute --key-file .env.pilot
```

The complete dry-run manifest must exactly match current data/config/prompts/
source. A persistent SQLite budget is shared by Single and Multi, including every
retry. Reservations commit before a potentially billable request. Valid usage
settles the reservation to a token-price estimate; timeouts, network/HTTP errors,
missing usage and interrupted calls retain the full unknown reservation. An
unexpected reported charge above the cap is recorded and stops further calls;
client estimates cannot control a provider invoice. Invoiced actual cost is null.

Budget exhaustion or invalid output/model identity skips new trades and keeps
holdings. Valid zero weight still liquidates. No performance-driven retuning or
silent reruns occur. A completed run is reused idempotently. An interrupted run
requires reviewing the retained diagnostic events; it is never silently retried.
Ctrl+C stops the runner and retains reservations, raw/validated responses and
partial diagnostic evidence in the ignored database. It is not a distributed queue.

The runner saves four source runs, their zero-call Replay runs, a private JSON
report and semantic equality evidence. Comparison covers decision/skip reasons,
validated roles, risk weights/status, trades, full daily equity and financial
metrics. IDs/timestamps differ; Replay tokens/costs are zero. Failed retry calls
remain visible in Replay traces. Replay revalidates saved input/prompt/output
and reruns deterministic risk, fills and accounting.

For useful offline progress without model configuration:

```sh
uv run --no-sync python tools/run_pilot.py --baselines --output artifacts/pilot/baselines.json
```

This runs only Buy & Hold and MA20/60, plus their Replay checks. It cannot be
reported as a completed real-model four-strategy study.

Stop the runner before opening the same database in the existing API/UI:

```powershell
$env:QUANTAGENT_DB_PATH = (Resolve-Path artifacts/pilot/research.sqlite3).Path
node tools/dev.mjs
```

Open the loopback workstation, select each run, inspect Agent decisions and raw
JSON (including reported model label/provider attribution, not proof of gateway upstream identity), compare the four strategies in the same
research group, inspect ledger/estimated/unknown costs and export JSON. Overview
shows the pilot's shared budget via `GET /api/v1/pilots/{manifest_hash}/budget`.
The UI/API exclude other pilot groups from comparisons. Raw responses, screenshots,
exports, the budget database, licensed data and reports stay local and ignored.

Report return/drawdown/Sharpe/trades/exposure alongside approved/skipped/rejected
decisions and reasons, attempts, tokens, latency, usage estimates and unknown
reservations. Price-only returns exclude dividends/cash interest; historical
model knowledge and unequal inference budgets remain confounders. A short pilot
proves integration, not investment effectiveness. Positive returns are not required.

## Offline verification

```sh
uv run --no-sync pytest -q
python tools/audit_public.py --history --approved-name opblackyo --approved-noreply 68807943+opblackyo@users.noreply.github.com
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

Tests use synthetic fixtures and HTTP mocks. They must never load private keys
or require licensed market data. No paid call runs in CI. Local commits use the
approved public noreply; new research commits must not be pushed automatically.
