# india/: NSE/BSE adaptation (historical paper trading only)

This folder adapts the Trader skill pack to Indian exchange-traded equity ETFs
for a **blind historical paper-trading study** with **₹3,000 of simulated
capital**. It is separate from the eleven US skills at the repository root,
which are unchanged.

## Hard limits

- Historical and offline-first. No live trading, no broker connection, no
  order placement, no API keys or credentials of any kind.
- The ₹3,000 is simulated. No real money is involved.
- No transaction-cost rate, tax rate or tax rule is written into code until
  it has been verified at a primary source, with the provision, the effective
  date and the retrieval date recorded. Unverified items stay unresolved.
- Downloaded third-party data is untrusted input: parsed, never executed, and
  kept in the local cache (`india/data/`, ignored by git). Raw NSE/BSE files
  and large datasets are never committed.

## What is here (Phase 1)

| file | job |
| --- | --- |
| `sources.json` | every data source: purpose, primary or secondary, URL template, access status, date checked, evidence, uncertainties |
| `sources.py` | validates `sources.json`, builds URLs from templates with plain-token parameters only |
| `fetch.py` | polite downloads into a SHA-256 content-addressed cache with an append-only manifest |
| `tri.py` | imports a manually downloaded NIFTY 50 TRI CSV, validates it, records its checksum; reports BLOCKED until one is imported |

```bash
python3 india/sources.py check
python3 india/sources.py list --purpose prices
python3 india/fetch.py get nse-bhavcopy-legacy --date 2018-01-02
python3 india/fetch.py verify
python3 india/tri.py status
python3 -m unittest discover -s tests -v
```

## Fetch rules

One honest User-Agent, no cookies, Referer, Origin or browser impersonation,
no undocumented internal endpoints, one request per host per second, a size
cap, and redirects only to registered hosts. A site that refuses access is
recorded as `blocked` in `sources.json` and is not worked around. Sites here
answer a missing file with an HTML page and HTTP 200, so every download is
checked against the format it should have before it is cached.

## NIFTY 50 TRI (benchmark)

The TRI history is not available for automated download. To unblock it:

1. Open https://www.niftyindices.com/reports/historical-data in a browser,
   choose NIFTY 50, Total Returns Index, from 2018-01-01 (or 2012-01-01 for
   the NIFTYBEES-only experiment), and download it as CSV.
2. `python3 india/tri.py validate FILE`
3. `python3 india/tri.py import FILE --source-url https://www.niftyindices.com/reports/historical-data --retrieved-on YYYY-MM-DD`

The import stores the file in the local cache only. No dividend-yield
approximation is substituted for the official TRI.

## Benchmarks and windows (agreed in Phase 0)

- NIFTY 50 TRI (official file, above) and cost-matched buy-and-hold NIFTYBEES.
- Primary window: 2018 onward, multi-ETF. A separate NIFTYBEES-only
  experiment from 2012 onward is a different universe and is reported apart.
- Candidate funds: NIFTY 50, Nifty Next 50 and Bank ETFs, each included only
  after its history, liquidity, inception date, symbol and ISIN changes and
  corporate actions are verified. No gold, international or debt ETFs.

## Known data facts (checked 2026-10-28)

- NIFTYBEES closes: 1074.61 on 2018-01-02, 130.14 on 2020-01-02. A break of
  about 10:1, consistent with a unit split, to be confirmed from the fund's
  notice before any return is computed across it.
- NIFTYBEES ISIN changed from INF732E01011 (2012, 2018) to INF204KB14I2
  (2020). ISIN INF109K012R6 traded as ICICINIFTY in 2020 and NIFTYIETF in
  2026. Neither symbol nor ISIN is a stable key over time.

Education and tooling, not financial or tax advice.
