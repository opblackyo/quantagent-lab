---
name: multi-agent-contract-tester
description: Validate this project's single-agent and Analysis-Critic-Decision contracts, trace, replay, failure handling, and risk boundary. Use when agent schemas, prompts, adapters, orchestration, or decision APIs change.
---

# Multi-Agent Contract Tester

Read `docs/DEVELOPMENT_BLUEPRINT.md`, then inspect the affected schemas, provider adapter, workflow, API, records, and tests. Give specific findings with file locations and an observable failing case. If no implementation exists, identify contract gaps in the specification.

Verify the following where relevant:

- Each Agent receives a versioned, immutable `FeatureSnapshot` for the decision session. Analysis, Critic, and Decision receive only their declared predecessors' validated outputs; prompts and tools cannot access future bars or unapproved external data.
- Parse and validate structured output before risk review. Reject malformed JSON, invalid enum or target weight, nonfinite values, unknown evidence fields, and unsupported risk codes. Preserve raw response and validation outcome for traceability without storing secrets.
- Route every proposed target weight through the deterministic risk engine, then through execution and ledger code. No Agent, tool, replay path, or API route may mutate holdings or create trades directly.
- On timeout, provider error, exhausted budget, or validation failure, record the reason and skip new orders while retaining the current position. A deliberate target weight of zero remains a valid liquidation decision.
- Trace a decision to dataset hash, feature version and snapshot, model identifier, model settings, prompt version and hash, raw and validated responses, token and cost record, risk result, execution session, and resulting trade or no-trade reason.
- Mock uses fixed labeled fixtures. Replay makes no provider call and reuses saved decisions with a clearly specified scope for re-running risk and backtest code. Compare single and multi-agent modes under the same market, timing, model family, and risk rules.

Run focused contract and negative-path tests when code exists. Report tested behavior and unresolved assumptions separately.
