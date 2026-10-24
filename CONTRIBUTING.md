# Contributing

The project is licensed under MIT, attributed to QuantAgent Lab contributors. Public repository publication and contribution intake remain pending final approval.

Use Python 3.13, uv, Node.js 22 and npm. See [README](README.md) for installation, launch and verification commands. Stop normal Next dev before browser tests. Tests use fixed synthetic data and Mock/Replay; never enable paid API calls in tests or PR workflows.

- Read root `AGENTS.md` and the research methodology before changing execution or signals.
- Use the project backtest-auditor skill for quant changes and contract-tester skill for agent changes.
- Add meaningful tests for causality, fees, positions, schema failures or replay when affected.
- Preserve common dataset hashes/calendars across comparisons and next-session-open execution.
- Consult current official documentation before changing version-sensitive APIs; update exact pins and lockfiles together.
- Describe observable changes, verification commands/results and limitations in the review.

Never submit credentials, private identities, profile paths, databases, real provider responses or proprietary market data. Use synthetic examples. Inspect staged files and commit metadata before sharing; `.gitignore` cannot remove older history. Run `python tools/audit_public.py` and Gitleaks with full redaction; visually review images/video.

Follow [SECURITY.md](SECURITY.md) for sensitive reports. Do not post private records in public issues.
