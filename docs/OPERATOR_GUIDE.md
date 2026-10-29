# Local operator guide

The supported demonstration is **synthetic prices + Mock/Replay**, without a key. This is research/education software, with no broker orders or performance promise. The alpha scope is closed; [live acceptance remains an explicit exception](RESEARCH_CLOSEOUT.md).

## Install and start

Use Python 3.13, uv, Node.js 22 and npm. From the repository root:

```sh
uv sync --locked
npm --prefix frontend ci --no-audit --no-fund
node tools/dev.mjs --backend-port 18411 --frontend-port 13411
```

Choose available owned ports; leave existing services running. Open `http://127.0.0.1:13411`. The launcher binds loopback, sets the matching frontend proxy/origin and stops its own children on Ctrl+C. It refuses occupied ports.

In 建立實驗 select `synthetic-spy-v1`, Mock and Single-Agent or Multi-Agent. Inspect the saved run, Agent decisions, deterministic risk and simulated ledger. The source fixture is not historical SPY. In 實驗紀錄, select a completed run and press 重播這份實驗; Replay uses the saved decisions without a provider request. Export JSON locally. Databases/exports stay under ignored `artifacts/`; a ZIP does not include a prepopulated private study.

With the service running, the existing four-strategy offline demonstration is:

```sh
uv run --no-sync python tools/run_research.py --dataset synthetic-spy-v1 --start 2024-01-01 --end 2024-12-31
```

## Verify without real credentials

```sh
uv run --no-sync pytest -q
node --test tools/dev_options.test.mjs
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Stop other Next dev instances in this checkout before browser verification; do not stop unrelated services. Install the matching Chromium runtime once with `npx playwright install chromium` from `frontend`, then run `npm --prefix frontend run test:e2e`. The default browser config uses owned test ports 8001/3001 and synthetic Mock data. Study and operator fixture suites use dynamically selected loopback ports:

```sh
node tools/run_research_fixture_e2e.mjs
uv run --no-sync python tools/run_live_operator.py e2e-test
```

For the Node fixture wrapper on non-Windows systems, set `QUANTAGENT_PYTHON` to the installed `.venv/bin/python` interpreter; its existing default is the Windows `.venv/Scripts/python.exe` path. The operator e2e command injects offline MockTransport and a synthetic credential. Its name does not mean paid live acceptance. Neither command should receive a real key or provider environment. Backend, browser and launcher tests are separate scopes.

## Experimental real operator boundary

`uv run --no-sync python tools/run_live_operator.py --help` lists freeze, approve-bundle, check-bundle, serve and offline e2e-test. Real use requires independently verified tariff units, exact key/group binding, cache/failure fees, an immutable dataset/calendar and source/prompt/schema/lock hashes, a bounded validity window and explicit spending authorization. A browser flag or proposed freeze never grants money. Keep the private key file and approval bundle server-only and untracked; never place secrets in a URL or frontend environment. No real configuration or approval is shipped.

Do not enable synthetic transport for a real run. Admit one frozen scope, reserve before dispatch/key load, and use the same study/idempotency journal after a response is lost. An uncertain send retains its full reserve; do not create a replacement study or retry. This release's real settlement path intentionally pauses without independent gateway charge evidence. The fixture reconcile button is not an actual billing import. Remain on Mock/Replay unless all independent real-money and accounting prerequisites are met; this release does not claim they were met.

For an actual saved source, Replay must be verified to make zero provider, key and billing calls and compared through the existing full-precision semantic result. A fixture replay does not stand in for real-model acceptance. Read [limits](RESEARCH_CLOSEOUT.md), [methodology](RESEARCH_METHODOLOGY.md), [MIT](../LICENSE) and [third-party notices](../THIRD_PARTY_NOTICES.md).
