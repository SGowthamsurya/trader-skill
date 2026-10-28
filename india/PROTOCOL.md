# Baseline experiment protocol (pre-registered 2026-10-28)

Frozen in `protocol.json`, committed before the evaluator, the runner or
any performance figure existed. The runner refuses a run whose strategy file
or criteria file does not match the frozen SHA-256.

## Hypothesis

A passive, equally weighted holding of every point-in-time eligible NIFTY
50, Nifty Next 50 and Nifty Bank ETF, rebalanced monthly from Rs 3,000 in
whole units, tracks the NIFTY 50 TRI before costs. Its shortfall after
evidence-backed costs measures what costs, whole-unit rounding and cash drag
take from a Rs 3,000 account. It is a measuring stick, not a search for an
edge.

## Rules

- Decision dates: the first tradable session of each calendar month in the
  evaluation window. Knowing that a session is the first of its month needs
  no later information.
- Signal (`strategy_baseline.py`, `EqualWeightEligible` v1): weight 1/n
  (rounded down to six decimals) on each of the n ETFs the point-in-time
  universe marks eligible that day, keys sorted; none eligible means all
  cash. No prices, no lookback, no parameters. Run in a separate process.
- Orders and fills: the stage 4 broker with the protocol's policies:
  ex-ante 5% liquidity cap on the decision day's traded value; fill at the
  next tradable session's open; day orders never retried; no-file days
  unfilled; insufficient cash rejected; sells before buys; proceeds settle
  on the next tradable session; no shorts, no leverage, whole units.
- Cash: whatever the whole-unit targets leave over; it earns nothing.
- Ties: none arise (every eligible ETF gets the same weight).

## Windows

- Development, 2018-01-01 to 2021-12-31: data and mechanics checks only.
  The strategy has no parameters, so nothing is tuned on it.
- Evaluation, 2022-01-01 to 2026-10-27. Its point-in-time inputs reach back
  250 sessions before each decision.

## Benchmarks

- NIFTY 50 TRI, only when the official file is imported, validated and
  covers the window.
- Cost-matched NIFTYBEES buy-and-hold, only when NIFTYBEES's identity,
  corporate actions, distributions and costs are verified.
- Uninvested cash: zero return by construction; a control, not a market
  benchmark.

## Sensitivity (pre-specified)

Unconfirmed closures rolled instead of unfilled; insufficient cash reduced
instead of rejected; the ex-post (same-day) liquidity rule, which uses
same-day information and is never a headline result.

## What is reported

Pre-tax only: investor-level taxes are reported as unresolved. Every figure
states whether any fee was unresolved. No result is called valid unless the
readiness gate passes.
