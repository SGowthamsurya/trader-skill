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
| 2 | `universe.py`: point-in-time universe; liquidity, listing and presence on trailing data only; candidates rebuilt from historical files (survivorship); split adjustment applied only from each action's `announced_on` | next |
| 3 | `signals.py`: strategy interface and hash-chained signal ledger; strategy optionally in a separate process | |
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

## Known limits

- Python cannot hide private attributes from code that searches for them;
  the View prevents accidental look-ahead. The ledger and process isolation
  are the stronger guarantees.
- Prices are BSE's. NSE history is unavailable from this environment.
