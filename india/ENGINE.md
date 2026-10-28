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
| 3 | `signals.py`: strategy interface and hash-chained signal ledger; strategy optionally in a separate process | next |
| 4 | `simbroker.py`, `portfolio.py`: T+1 fills at the next tradable session's open, whole units, cash, rejections with reasons | |
| 5 | `costs.py`: evidence-backed, effective-dated components; `CostUnavailable` when any is missing | |
| 6 | `readiness.py`, `evaluate.py`, `backtest.py`: the gate, metrics, benchmarks | |

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
  unresolved (proposed values, `etf_criteria.json` point_in_time).
- A valid session on which an instrument did not trade is an observed zero;
  a documented gap, an invalid file or an unobserved weekday is unknown and
  left out, never zero. No-file days and holidays are not sessions.
- Candidates are the fund instruments in the collected files up to t. An
  instrument that stops trading is reported inactive once it leaves the
  window. This reduces survivorship bias only as far as the surviving files
  reach; it is not shown to be eliminated.
- `etf.py` remains a whole-period data-quality review, not a selection rule.

## Known limits

- A decision on the full BSE history takes about 30 seconds; a run over
  many decision dates will need an incremental index (stage 3 or later).

- Python cannot hide private attributes from code that searches for them;
  the View prevents accidental look-ahead. The ledger and process isolation
  are the stronger guarantees.
- Prices are BSE's. NSE history is unavailable from this environment.
