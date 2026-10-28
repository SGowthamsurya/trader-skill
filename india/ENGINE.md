# Backtest engine (Phase 3): design and stages

Building the engine and authorising a valid performance run are separate.
Every run is labelled by `readiness.py`; until every gate passes, the only
possible label is **MECHANICS ONLY - NOT A PERFORMANCE RESULT**. Synthetic
fixtures test mechanics and are never presented as market results.

## Data flow

```
pit.MarketData (engine only)
   -> pit.View(as_of)            past-only, read-only, per decision session
   -> strategy(view) -> targets  no access to fills, outcomes or evaluator
   -> signals ledger             hashed, chained, written before the next session
   -> simbroker (T+1)            whole units, cash, 5% rule, no gap or no-file fills
   -> portfolio (marks.py)       stale and gap flags carried into every figure
   -> evaluate                   reads only the sealed ledger and fills
   -> readiness gate             VALID PERFORMANCE RESULT or MECHANICS ONLY
```

## Stages

| stage | module | status |
| --- | --- | --- |
| 1 | `pit.py`: point-in-time market view | done |
| 2 | `universe.py`: point-in-time universe; liquidity, listing and presence on trailing data only; candidates rebuilt from historical files (survivorship); split adjustment applied only from each action's `announced_on` | done |
| 3 | `signals.py`: strategy interface and hash-chained signal ledger; strategy optionally in a separate process | done |
| 4 | `simbroker.py`, `portfolio.py`: T+1 fills at the next tradable session's open, whole units, cash, rejections with reasons | done |
| 5 | `costs.py`, `cost_schedule.json`: evidence-backed, effective-dated components; unresolved (never zero) when any is missing | done |
| 6 | `readiness.py`, `evaluate.py`, `backtest.py`: the gate, metrics, benchmarks, reproducible runner | done; the gate is BLOCKED (see `READINESS_REPORT.md`) |

## Readiness gate (a run is a valid performance result only if all pass)

1. Every ETF traded or used as a benchmark passes the evidence criteria:
   identity, corporate actions and distributions verified.
2. The candidate universe was built point-in-time (no 2026 listing used to
   choose 2018 holdings).
3. Every cost component has a verified value for every date in the window.
4. The NIFTY 50 TRI is imported, validated and covers the window.
5. The cost-matched NIFTYBEES benchmark passes its own checks.
6. Data gaps are exactly the documented, checksum-matched ones.
7. Input data matches its recorded checksums.
8. The data source is real exchange data, not a synthetic fixture.

Implemented in `readiness.py` (stage 6) as checks in ten categories (data,
calendar, universe, identity-corporate, distributions, costs-taxes,
benchmark, ledger, execution, reproducibility). Each is PASS, BLOCKED,
UNRESOLVED or NOT_APPLICABLE and mandatory or not; the run is READY only
if every mandatory check is PASS or NOT_APPLICABLE. Beyond the list
above it also requires the frozen strategy and criteria hashes, a clean
git tree, every inventoried file present with its checksum, an official
holiday list for every year of the window, category evidence knowable by
the evaluation start, at least one ETF eligible on every criterion, and
the ex-ante liquidity rule. Investor taxes, price limits and an
off-container backup are reported as non-mandatory limitations.

## Point-in-time universe (stage 2)

`universe.Universe(market_data, evidence, rules).decide(date)` returns a
`Snapshot` of `Decision`s (eligible, excluded, insufficient-data, inactive)
with a pass / fail / unresolved outcome and a date-specific reason for each
test, and a SHA-256 digest of its canonical JSON. It reads prices only
through `pit.View`.

- Evidence rule: a fact may support a decision on t only if the fact is
  dated on or before t (an action's `announced_on`, a confirmation's
  `effective_on`, the period a distribution review covers). An undated fact
  is knowable only from its retrieval date: the AMFI scheme names, the only
  category evidence so far, are knowable from 2026-10-28, so every earlier
  decision has category unresolved until dated scheme documents are imported.
  Exchange-listed names are shown as hints, never as evidence.
- Window: the last 250 tradable sessions up to t; 120 sessions of history
  needed; above 5% unknown-state days, liquidity and presence are
  unresolved (`etf_criteria.json` point_in_time). Approved by the user on
  2026-10-28 as research assumptions, not optimized values; they are not to
  be tuned on the final evaluation period.
- A valid session on which an instrument did not trade is an observed zero;
  a documented gap, an invalid file or an unobserved weekday is unknown and
  left out, never zero. No-file days and holidays are not sessions.
- Candidates are the fund instruments in the collected files up to t. An
  instrument that stops trading is reported inactive once it leaves the
  window. This reduces survivorship bias only as far as the surviving files
  reach; it is not shown to be eliminated.
- `etf.py` remains a whole-period data-quality review, not a selection rule.

## Signals and the ledger (stage 3)

- A strategy is a class with `NAME`, `VERSION`, `LOOKBACK` and
  `decide(inp) -> {key: weight}`. It receives only a `StrategyInput`: the
  decision date, eligible keys and their ISINs, the universe and market
  digests, and the closes of eligible keys inside its lookback. No view,
  universe, evidence, cache, fills or portfolio.
- `run_isolated` runs `decide` in a separate `python -I` process in an empty
  directory, given only the input as JSON: future data is not in its memory.
  It is not a filesystem sandbox; a strategy that opens cache files itself
  is outside what the engine can prevent.
- Targets: eligible keys only, weights as 6-decimal strings, each at least
  0, total at most 1 (the rest is cash). Invalid targets never reach the
  ledger.
- Ledger: canonical JSON lines (sorted keys, no floats), `open` /
  `signal` / `seal` records, each hashed and linked to the one before;
  strictly increasing decision dates; append-only API that re-verifies the
  file; nothing after the seal. The signal chain has no timestamps, so the
  same inputs give a byte-identical chain; the seal holds the only one.
- Trust boundary: the chain detects edits, deletions, insertions and
  reordering. A process able to rewrite the file can rewrite every hash and
  forge a consistent chain (a test shows this); only the seal hash recorded
  outside the ledger before evaluation catches it. `open_sealed(path,
  expected_seal)` is the only reader for later stages and refuses an
  unsealed ledger or a different seal.
- The dummy strategies in `tests/_india_dummy_strategy.py` are mechanics
  tests only, not strategies.

## Broker and portfolio (stage 4)

- Input: `Broker(data, ledger_path, expected_seal)` opens the ledger only
  through `signals.open_sealed` with the seal recorded outside it; no anchor,
  no run. The ledger file is never written.
- Decision after the close of t: the portfolio is marked on a past-only
  view; each target gets floor(weight x value / reference price) whole
  units, the reference price being t's close or a flagged stale mark;
  holdings outside the targets are sold to zero.
- Execution on the first weekday after t: trading -> fill at that
  session's open; official holiday or weekend -> next day; gap, invalid,
  unobserved, unreachable, conflict, or an official session without a file
  -> unfilled; no-file (unconfirmed closure) -> unfilled, or, with the
  explicit policy `on_unconfirmed_closure="roll"`, treated as a closure and
  recorded on the order. Day orders: never retried.
- Fill price: the execution session's open (printed after the order was
  placed), positive and within the day's low-high range. Sells before buys.
  The ETF must trade that session under the ISIN the order named (a holding
  keeps the ISIN it was bought under). Order value at most 5% of the day's
  traded value, else unfilled (no partial fill). No short sales. A buy
  needs units x price + known fees within settled cash: rejected by
  default, or cut to the most whole units affordable with
  `on_insufficient_cash="reduce"`, recorded.
- Settlement: sale proceeds become usable on the next tradable session,
  never for a buy on the day of the sale.
- Money is integer paise; a price that is not whole paise is refused, not
  rounded. Cash, unsettled proceeds, units and fees are recomputed from the
  journal after every event and must match exactly. Marks use `marks.mark`
  on past-only views: stale marks stay flagged, each mark records the
  documented gaps since the previous one, and a holding with no mark makes
  the value unknown, never zero.
- Costs: `CostModel.charges(...) -> Charges(paise, status, components)`.
  The default `UnresolvedCosts` returns no amount (status unresolved); the
  journal and every valuation say cash excludes those fees. No model treats
  fees as zero. The only fixed-fee model is a test fixture labelled
  synthetic.
- Every broker result carries the label MECHANICS ONLY until stage 6.
- Not modelled because the price files do not support it: price bands and
  circuit limits, slippage from the open, queue position, partial fills.
- Deviation (recorded before any real-market test): the 5% rule first used
  the execution day's full traded value, which is not known at the open.
  The default is now `liquidity_cap="ex-ante"`: when the order is placed its
  value at the reference price must be at most 5% of the ETF's traded value
  on the decision day, known then; otherwise it is not placed. Each fill
  records, as an audit field only, whether it was also within 5% of the
  execution day's value. The old rule remains as `liquidity_cap="ex-post"`,
  a labelled sensitivity mode that is never the default.

## Costs (stage 5)

- `cost_schedule.json`: every component that may apply to an ETF delivery
  trade (brokerage, STT, BSE and NSE transaction charges, SEBI turnover
  fee, stamp duty, depository debit charge, GST on charges), with where it
  applies, its basis (turnover, per fill, per debit, on other components),
  and dated periods holding an exact rational or paise rate, a rounding rule
  and a cited, checksummed primary source. Overlapping periods are refused.
- `costs.ScheduleCosts` implements the stage 4 `CostModel`: one component
  at a time, exact Decimal, rounded per component to paise. A component
  without a verified period covering the trade date is unresolved; the
  fill's Charges keep the known part (`known_paise`), list what is
  unresolved and have status unresolved. The portfolio deducts the known
  part and counts the fill as having an unresolved remainder; never zero.
- On 2026-10-28 no component has a verified rate for the study window
  (12 component-side pairs unresolved); the CDSL debit tariff is recorded as
  ambiguous partial evidence, not a rate.
- Investor-level taxes (capital gains, tax on distributions) are listed with
  what they depend on and are never computed; results are pre-tax.

## Gate, metrics and runner (stage 6)

- `readiness.py`: the gate above. `python3 india/backtest.py readiness`
  writes `READINESS_REPORT.md` and `readiness_report.json` (committed).
- `evaluate.py`: exact metrics from the broker journal: start and end
  value, return net of known costs (labelled when any fee is unresolved),
  CAGR only over at least 365 days, maximum drawdown over marks, volatility
  only from at least 24 monthly returns, turnover, orders by outcome, fees
  by component, time invested, stale and gap counts, and benchmark-relative
  figures for benchmarks the gate found valid. Uninvested cash is the
  zero-return control.
- `backtest.py run`: the pre-registered experiment (`PROTOCOL.md`). It
  refuses, printing BLOCKED - NO VALID PERFORMANCE RESULT, unless the gate
  is READY before and after the market data is loaded, and it refuses
  market data that did not come from the verified cache. It writes the
  ledger, a separate seal anchor, orders, journal, results (headline,
  pre-specified sensitivity runs, cost-matched NIFTYBEES buy-and-hold) and
  a manifest (window, venue, git commit and dirty state, hashes of every
  code and config file, protocol, strategy, criteria, cost schedule and
  inventory, the inventory check, TRI file hashes, ledger head and anchor,
  policies, Python version, readiness digest, output digest) under the
  ignored `india/data/runs/`.
- `backtest.py mechanics`: the same pipeline on synthetic data built in
  code (made-up ETFs dated in the 2090s) and synthetic costs; every
  result says MECHANICS ONLY - NOT A PERFORMANCE RESULT. It refuses cache
  data. Two runs give the same output digest.

## Known limits

- A decision on the full BSE history takes about 30 seconds; a run over
  many decision dates will need an incremental index (stage 3 or later).

- Python cannot hide private attributes from code that searches for them;
  the View prevents accidental look-ahead. The ledger and process isolation
  are the stronger guarantees.
- Prices are BSE's. NSE history is unavailable from this environment.
