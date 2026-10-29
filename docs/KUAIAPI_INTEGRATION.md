# KuaiAPI private pilot integration

Status: implementation supports an explicitly authorized, bounded first-call probe.
Owner model/group/budget decisions and actual usage records stay private. A live
request still requires intentional project-key configuration and a complete frozen
manifest. Local commits and private artifacts must not be published automatically.

## Public evidence checked 2026-10-25

KuaiAPI identifies itself as a third-party gateway with routing/failover; its
compatibility claims do not independently establish upstream model identity,
weights, version, quality, official-provider equivalence or privacy controls.
The implemented path is **Responses only**, using numeric inputs and the existing
strict JSON schema. There is no Anthropic/native client, free-form debate,
Chat Completions fallback, JSON repair or schema relaxation.

| Source | Observation and limit |
| --- | --- |
| [Gateway quick facts](https://kuaiapi.net/docs/en/) | Lists `/v1/responses`, base `https://kuaiapi.net/v1`, and 6.8 balance units per USD. This does not prove per-model schema support. |
| [Service overview](https://kuaiapi.net/docs/en/overview.html) | Describes automatic failover/load balancing. The upstream route/version is not independently observable here. |
| [Key documentation](https://kuaiapi.net/docs/guide/api-keys.html) | Keys bind to a group. The runner records the owner's declared binding; it does not query accounts or send an invented group-selection header. |
| [Usage documentation](https://kuaiapi.net/docs/guide/usage.html) | Billing depends on tokens, price and group multiplier; cached pricing can vary. Usage-record charges are authoritative. |
| [Model Plaza](https://kuaiapi.net/model-plaza/) | Public live configuration, group-specific prices, token/per-call distinctions. The page fetches `data/plaza.json`. Names/prices/availability can change. |

The public page source uses `$` as a display symbol, contains separate group
display multipliers, and differs in terminology from the English conversion
documentation. The selected public JSON explicitly uses token billing and a null request fee;
the renderer maps token capability to the English Per call badge. This explains
the label/prose inconsistency without establishing a fixed per-request fee.
The documentation conversion is 6.8 native units/USD; the separate JavaScript
RATE 7.1 is used only in the rounded percentage badge. Do not apply it as account
conversion or multiply the displayed discount/group adjustment again.
Anonymous HTML/JSON snapshots and SHA-256 evidence are saved privately. Public
labels such as `gpt-5.6-luna` are configuration candidates only, not verified
dated upstream snapshots. Group display renames can also differ from raw data.
The public catalog does not establish which key group the owner actually has.

Runtime correction: the public key-usage UI calls `GET /v1/usage` with the project
key as Bearer and date/timezone filters. Its explicit `unit=USD` and `actual_cost`
must be treated as USD, without dividing by the documentation's 6.8 conversion.
The public plaza source also references `GET /v1/sub2api/billing`; the observed
multiplier is separate from the pre-multiplier usage `cost`. The two routes do
not expose a per-request ID or a group name. A compatible multiplier alone does
not verify the exact group. Sanitized private evidence is authoritative for the
observed deduction; public display conversion assumptions are not runtime tariffs.

## Configuration and isolated credentials

OpenAI direct remains supported with its dated snapshot requirement, fixed
`https://api.openai.com/v1/responses` endpoint and `OPENAI_API_KEY`. KuaiAPI uses
only `https://kuaiapi.net/v1/responses` and `KUAIAPI_API_KEY`. No arbitrary base
URL is accepted. Redirect following is disabled, including injected HTTP clients.
Neither provider can fall back to the other's key. Known application keys are
redacted if a successful or malformed response echoes them; exception values
are never printed by the pilot CLI. No auth profile or subscription token is read.

Create a separate ignored template; the existing direct pilot config is preserved:

```sh
uv run --no-sync python tools/run_pilot.py --init --provider kuaiapi --config artifacts/kuaiapi/config.json
```

Complete these fields only after selecting/reviewing the intended application route:

- `provider="kuaiapi"`, exact gateway `model_id`, and exact owner-declared `provider_group`.
- For historical native-unit assumptions, `pricing_unit="balance_units_per_million"`
  leaves direct USD fields null. Do not reuse a native interpretation after
  runtime USD evidence contradicts it.
- For a verified runtime tariff use `pricing_unit="runtime_usd_per_million"`,
  effective `input_usd_per_million`, `output_usd_per_million`,
  `cache_write_usd_per_million`, `cache_read_usd_per_million`,
  `runtime_rate_multiplier` (attribution only), and `runtime_pricing_verified=true`.
  All native fields/conversion must be null. Missing cache rates or unverified
  runtime pricing blocks provider construction/execution. A reviewed published
  cache tariff combined with an observed key multiplier can establish a budget
  estimate without deliberately making a cache-billed test call. Label published
  derivations separately from measured deductions; do not generalize a lone
  zero-cache deduction without that supporting tariff evidence.
- Explicit effective-group input/output **ceiling** rates in balance units per
  million tokens, covering applicable cache-write/read, peak/context tiers and
  multipliers. Apply each multiplier once before entering the rates; the runner
  applies no second group multiplier. Per-call/image/audio pricing is unsupported.
- `balance_units_per_usd`, current `price_source` on the gateway's official site,
  `price_verified_on` and SHA-256 of the actual reviewed price/conversion evidence.
  Do not infer conversion from the `$` glyph or assume upstream direct prices.
- `gateway_routing_acknowledged=true` acknowledges label-only identity/failover
  limits; it is not spending authorization or proof of routing.
- An explicit total USD budget and bounded attempts/output tokens/settings.

No dollar cap, group, conversion or model is silently selected. Optional
`cache_write_balance_units_per_million` is added in full to the input rate for
reservations and usage-cost ceilings. Cache-read is recorded, with no discount
assumed. This intentionally covers the case where base input and cache creation
are both charged; verified actual deductions remain separate. Normalized rates
for that historical mode are `native_rate / balance_units_per_usd`, **not multiplied** by the conversion.
Runtime USD mode applies neither a conversion nor another multiplier; effective
input plus full additive cache-write cost is its conservative reservation ceiling.
For an arithmetic fixture only, 6.8/13.6 native units per million at 6.8 units/USD
means USD 1/2 per million. These are test numbers, not a gateway quote.

Hidden local setup is available when a separate application key is intentionally
provided. Do not paste a key into chat or reuse coding/subscription credentials:

```sh
uv run --no-sync python tools/run_pilot.py --set-key --provider kuaiapi
```

This creates only ignored `.env.pilot.kuaiapi`, with `KUAIAPI_API_KEY=...`, and
refuses overwrites. Direct setup retains `.env.pilot`. Explicit `--key-file`
loading rejects the wrong provider's assignment. General `.env` files are not
automatically loaded. The KuaiAPI application route is restricted to the bounded
pilot runner; generic API experiment creation cannot bypass its shared budget.

## Dry-run, attribution and billing

```sh
uv run --no-sync python tools/run_pilot.py --config artifacts/kuaiapi/config.json --manifest artifacts/kuaiapi/dry-run.json
```

Dry-run is zero-call and saves all missing settings, native/normalized prices,
provider/endpoint/group/model/settings, schema/prompt/input/data/source hashes and
calendar. The existing January 2023 immutable raw SPY scope is preserved: four
eligible weeks, 4 Single + 12 Multi nominal calls, up to 32 attempts with one
retry per role, zero separate smoke calls. Both baselines make zero provider calls.

After independent model/pricing/total-budget approval, the reviewed local source
must be committed and the complete manifest refrozen under a new filename.
`--execute --key-file .env.pilot.kuaiapi` would then run that exact manifest; it
must only be executed when covered by the private conditional authorization.

Every attempt reserves against the same persistent pilot budget before transport.
429/5xx may retry within the configured allowance; timeouts are never automatically resent because billing may already have occurred. 400/schema errors, model-label mismatch, invalid
JSON, duplicate keys, nonfinite values and contract failures skip without weaker
fallbacks. Network/HTTP/missing or inconsistent usage retain unknown reservations.
Canonical `input_tokens/output_tokens` and legacy `prompt_tokens/completion_tokens`
are supported; contradictory aliases, booleans, negatives and inconsistent totals
are unknown. Cached/reasoning details are subsets and are not added twice.
Usage-based estimates use the frozen normalized ceiling rates. Undocumented
gateway quota/cost fields are not interpreted as USD. Invoiced actual cost remains
null until independently reconciled; the client cannot control a provider invoice.

Trace/cache/manifest attribution includes provider, fixed endpoint, declared key
group, requested and reported model labels, settings and native price evidence.
Same labels do **not** prove the same upstream route. Configuration mutation stops
new calls. Comparisons require the same research group, provider attribution,
model labels and settings. The UI explicitly labels gateway upstream identity as
unverified; raw/exported evidence retains this limitation.

Replay constructs no provider, preserves failed/retried calls at zero new cost,
checks provider attribution, inputs, prompts and validated output, and reruns
deterministic risk/ledger code. Invalid/failed decisions retain holdings; an
approved target zero still liquidates. No trading-engine or feature formulas change.

## Verification and remaining live evidence

Offline tests use synthetic data and HTTP fixtures, including endpoint/key
isolation, redirect refusal, strict output failures, native-unit normalization,
shared retries/unknown charges, group attribution and zero-call Replay. Browser
fixtures stay labelled Mock. No real key, licensed market data or paid request is
required by tests or CI. Reproduction uses the existing backend/typecheck/build/
E2E commands in `REAL_MODEL_PILOT.md`.

Before continuing, consult the existing private authorization/probe/charge records.
Required conditions include explicit model/group/cap, verified runtime/cache
prices, intentional project key, strict schema/usage validation, independently
verified key-group binding, and actual charge reconciliation. A completed first
probe must be reused. Full-run Replay/UI review follows completion, not preparation.
This integration makes no investment-effectiveness
or official-model-equivalence claim.


## First-call checkpoint and continuation

For KuaiAPI, `--execute` makes at most one request for the **first Single decision**,
then returns `PAUSED_FOR_FIRST_CALL_RECONCILIATION`. That request is part of Single's
four nominal calls, not an extra smoke. There is no automatic retry at this gate.
A durable probe record and shared budget reservation precede transport; rerunning
or restarting cannot repeat an already recorded or uncertain probe. An interrupted
or invalid/unknown-usage probe stays paused for review.

To continue, provide an explicitly named ignored `--probe-reconciliation` JSON
record with `manifest_hash`, `call_id`, matching `provider_response_id`, selected
`provider_group` and `model_id`, independently checked `actual_charge_usd`, evidence
SHA-256, source (`owner-provided-private-record` or
`documented-authorized-readonly-billing`), true `key_group_binding_checked` /
`strict_output_and_usage_checked` flags, separate `key_group_binding_evidence_sha256`
and `key_group_binding_source` (`owner-verified-key-settings` or
`documented-group-binding-readonly`, or `authenticated-readonly-key-settings-ui`).
For the UI source, save `observed_key_group_label`, `group_display_prefix` and
`key_match_scope="unique-project-row-masked-prefix6-suffix4"`. Preserve the exact
approved group label; a displayed prefix is recorded separately and checked
against it. This partial masked-key/project-row match is operational evidence,
not a full cryptographic or upstream identity proof. Usage-only evidence cannot double as proof
of the group name. Never place a key, account identity or
session credential in this record. The record is not inferred from token estimates.

The gate revalidates saved output/input/prompt, checks reported token bounds and
identity against the record, and records actual charge independently of usage-cost
ceilings. It then seeds that same successful result into Single's first decision;
no duplicate provider call occurs. Completed runs and Replays remain idempotent.
A confirmed charge above the total budget prevents continuation and cannot be
silently rewritten. Unknown charges retain conservative reservations.

The input byte/token bound is 32,768 plus a separate bounded output allowance,
well below the selected model's publicly noted 272k long-context boundary.
Public display/native assumptions must not substitute for runtime USD evidence.
The 32-attempt overall ceiling includes smoke, all roles and retries. Only the
first actual record is required for this continuation gate; other unreconciled
charges remain explicitly usage estimates or unknown reserves, not invoices.

## Offline reconciliation and price correction after a paid probe

Keep the original manifest, database, call, raw output and estimates unchanged.
The following operations load no key and send no HTTP request:

```sh
uv run --no-sync python tools/run_pilot.py --record-billing --manifest artifacts/kuaiapi/original-manifest.json --database artifacts/kuaiapi/research.sqlite3 --usage-evidence artifacts/kuaiapi/readonly-usage.json --probe-window artifacts/kuaiapi/execution-window.json --output artifacts/kuaiapi/charge-receipt.json
```

This accepts only one total/today/day/model request with matching tokens, zero
cache, explicit USD, matching local execution date/source, and consistent
`cost * effective_rate_multiplier = actual_cost`. It revalidates the saved strict
output with zero-call Replay. The actual deduction is stored separately and
immutably, even while the group gate remains closed. The correlation method is
explicitly singleton/date/model/tokens; no gateway request ID is fabricated.

Commit the reviewed correction, save a new **revision** manifest using the same
data/model/group/settings/cap, and register it against the original anchor:

```sh
uv run --no-sync python tools/run_pilot.py --config artifacts/kuaiapi/runtime-config.json --manifest artifacts/kuaiapi/runtime-revision.json
uv run --no-sync python tools/run_pilot.py --migrate-pricing --config artifacts/kuaiapi/runtime-config.json --manifest artifacts/kuaiapi/runtime-revision.json --continuation-anchor artifacts/kuaiapi/original-manifest.json --database artifacts/kuaiapi/research.sqlite3 --usage-evidence artifacts/kuaiapi/readonly-usage.json --output artifacts/kuaiapi/migration-receipt.json
```

The migration requires a confirmed original charge and clean committed source.
It stores both manifests, changes no old call or estimate, and preserves the
original pilot ID, comparison group, attempts, unknown reservations and total
cap. Repeating the identical migration is idempotent. Changing a model, group,
prompt, dataset, calendar, engine, capital, fees, budget or attempt limit is
rejected. Old pricing cannot reserve more calls; a revision cannot open a fresh
budget. A blocked candidate with incomplete prices can be registered without
authorizing inference. A different later revision needs another explicit migration
implementation/review; this narrow path never silently overwrites an audit record.

Any eventual execution must pass `--continuation-anchor` as well as the original
database, complete verified runtime pricing and independently verified group
record. It still reuses the original successful first decision. Never create a
fresh manifest/pilot/database to evade prior spending or unknown charges.

Final key-usage aggregates can be reconciled with `record_aggregate_charge` only
when lifetime/today/day/model counts and tokens match every saved attempt and
no unknown-usage attempt is covered. The original probe deduction is counted
once. Aggregate USD is never allocated to invented per-request charges. The
budget retains the larger of its conservative covered-call ceiling and actual
aggregate, plus all uncovered/unknown reservations. New evidence cannot erase
prior deductions, change the frozen multiplier or rewrite a settled aggregate.
Reported deductions remain distinct from formal invoices.
