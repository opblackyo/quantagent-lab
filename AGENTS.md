# QuantAgent Lab

This repository is a research and education project for SPY daily-bar, weekly-decision backtests. Read `docs/DEVELOPMENT_BLUEPRINT.md` before implementation and `docs/PROJECT_REVIEW.md` for unresolved design decisions. Complete the phase-0 technical review before phase-1 feature work.

- Keep all comparisons on one immutable, hashed dataset and common decision and execution calendar. A signal formed after a session close may execute only at the next valid session's open.
- Calculate features, risk, fills, cash, holdings, and metrics in deterministic code. Never let an LLM directly place an order or edit a ledger.
- Treat invalid data, invalid Agent output, provider failure, or budget exhaustion as a recorded skipped decision that preserves the current position. Keep this distinct from an approved zero-weight liquidation.
- Store enough input, configuration, model, prompt, validation, risk, and execution evidence to trace and replay each decision. Never persist API keys.
- Use fixed CSV fixtures, Mock, and Replay for tests. Label mock results in the UI; do not present them as live-model research results.
- Consult current official documentation when selecting or changing version-sensitive library APIs. Pin verified dependency versions in phase 0.
- When changing quant code, use the `quant-backtest-auditor` skill; when changing Agent contracts or workflows, use `multi-agent-contract-tester`. Add tests for the affected invariants and report commands and observed results.
- Keep the application research-only: no broker or exchange order integration. Resolve specification conflicts explicitly in the phase-0 review before implementing them.
- Never publish the owner's API keys, tokens, credentials, account identifiers, private email addresses, account plans/usage, profile locations, or personal machine paths. This applies to code, docs, screenshots, recordings, exports, logs, CI output, and Git commit metadata/history. Report sensitive findings by category and location only; never echo their values.
- Before any public release, audit the exact release files and all history to be pushed. Use synthetic Mock/Replay data for public examples. Keep local development settings and private artifacts outside tracking. Do not push or publish until the owner confirms the reviewed release; do not rewrite existing history without explicit approval.
- The owner may explicitly approve a public GitHub owner/commit name and exact Settings noreply for publication. This exception applies only to that confirmed public identity; never infer a noreply address or disclose private email, credentials, or unrelated account details. An approved identity/license is not permission to publish.
