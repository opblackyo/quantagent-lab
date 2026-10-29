# Local study storage boundary

`research_store.py` implements storage/admission bookkeeping for a local research
study. It does not launch experiments, send requests, load settings/keys, check
billing, expose HTTP routes or start a worker. The ordinary gateway restriction
and no-key Mock/Replay application remain unchanged.

## Explicit database ownership

Constructing `ResearchStore(path)` performs no IO. Only explicit
`ResearchStore.create_new(path)` creates a new database. Use a new ignored path
such as `artifacts/phase2/new-study.sqlite3`; an existing file is never overwritten.
`open_existing(path)` requires the Phase2 domain marker and supported schema.
It refuses missing, legacy and future-version databases. Do not point this at old
research evidence or pass this file to the ordinary application's legacy startup.

The existing Store process lock, connection transaction/RLock and runs/events
schema are reused. Research startup does not call legacy `Store.initialize()` or
its `SERVICE_RESTARTED` run update. Migration is explicit, transactional and
additive, uses individual SQL statements, and rolls back DDL/version on error.
Version1 initialization accepts only an empty database; repeating it on supported
schema is a no-op. No automatic migration of old evidence exists.

Status/cost polling reuses the owned keeper for SELECT/readonly PRAGMA queries.
It neither opens a new DB connection/file nor invokes a budget constructor,
migration or admission. An unopened store refuses reads. One owner and serial
transactions apply to this runtime; there is no distributed queue or new ORM.

Existing run/list/event/export reads also use `Store.read()`. The legacy pilot
budget GET uses `PilotBudget.read_summary()` instead of constructing a budget.
For an old attempt table without `actual_charge`, reads project unavailable charge
evidence as NULL and retain reservations; they never add a column. The existing
response shape is retained. New schema writes belong only to explicit initialization.

## Study and authorization lifecycle

1. `register(plan, PlanningInputs(...), idempotency_key=...)` verifies the P2-01
   immutable plan and stores DRAFT. Same key/manifest returns the same study;
   changed payload conflicts.
2. `freeze(study_id, inputs, at=...)` verifies again and creates immutable decision,
   role and attempt slots before FROZEN. IDs belong to that study and are not reused.
3. `record_authorization(ServerAuthorization(...), at=...)` is **server-only
   injection**, not a browser DTO. It binds one record to the exact study, manifest,
   namespace, profile/revision/source, caps, expiry and private opaque key/cohort
   references. References must never be real keys, fingerprints or account IDs.
4. `start(study_id, authorization_id, inputs, at=...)` atomically admits that account
   and marks QUEUED. A browser approval flag or requested cap is not authority.
   One active admission is permitted per key reference and per cohort reference.
   Repeated start does not allocate another budget. This operation submits nothing.

There is no default/production authorization source. Synthetic tests inject fake
records. This development boundary is not owner approval for any paid study; a
future execution service must validate its independently scoped server evidence.

`mark_running`, `pause`, `request_cancel` and `finish` persist lifecycle only.
RUNNING is not proof of an executing worker. Paused states distinguish billing,
unknown liabilities and drift. Terminal cancellation/failure cannot reopen.
COMPLETED also requires four completed source-child links and no outstanding
actionable/in-flight slots; no research is performed to obtain those conditions.

Child links refer to existing runs/events. They validate dataset, source strategy,
period/capital/costs, model, prompt, study group and frozen ceilings. Replay cannot
replace a source. Linking does not create runs, modify result JSON or execute a
strategy; this module has no execution or portfolio ledger of its own.

## Durable future-send claims and liabilities

`reserve_slot` checks a verified current plan/profile/expiry, selects only a
never-sent slot and atomically inserts the existing `pilot_attempts` reservation
with its slot transition. It uses the shared `budget_ledger` implementation also
used by legacy `PilotBudget`. Legacy fresh-ID/revision protections remain intact;
there is no constructor bypass flag or cap reset.

`mark_dispatched` is a one-time durable claim for a **future** sender. It does not
send. A second claim fails; profile drift, missing/altered reservation and orphans
block it. A lost response or crash after this claim must never be resent.

`record_observation` retains an immutable evidence hash/outcome and optional usage
estimate, then pauses for billing. The observation is trace evidence, not a second
money account. It does **not** release a hold, attribute an actual charge or
reconcile billing. Full unknown reservations remain in the shared ledger, even
for a successful observed result. Actual reconciliation is later integration work.

`recover_interrupted` is explicit. Reserved/dispatched or orphan attempts become
unknown/paused with their liability retained; a RUNNING study before any reservation
requires verification rather than automatic restart. `resume_eligibility` is pure
and exposes only verified never-sent slots. `queue_verified_unstarted` can requeue
those slots only under the original plan/caps with no unknown/orphan liability.
No method resets a successful, uncertain or interrupted attempt for resending.

Cancellation/failure preserves accounting. Only a terminal admission with **no
attempts at all** releases its key/cohort lease here. A lease with financial history
stays held pending future verified reconciliation/closure. Old approvals and
stopped-study remainders are not transferable. Do not restore a pre-reservation DB
as a code rollback or infer zero charge from timeout/restart/cancellation.

Safe `get_study` output uses P2-01's explicit projection and shared cost summary.
Private manifests, authorization payloads, evidence and key/cohort references remain
server-side. The local research HTTP surface returns a second allowlist projection.
It does not rewrite stored evidence, accept uploads, or return raw envelopes.

## Offline checks and sources

```sh
uv run python -B -m pytest backend/tests/test_research_store.py -q -p no:cacheprovider
```

Fixtures use generated data and new scratch SQLite only. They test migration
fault rollback/version refusal, single admission under races, exact authorization
scope, no-reset budgets, atomic attempt boundaries, uncertain restart/cancel,
pure reads, safe projection and existing Mock/Replay compatibility.

The project retains its pinned Python3.13/SQLite, Pydantic and existing schema/
accounting helpers; no dependencies or third-party source were added. Existing
native SQLite transactions fit the cross-table financial atomicity requirements,
so a new workflow/ORM stack would add integration work without replacing the
required domain checks. See [Python sqlite3 transaction control](https://docs.python.org/3.13/library/sqlite3.html#transaction-control),
[SQLite transactions](https://www.sqlite.org/lang_transaction.html) and
[SQLite readonly URI mode](https://www.sqlite.org/uri.html). The same MIT and
third-party notice scope applies.
