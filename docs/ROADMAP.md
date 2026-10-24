# Roadmap

## v0.1.0-alpha publication preparation

- Bilingual README, architecture/methodology, synthetic screenshots and walkthrough.
- Redacted secret/privacy checks, portable installation, read-only CI and release checklist.
- MIT selected with project attribution. Publication identity/history, destination and final approval remain controlled by the owner.
- Run hosted CI and configure private security reporting before announcing release.

## Real-model research

- Select a fixed model and explicit token price assumptions; configure credentials privately.
- Verify one development-period real call, structured output, usage and budget accounting.
- Freeze model/prompt/data/budget, compare Single/Multi with baselines and inference-budget controls.
- Complete project holdout only after freezing; publish failure/coverage/cost and limitations.
- Add dividend-inclusive sensitivity and independent accounting cross-checks.

## Future engineering

- Provider adapters beyond the current OpenAI adapter, with contract/negative tests.
- Better research reports, prompt comparisons and additional authorized datasets.
- Assess patched SQLite/runtime releases against the locked compatibility suite.
- Design a synthetic Mock/Replay public demo separately: authentication/quotas where needed, persistence, background jobs and isolation. Anonymous visitors must not trigger paid inference.

No real broker orders, leverage, short selling or autonomous prompt evolution are planned for this alpha.
