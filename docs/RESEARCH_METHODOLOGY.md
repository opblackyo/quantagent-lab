# Research methodology

## Scope and data

One symbol (SPY), daily raw OHLCV, long-only, no leverage, fractional shares. Price-only returns exclude dividends and cash interest; there is no forced final liquidation. The public fixture is generated synthetic data and must never be described as historical SPY pricing. Real data stays in a private cache under its provider's terms.

An experiment records the dataset byte SHA-256. Comparisons share dataset, period, capital, cost assumptions, feature/engine versions and decision/execution schedule. API agent comparisons also require compatible model/prompt provenance. Missing normal XNYS sessions, duplicates, unsorted dates, nonfinite or inconsistent OHLC are rejected. Holidays are defined by the exchange calendar, not by missing CSV rows.

## Time and signals

Indicators use only information available after each decision session closes. Warmup requires at least 60 dataset bars. Weekly decisions occur on the last exchange session of each ISO week and can execute only at the next valid session's open. All strategies use this schedule; Buy & Hold establishes its position at the first common eligible execution. Signals with no execution inside the requested period are skipped.

MA20/60 use full windows. RSI14 uses Wilder's first 14 changes as its seed, with flat RSI=50. ATR14 seeds with the first 14 true ranges. Volatility is the sample standard deviation of 20 log returns, annualized by square root of 252. Features include five-day return and volume relative to its 20-day average.

## Decisions, risk and fills

Agent inputs contain numeric features and validated predecessor outputs; dates, symbols, credentials and external search are excluded. Output weights are numeric 0, 0.5 or 1. Confidence is an uncalibrated self-assessment, not a profit probability. Pydantic validation precedes deterministic exposure checks.

Invalid input/output, provider errors or exhausted budgets produce recorded skipped decisions that retain holdings. A valid approved zero weight is liquidation. Buy fills have adverse upward slippage, sells downward slippage, and fees apply once to execution notional. Sizing solves for post-cost target exposure at the reference open while preserving nonnegative cash/shares. Slippage embedded in fill price is not charged again.

## Metrics and fairness

Daily close equity includes cash and holdings. Returns/drawdown include initial cash. CAGR and Sharpe use 252-session annualization; undefined ratios are null. Turnover is absolute traded notional divided by average daily equity. Report costs, exposure time, approved/skipped/rejected decisions, real API calls, Mock calls, tokens, latency and unknown-cost reservations alongside returns.

Single-Agent performs one role per eligible decision; Multi-Agent performs up to three, plus bounded retries. Comparing architecture alone requires an additional equal-inference-budget control. Failures are part of results and must not be silently removed.

## Reproducibility and interpretation

Save dataset/config/code hashes, numeric snapshot, role input, instructions, raw/validated output, model identifiers/settings, prompt/schema/input hashes, validation, risk, cost and execution evidence. Replay makes no model call and records its source. Paid usage estimates are not invoices.

Freeze model version, prompts, parameters, data and budgets before evaluating a holdout. Planned periods are 2019–2023 development, 2024 validation and 2025 project holdout. These formal model experiments are not complete. A pretrained model may already know historical events even when dates are hidden, so the holdout does not prove unseen model knowledge. Dividend-inclusive sensitivity and prospective validation remain future work.
