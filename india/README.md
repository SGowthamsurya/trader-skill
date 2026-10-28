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

## What is here (Phases 1 to 2.2)

| file | job |
| --- | --- |
| `sources.json` | every data source: purpose, primary or secondary, URL template, access status, date checked, evidence, uncertainties |
| `sources.py` | validates `sources.json`, builds URLs from templates with plain-token parameters only |
| `fetch.py` | polite downloads into a SHA-256 content-addressed cache with an append-only manifest |
| `tri.py` | imports a manually downloaded NIFTY 50 TRI CSV, validates it, records its checksum; reports BLOCKED until one is imported |
| `bhav.py` | reads NSE/BSE bhavcopies (legacy and UDiFF layouts), checks every row's date against the day fetched, and samples files politely |
| `calendar_in.py` | per-exchange calendar: observed sessions (files) kept apart from official holiday lists; a missing file is "no-file", never a holiday; no future dates predicted |
| `secmaster.py` | symbol and ISIN history per listing, with observed change windows, file checksums as provenance, and conflicts; ISIN changes stay proposed until confirmed |
| `etf.py` | finds candidate ETFs from AMFI scheme names and tests them against `etf_criteria.json` |
| `etf_criteria.json` | the documented inclusion criteria; the liquidity threshold is left for the user to set |
| `etf_candidates.json` | the 33 candidates found on 2026-10-28, with the checksums of the AMFI and BSE files used |
| `identity_confirmations.json`, `corporate_actions.json` | identity links and corporate actions, each with its documents and a verification level |
| `evidence.py` | imports official documents (notices, circulars, holiday lists, Acts) with URL, retrieval date and SHA-256, and checks that every cited document is present |
| `cost_tax_sources.json` | where each cost and tax component must be verified, split statutory / regulatory / exchange / depository / broker; no values |
| `data_gaps.json` | sessions whose published price file is corrupt: unresolved, excluded, checksum kept for forensic review |
| `marks.py` | valuation rules for any day, including gaps (no later price, no interpolation, flags) |
| `execution.py` | whether a simulated order could fill: valid session, a verified trade in the ETF, at most 5% of its traded value |
| `MANUAL_DOWNLOADS.md` | the documents only a person can fetch here, with exact pages, formats, ranges and import commands |
| `ENGINE.md` | the backtest engine design, stages and readiness gate |
| `pit.py` | point-in-time market view: a strategy sees only sessions up to its decision date; reading later raises |
| `universe.py` | point-in-time universe: which ETFs could be held on a date, from data and evidence knowable then, with reasons and a digest |
| `signals.py` | strategy interface (past-only input, optional separate process) and the append-only, hash-chained, sealed signal ledger |
| `portfolio.py` | accounting in integer paise: settled cash, unsettled proceeds, whole units, fees, a journal reconciled after every event |
| `simbroker.py` | simulated broker: sealed-ledger input, next-session open fills, 5% cap, gap and closure rules, cost interface (no costs assumed) |
| `ETF_REVIEW.md` | the current verdict for every candidate |

```bash
python3 india/sources.py check
python3 india/sources.py list --purpose prices
python3 india/fetch.py get nse-bhavcopy-legacy --date 2018-01-02
python3 india/fetch.py verify
python3 india/tri.py status
python3 india/bhav.py sample BSE --from 2018-01 --to 2026-10
python3 india/calendar_in.py coverage BSE 2018-01-01 2026-10-28
python3 india/secmaster.py history BSE 590103
python3 india/etf.py evaluate
python3 india/etf.py liquidity
python3 india/bhav.py collect BSE --from 2018-01-01 --to 2026-10-27 --max-requests 300   # resumable
python3 india/evidence.py check
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

## Data gaps and valuation (approved 2026-10-28)

Two BSE sessions, 2022-02-07 and 2022-07-15, have published files that are
corrupt (rows spliced mid-field). They are listed in `data_gaps.json` with
the checksum of the corrupt file, which stays in the local cache for
forensic review and is never read as market data. The calendar marks them
`gap` only when that checksum matches the invalid file on record.

- No signal is generated and no trade is made on a gap (or on any day that
  is not a valid session: `calendar.tradable`).
- No price is interpolated or invented for a gap.
- A position is marked at the last valid close strictly before the day,
  flagged `stale`; a later price is never used (that would look ahead).
  With no earlier close there is no mark.
- A return is computed between marks and flagged `stale-endpoint` when an
  endpoint is stale and `spans-gap: DATE` when a gap lies after the start
  and on or before the end. A flagged return is never reported as clean.
- Sessions on either side of a gap are not adjacent, so a price break
  across a gap cannot be located or explained.

`tests/test_india_marks.py` pins each rule.

## Liquidity and execution (approved 2026-10-28)

- Eligibility: median daily traded value on BSE of at least Rs 3,00,000
  (provisional), counting sessions without a trade as zero from the ETF's
  first observed day. Recorded in `etf_criteria.json`; a test fails if it
  changes.
- Execution, separately: a simulated order above 5% of the ETF's verified
  traded value that day is unfilled. A full Rs 3,000 order therefore needs a
  day with at least Rs 60,000 traded. No fill price or cost is set here.

## Durability of the local cache (risk; no backup exists)

Everything downloaded lives only in `india/data/` (ignored by git) on the
machine that ran the collection. On 2026-10-28 that was a temporary cloud
container, which is reclaimed after inactivity. Measured then: 654 MB of
raw files (2,176 BSE daily files, the AMFI NAV file, 4 official documents of
about 3.8 MB), a 363 MB derived index (rebuilt from the raw files on first
use) and a 1.1 MB manifest.

**No backup has been made or verified.** Checked on 2026-10-28: raw market
data may not go into git; no folder on the user's own computer is shared
with the session (Remote Control lists none); the artifact asset store
allows 15 MB per file and 256 MB per version and would publish third-party
data whose redistribution terms are unreviewed, so it is not used.

If the cache is lost:

- Committed files still record what was used: the checksums of the
  corrupt files (`data_gaps.json`), and the URL, retrieval date and SHA-256
  of every cited document (`corporate_actions.json`,
  `identity_confirmations.json`, `cost_tax_sources.json`,
  `etf_candidates.json`). `evidence.py check` reports each one missing.
- BSE daily files can be collected again with `bhav.py collect` (about
  2,100 requests, roughly an hour at one request per second). Their URLs
  follow from the dates; their individual checksums are only in the lost
  manifest, so a re-collected file cannot be shown identical to the first.
- Documents are re-downloaded from the URLs recorded in the committed files
  and imported with `evidence.py import`; a different checksum means the
  publisher's file changed and the evidence must be read again.
- Anything derived from the cache (`ETF_REVIEW.md`, eligibility) must be
  regenerated and compared before it is relied on.

To make the cache durable, the user can share a folder on their own
computer through Remote Control (the session can then copy `india/data/`
there with the user's approval), or approve committing a compact checksum
inventory of the manifest (metadata only, no market data).

## Evidence levels

- **verified**: an official document, retrieved and read, states the fact.
  Only this resolves an identity link or a corporate action.
- **corroborated** (identity links): official sources together point to
  it, but no single retrieved document states it. Shown, never relied on.
- **unverified**: a lead (often a search summary of a document that could
  not be retrieved). Resolves nothing.

A split ratio always comes from a document. A price break is only checked
against it; a ratio is never inferred from prices.

No price test can show that a distribution (dividend / IDCW) did not occur,
so an ETF is not eligible until its distribution history for the window is
verified from official documents (criterion `distributions`, added in Phase
2.2 for review).

## Documents only a person can fetch here

See `MANUAL_DOWNLOADS.md` for the current list.

Some publishers refuse this environment (NSE's archive, India Code, the
income-tax site, HDFC AMC). They are not worked around. Download the
document in a browser and import it:

```bash
python3 india/evidence.py import FILE.pdf --url https://... --retrieved-on YYYY-MM-DD \
    --title "..." --publisher "..."
```

## Benchmarks and windows (agreed in Phase 0)

- NIFTY 50 TRI (official file, above) and cost-matched buy-and-hold NIFTYBEES.
- Primary window: 2018 onward, multi-ETF. A separate NIFTYBEES-only
  experiment from 2012 onward is a different universe and is reported apart.
- Candidate funds: NIFTY 50, Nifty Next 50 and Bank ETFs, each included only
  after its history, liquidity, inception date, symbol and ISIN changes and
  corporate actions are verified. No gold, international or debt ETFs.

## Known data facts (checked 2026-10-28)

- NIFTYBEES (BSE 590103): close 1292.95 on 2019-12-18 and 129.90 on
  2019-12-19, adjacent sessions. Its ISIN changed from INF732E01011 to
  INF204KB14I2 between the same two sessions. BANKBEES (590106) shows the
  same pattern on the same days. Both are consistent with a 10:1 unit split
  and an ISIN change, and both stay unconfirmed until an official notice is
  read.
- BSE did not restate PREVCLOSE on those ex-dates, so the price files alone
  cannot confirm a split.
- One ETF can carry several names: ISIN INF109K012R6 was NIFTYIWIN,
  ICICINIFTY and then NIFTYIETF on BSE. Neither symbol nor ISIN is a stable
  key over time.
- BSE switched bhavcopy layouts between 2024-07-01 and 2024-08-01.
- The NSE archive refused connections on 2026-10-28 after serving files
  earlier that day; BSE is the only venue with reachable history for now.
- A bhavcopy lists only what traded that day: absence is not delisting.
- Phase 2.1: BSE daily files collected for every weekday 2018-01-01 to
  2026-10-27: 2,174 sessions, 126 weekdays refused in both layouts
  (closures, unconfirmed), and 2 files corrupt as published (2022-02-07,
  2022-07-15), left unresolved.
- Reliance Nippon Life AMC Notice cum Addendum No. 107 verifies the
  NIFTYBEES and BANKBEES face-value change from 10 to 1 with record date
  2019-12-20. The new ISINs are corroborated, not verified: no retrieved
  document states both old and new.

Education and tooling, not financial or tax advice.
