# Offline research planner

The local planner previews a bounded four-strategy study. It does not start a run,
load credentials, check live billing, initialize SQLite/budgets or submit work.
Ordinary gateway API launch remains restricted. Mock/Replay routes are unchanged.

`backend/app/research_contracts.py` defines `StudyRequest`, injected
`ServerResearchProfile`/`SourceContext`, `FrozenStudyPlan` and `SafePlanView`.
`backend/app/research_service.py` provides:

```python
profile = resolve_profile(request.profile_id, server_allowlist)
frozen, safe_view = plan_study(request, profile, registry, context=source_context)
verify_plan(frozen, request, profile, registry, context=source_context)
```

There is no default runtime allowlist, key/config loader, implicit current clock,
HTTP endpoint or execute mode. Callers inject a verified source inventory, stable
aware clock and normalized effective USD prices/evidence references. The planner
checks their consistency and expiry; it cannot attest that injected claims match
current billing. No browser input can supply rates, an endpoint, a key, private
group binding, raw provider settings, an arbitrary model label or funding approval.

The request's budget and attempt ceiling are preferences, **not approved funding**.
Incomplete/missing prices reject validation. Cost estimates reserve full input plus
full cache creation and bounded output. The complete attempt plan and the smaller
requested call ceiling are reported separately; insufficient caps produce a warning.
`capped_reservation_usd` is the cost ceiling for that number of attempts, before the
requested monetary cap; an underfunded preview is not permission to exceed either.
Baselines consume zero provider calls and zero inference cost. Transaction fees and
slippage are assumptions; actual trade costs cannot be predicted before decisions.

The manifest binds dataset bytes, canonical snapshots (without UUID identity),
as-of times, numeric features, XNYS weekly/next-open sessions, source/locks/versions,
role prompts/schemas, settings/timeout policy, rates, explicit retry ceiling,
expiry/drift policy and private evidence references. Features-only payload hashes
are exact. Later Critic/Decision payloads are **input templates**, since validated
predecessor outputs do not yet exist. Their actual payload/output hashes must be
recorded by a future execution service. No predecessor content is fabricated.

`FrozenStudyPlan.canonical_payload` is immutable canonical JSON; `.payload` returns
a detached private copy. `verify_plan` recomputes the preview and rejects drift; it
does not resume anything. The explicit safe projection omits private evidence,
raw profile/config, filesystem paths and account/group binding. It includes the
scientific model/settings, calendar, versions, call/cost estimates and warnings.
Do not send the private manifest to a browser in place of that projection.

Current reference defaults are January 2023, raw-price SPY, no dividends/interest,
60-bar warmup and one common next-open calendar. Server allowlisting narrows dates
and data; features are calculated only through the requested end. The planner
does not download or evaluate a holdout. Rejected/invalid
decisions retain holdings; approved zero is liquidation in the existing engine.

Run the synthetic, side-effect-guarded tests:

```sh
uv run python -B -m pytest backend/tests/test_research_planner.py -q -p no:cacheprovider
```

The generated CSV/profile in these tests is synthetic. It exercises deterministic
hashes, price/call arithmetic, malicious requests, data/calendar rejection, expiry,
drift, privacy and guards against provider, DB, worker and credential access. It is
not historical research or evidence of live model reliability.

The planner itself still does not admit, fund, reconcile, or launch work.
A separate local fixture runtime can expose an allowlisted `/api/v1/research`
preview and, only when that process was started with an injected synthetic
store, a bounded fixture execution path. The default application leaves that
path disabled. Requested caps are not funding, and this module must not be
used to bypass the existing private-runner restriction.

Pure prompt/schema helpers live in `agent_contracts.py`; the existing `agents`
module re-exports them. The common schedule lives in `data.py`; `experiments`
re-exports it. Their original behavior is preserved while preview avoids importing
the execution graph and its runtime dependencies.
