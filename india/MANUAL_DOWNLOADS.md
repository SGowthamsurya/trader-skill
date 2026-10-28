# Manual downloads (Phase 2.2 checklist)

These publishers refuse requests from the cloud environment, or need a choice
made in a browser. Nothing here is treated as obtained until the file's
bytes are imported, checked and recorded with a SHA-256. Save every file
exactly as downloaded: do not edit, rename columns or re-save a PDF.

Use the day you downloaded the file for `--retrieved-on`, and the exact
address you downloaded it from for `--url` / `--source-url`.

## What each download unblocks

The readiness gate (`python3 india/backtest.py readiness`) lists every
mandatory check that is not passing. Downloads below map onto them:

| gate check | needs |
| --- | --- |
| `benchmark-tri` | section 1 |
| `identity-evidence`, `corporate-evidence` | section 2 (and the HDFC AMC notice in section 5) |
| `official-calendar` | section 3, for every year 2022 to 2026 |
| `distributions-evidence`, `eligible-candidates` | section 4 |
| `category-evidence` | section 6 |
| `cost-coverage`, `benchmark-niftybees` | section 5 plus broker and depository tariffs, each with effective dates |

## 1. NIFTY 50 Total Returns Index (benchmark; currently BLOCKED)

- Official page: https://www.niftyindices.com/reports/historical-data
- Choose: index type **Total Returns Index**, index **NIFTY 50**.
- Range: **01-Jan-2018 to 27-Oct-2026** (the last BSE session in the study).
  For the separate NIFTYBEES-only experiment, also 01-Jan-2012 to 31-Dec-2017.
- Format: CSV. If the page offers only Excel, open it and "Save As" CSV
  (UTF-8) without changing any value. If the page limits the range, download
  consecutive ranges; import each one (they are merged, and two files that
  disagree on a date block the benchmark).
- Expected columns: a date column and a total-return value column (for
  example "Date" and "Total Returns Index"). If the names differ, pass
  `--date-col` and `--value-col`.

```bash
python3 india/tri.py validate ~/Downloads/NIFTY50_TRI.csv
python3 india/tri.py import ~/Downloads/NIFTY50_TRI.csv \
    --source-url https://www.niftyindices.com/reports/historical-data --retrieved-on YYYY-MM-DD
python3 india/tri.py status --need-from 2018-01-01 --need-to 2026-10-27 --against-sessions BSE
```

Done when `status` prints AVAILABLE and "the required window is covered".
Duplicate dates, missing or non-positive values, an HTML page saved as CSV,
or files that disagree are refused.

## 2. NSE circulars (identity and corporate actions)

Open each from NSE's website in a browser and save the PDF unchanged.

| circular | expected content | needed for |
| --- | --- | --- |
| https://nsearchives.nseindia.com/content/circulars/CML42888.pdf | Nippon India ETF face-value change: old and new ISINs, effective from the ex-date | NIFTYBEES (590103) and BANKBEES (590106) ISIN links, now only corroborated |
| https://nsearchives.nseindia.com/content/circulars/CMPT42889.pdf | Record date and ex-date for the same change | ex-date of the 2019 split |
| https://nsearchives.nseindia.com/content/circulars/CMPT47359.pdf | HDFC Nifty ETF face-value split: record date, ex-date, face values | HDFCNIFTY (539516) split, now unverified |
| https://nsearchives.nseindia.com/content/circulars/CML47344.pdf | New ISIN for HDFC Nifty ETF | HDFCNIFTY ISIN link |

```bash
python3 india/evidence.py import ~/Downloads/CML42888.pdf \
    --url https://nsearchives.nseindia.com/content/circulars/CML42888.pdf --retrieved-on YYYY-MM-DD \
    --title "NSE circular CML42888" --publisher "National Stock Exchange of India"
# likewise for CMPT42889, CMPT47359, CML47344
python3 india/evidence.py list
python3 india/evidence.py check
```

A file that is not a PDF (an error or block page saved as .pdf) is refused.
After import, the documents are read and `identity_confirmations.json` /
`corporate_actions.json` are updated only with what the text states: a link
becomes "verified" only if one document states both the old and the new
ISIN, and a split's units multiplier only from its stated face values.

## 3. BSE trading-holiday notices (official calendar)

The 2025 notice (20241213-30) is imported and matches every 2025 weekday
without a price file. For 2018-2024 and 2026, find each notice on
https://www.bseindia.com (Notices, search "Trading Holidays for the Calendar
Year") and note its number (format YYYYMMDD-NN). Then either send the
numbers, or run:

```bash
python3 india/calendar_in.py bse-notice NOTICE_NUMBER
python3 india/calendar_in.py coverage BSE 2018-01-01 2026-10-27
```

Holiday lists do not block ETF eligibility; they turn "no-file" days into
confirmed holidays and expose any day where a file and a holiday disagree.

## 4. Distribution (dividend / IDCW) history of the five candidates

Needed for every remaining candidate; for NIFTY1, NIFTYIETF and JUNIORBEES
it is the only open criterion. For each ETF, the AMC's official record of
every distribution (IDCW / dividend) declared from 2018-01-01 to 2026-10-27,
or an official statement that none was declared in that period: the AMC's
website (scheme page, "IDCW history" or notices) or its filings to BSE.

| ETF | BSE code | ISIN(s) | AMC |
| --- | --- | --- | --- |
| NIFTYBEES | 590103 | INF732E01011, INF204KB14I2 | Nippon Life India Asset Management |
| JUNIORBEES | 590104 | INF732E01045 | Nippon Life India Asset Management |
| NIFTYIETF | 537007 | INF109K012R6 | ICICI Prudential Asset Management |
| NIFTY1 | 537708 | INF174K014P6 | Kotak Mahindra Asset Management |
| HDFCNIFTY | 539516 | INF179KB1KP3, INF179KC1965 | HDFC Asset Management |

Import each document with `evidence.py import`. A `distributions-reviewed`
record is then added to `corporate_actions.json` only from what the
documents state, listing every distribution with its record date.

## 5. Tax and statutory charges (for a later phase; no values recorded)

India Code, the income-tax site and the e-Gazette refuse or do not answer
this environment. Download from https://www.indiacode.nic.in or
https://egazette.gov.in:

- Finance (No. 2) Act, 2024 (capital-gains amendments, sections 111A/112A).
- Finance (No. 2) Act, 2004, Chapter VII (Securities Transaction Tax), and
  the amending Finance Acts for each year 2018-2026.
- Finance Act, 2019 (Indian Stamp Act amendments) and the Indian Stamp
  (Collection of Stamp-Duty through Stock Exchanges, Clearing Corporations
  and Depositories) Rules, 2019, with their commencement notification.
- Whether a newer Income-tax Act governs any year of the window.

Optional, for the HDFC event: HDFC AMC's own notice on the February 2021
face-value change of HDFC Nifty ETF (hdfcfund.com refuses this environment).

```bash
python3 india/evidence.py import FILE.pdf --url https://... --retrieved-on YYYY-MM-DD \
    --title "..." --publisher "..."
```

## 6. Fund-category evidence dated before 2022 (universe)

The only category evidence on record is AMFI's current NAVAll.txt, which is
undated and therefore counts only from its retrieval date (2026-10-28). The
point-in-time universe needs, for each candidate, a document dated on or
before 2022-01-01 that names the index the ETF tracks: the scheme
information document or key information memorandum in force then (from the
AMC's site or SEBI's filings page), or an archived AMFI scheme list dated
before 2022. Import each with `evidence.py import`. Note: today
`universe.Evidence` reads category facts only from AMFI's list; a small
code change is needed to read dated category records from these documents.
