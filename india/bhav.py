#!/usr/bin/env python3
"""Read exchange bhavcopies (daily price files) and collect them politely.

Three layouts are read, each recognised by its header, as seen in real files
on 2026-10-28:

  nse-legacy    SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE,
                TOTTRDQTY, TOTTRDVAL, TIMESTAMP (02-JAN-2020), TOTALTRADES, ISIN
  bse-legacy    SC_CODE, SC_NAME, SC_GROUP, SC_TYPE, OPEN, HIGH, LOW, CLOSE,
                LAST, PREVCLOSE, NO_TRADES, NO_OF_SHRS, NET_TURNOV, ISIN_CODE,
                TRADING_DATE (02-Jan-18)
  udiff         TradDt (2026-10-27), Src (NSE or BSE), FinInstrmId, ISIN,
                TckrSymb, SctySrs, FinInstrmNm, OpnPric, HghPric, LwPric,
                ClsPric, PrvsClsgPric, TtlTradgVol, TtlTrfVal,
                TtlNbOfTxsExctd, NewBrdLotQty

Every row becomes the same record: date, exchange, code (BSE scrip code or
NSE instrument id; None in the NSE legacy file), symbol, series, name,
isin, open, high, low, close, prev_close, volume (units), value (rupees),
trades, lot. Every row must carry the date that was asked for; a file whose
own date differs is refused. A bhavcopy lists what traded that day, so an
instrument missing from one is "no trade recorded", not "delisted".

    python3 bhav.py show BSE 2018-01-02 --isin INF732E01011
    python3 bhav.py sample BSE --from 2018-01 --to 2026-10       # first session each month
    python3 bhav.py sample BSE --daily 2019-11-25 2019-12-20       # every weekday in a range

Sampling skips Saturdays and Sundays to save requests. That is a choice
about where to look, not a claim that the market was shut: calendar_in.py
never treats a weekend as closed without an official record.
"""

import argparse
import csv
import datetime
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fetch  # noqa: E402
import sources as src  # noqa: E402

# the price sources per exchange, newest layout first
VENUES = {"NSE": ("nse-bhavcopy-udiff", "nse-bhavcopy-legacy"),
          "BSE": ("bse-bhavcopy-udiff", "bse-bhavcopy-isin")}
SOURCE_VENUE = {s: v for v, ss in VENUES.items() for s in ss}
MAX_MEMBER = 256 * 1024 * 1024

LAYOUTS = {
    "nse-legacy": {"need": ("SYMBOL", "SERIES", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TOTTRDVAL", "TIMESTAMP", "ISIN"),
                   "date": ("TIMESTAMP", "%d-%b-%Y"), "exchange": "NSE",
                   "map": {"code": None, "symbol": "SYMBOL", "series": "SERIES", "name": None, "isin": "ISIN",
                           "open": "OPEN", "high": "HIGH", "low": "LOW", "close": "CLOSE",
                           "prev_close": "PREVCLOSE", "volume": "TOTTRDQTY", "value": "TOTTRDVAL",
                           "trades": "TOTALTRADES", "lot": None}},
    "bse-legacy": {"need": ("SC_CODE", "SC_NAME", "CLOSE", "PREVCLOSE", "NO_OF_SHRS", "NET_TURNOV", "ISIN_CODE",
                            "TRADING_DATE"),
                   "date": ("TRADING_DATE", "%d-%b-%y"), "exchange": "BSE",
                   "map": {"code": "SC_CODE", "symbol": "SC_NAME", "series": "SC_GROUP", "name": "SC_NAME",
                           "isin": "ISIN_CODE", "open": "OPEN", "high": "HIGH", "low": "LOW", "close": "CLOSE",
                           "prev_close": "PREVCLOSE", "volume": "NO_OF_SHRS", "value": "NET_TURNOV",
                           "trades": "NO_TRADES", "lot": None}},
    "udiff": {"need": ("TradDt", "Src", "FinInstrmId", "ISIN", "TckrSymb", "SctySrs", "ClsPric", "PrvsClsgPric",
                       "TtlTradgVol", "TtlTrfVal"),
              "date": ("TradDt", "%Y-%m-%d"), "exchange": "Src",
              "map": {"code": "FinInstrmId", "symbol": "TckrSymb", "series": "SctySrs", "name": "FinInstrmNm",
                      "isin": "ISIN", "open": "OpnPric", "high": "HghPric", "low": "LwPric", "close": "ClsPric",
                      "prev_close": "PrvsClsgPric", "volume": "TtlTradgVol", "value": "TtlTrfVal",
                      "trades": "TtlNbOfTxsExctd", "lot": "NewBrdLotQty"}},
}
NUMERIC = ("open", "high", "low", "close", "prev_close", "volume", "value", "trades", "lot")

class BhavError(Exception):
    pass

def unpack(data):
    """The CSV text inside a bhavcopy (zipped or not)."""
    if data[:4] == b"PK\x03\x04":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise BhavError("a broken zip archive: %s" % e)
        members = [i for i in z.infolist() if i.filename.lower().endswith(".csv")]
        if len(members) != 1:
            raise BhavError("expected one CSV in the archive, found %d" % len(members))
        if members[0].file_size > MAX_MEMBER:
            raise BhavError("the CSV in the archive is implausibly large")
        data = z.read(members[0])
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue

def _num(x):
    x = (x or "").strip().replace(",", "")
    if not x:
        return None
    try:
        return float(x)
    except ValueError:
        return None

def parse(data, want_date=None):
    """(layout, rows) for one bhavcopy, every row checked against want_date."""
    text = unpack(data)
    rd = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (rd.fieldnames or [])]
    rd.fieldnames = header
    layout = None
    for name, lay in LAYOUTS.items():
        if all(c in header for c in lay["need"]):
            layout = name
            break
    if not layout:
        raise BhavError("not a bhavcopy layout this tool knows; header starts %s" % ", ".join(header[:6]))
    lay = LAYOUTS[layout]
    dcol, dfmt = lay["date"]
    want = None if want_date is None else str(want_date)
    rows = []
    for n, r in enumerate(rd, 2):
        if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
            continue
        try:
            d = datetime.datetime.strptime(r[dcol].strip(), dfmt).date().isoformat()
        except (ValueError, AttributeError):
            raise BhavError("line %d: %s %r is not a date" % (n, dcol, r.get(dcol)))
        if want and d != want:
            raise BhavError("line %d is dated %s, but this file was fetched for %s" % (n, d, want))
        rec = {"date": d}
        rec["exchange"] = (r["Src"].strip() if lay["exchange"] == "Src" else lay["exchange"])
        for k, col in lay["map"].items():
            v = r.get(col) if col else None
            rec[k] = _num(v) if k in NUMERIC else (v.strip() if isinstance(v, str) and v.strip() else None)
        if rec["exchange"] not in ("NSE", "BSE"):
            raise BhavError("line %d: unknown exchange %r" % (n, rec["exchange"]))
        rows.append(rec)
    if not rows:
        raise BhavError("the file has no rows")
    dates = {r["date"] for r in rows}
    if len(dates) != 1:
        raise BhavError("the file mixes dates: %s" % ", ".join(sorted(dates)))
    return layout, rows

def observations(cache, venue=None, verify=True):
    """[(record, layout, rows)] for every price file in the cache, oldest first."""
    out = []
    for r in cache.records():
        if not r.get("ok") or r.get("source") not in SOURCE_VENUE or not r.get("sha256"):
            continue
        if venue and SOURCE_VENUE[r["source"]] != venue:
            continue
        layout, rows = parse(cache.read(r["sha256"]), r.get("date") if verify else None)
        out.append((r, layout, rows))
    out.sort(key=lambda x: x[2][0]["date"])
    return out

def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += datetime.timedelta(days=1)

def fetch_session(fetcher, venue, day, prefer):
    """The first source of this venue that has a file for this day, or None.
    prefer is a one-item list holding the source that worked last."""
    order = list(VENUES[venue])
    if prefer[0] in order:
        order.remove(prefer[0])
        order.insert(0, prefer[0])
    for sid in order:
        try:
            rec = fetcher.get(sid, day.isoformat())
        except fetch.FetchError as e:
            if e.reason == "network":
                raise
            continue
        parse(fetcher.cache.read(rec["sha256"]), day.isoformat())
        prefer[0] = sid
        return rec
    return None

def months(a, b):
    y, m = a
    while (y, m) <= b:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

def main(argv=None):
    ap = argparse.ArgumentParser(description="Read and collect NSE/BSE bhavcopies.")
    ap.add_argument("--registry", default=src.DEFAULT_PATH)
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("show")
    ps.add_argument("venue", choices=sorted(VENUES))
    ps.add_argument("date")
    ps.add_argument("--isin", action="append", default=[])
    pm = sub.add_parser("sample")
    pm.add_argument("venue", choices=sorted(VENUES))
    pm.add_argument("--from", dest="start", help="YYYY-MM, first month")
    pm.add_argument("--to", dest="end", help="YYYY-MM, last month")
    pm.add_argument("--daily", nargs=2, metavar=("FROM", "TO"), help="every weekday FROM..TO (YYYY-MM-DD)")
    pm.add_argument("--max-tries", type=int, default=6, help="weekdays tried per month before giving up")
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "show":
            for r, layout, rows in observations(cache, a.venue):
                if rows[0]["date"] != a.date:
                    continue
                for x in rows:
                    if not a.isin or x["isin"] in a.isin:
                        print(json.dumps(x, sort_keys=True))
            return 0
        f = fetch.Fetcher(src.read(a.registry), cache)
        prefer = [None]
        got = missed = 0
        if a.daily:
            days = list(weekdays(datetime.date.fromisoformat(a.daily[0]), datetime.date.fromisoformat(a.daily[1])))
            for d in days:
                ok = fetch_session(f, a.venue, d, prefer)
                got, missed = got + bool(ok), missed + (not ok)
                print("%s %s" % (d, ok["source"] if ok else "no file"))
        else:
            if not (a.start and a.end):
                raise BhavError("give --from and --to, or --daily")
            ya, ma = map(int, a.start.split("-"))
            yb, mb = map(int, a.end.split("-"))
            for y, m in months((ya, ma), (yb, mb)):
                first = datetime.date(y, m, 1)
                found = None
                for n, d in enumerate(weekdays(first, first + datetime.timedelta(days=13))):
                    if n >= a.max_tries:
                        break
                    found = fetch_session(f, a.venue, d, prefer)
                    if found:
                        break
                got, missed = got + bool(found), missed + (not found)
                print("%04d-%02d %s" % (y, m, ("%s %s" % (found["date"], found["source"])) if found else "NONE"))
        print("%d with a file, %d without." % (got, missed))
    except (BhavError, fetch.FetchError, src.SourceError) as e:
        print("bhav.py: %s" % e, file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
