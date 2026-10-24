---
name: quant-backtest-auditor
description: Audit changes to this project's market data, features, strategy, risk, execution, ledger, or performance code for temporal leakage and backtest accounting errors. Use for quant code review or when these modules change.
---

# Quant Backtest Auditor

Read `docs/DEVELOPMENT_BLUEPRINT.md` and the affected code and tests. Report concrete findings with file locations, a reproducing case, and the effect on results. If implementation has not begun, audit the specification and label findings as design gaps.

Check these invariants where relevant:

- Every decision snapshot uses only bars and values available after that session's close. Changing later bars must not change an earlier snapshot. Distinguish a missing market session from an exchange holiday.
- A weekly signal executes no earlier than the next valid session's open. Apply the same signal schedule, warmup, start date, and execution rule to every comparison strategy.
- The ledger uses the stated price basis consistently. Buying uses adverse upward slippage, selling uses adverse downward slippage, and fees are charged once. Size buys so cash cannot fall below zero; enforce nonnegative shares and the exposure cap after execution.
- Keep `SKIPPED` or `REJECTED` separate from an approved target weight of zero. Failed inputs or models must not create a new order.
- Define how dividends, splits, price adjustment, and ending open positions affect reported returns. Do not label a price-only backtest as total return.
- Verify metrics against small hand-computable ledgers, including no-trade, flat-return, loss, partial position, and zero-volatility cases. Compare gross and net P&L using a fixed reference open, executed price, and quantity so slippage is not counted twice.

Run the smallest relevant deterministic tests when code exists. State exactly what passed, what failed, and what could not be checked; do not infer correctness from a rendered dashboard.
