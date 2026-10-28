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
    python3 bhav.py collect BSE --from 2018-01-01 --to 2026-10-27 --max-requests 200
    python3 bhav.py status BSE --from 2018-01-01 --to 2026-10-27

collect is resumable: the manifest is its state. A weekday is resolved when
a valid file for it is cached, or when every layout of the venue refused
it. Only unresolved weekdays are asked for, layouts in the order the
evidence says they were published, until --max-requests network requests
have been made or --max-network-failures network failures happen in a row.
A file that downloads but does not parse (wrong date, unknown layout, no
rows) is recorded as invalid: an error, not a non-trading day.

Reading every full-market file again for each report takes minutes, so a
derived index is kept beside the cache (<cache>/derived/v1/), one JSON per
raw file and named by that file's SHA-256, so it can never describe other
bytes: the verified date, the layout, every fund row (ISIN starting INF) in
full, and the listing key and ISIN of every row. observations(...,
funds_only=True) reads it. Rows that only record a key's identity (a key
that was a fund at some point, seen here with a non-fund ISIN) are marked
identity_only and carry no prices, so a code reused between a share and an
ETF still shows up as an ISIN change. Rebuild it by deleting the folder.

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
# Layout order by date, from the Phase 2 evidence in sources.json: BSE's
# legacy ISIN file was last seen 2024-07-01, UDiFF first seen 2024-08-01.
SWITCH = {"BSE": ("2024-07-01", "2024-08-01")}

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
INDEX_VERSION = 1

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
    seen_dates = {}  # every row of a file carries the same few date strings; parse each once
    rows = []
    for n, r in enumerate(rd, 2):
        if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
            continue
        raw = r.get(dcol)
        d = seen_dates.get(raw)
        if d is None:
            try:
                d = datetime.datetime.strptime(raw.strip(), dfmt).date().isoformat()
            except (ValueError, AttributeError):
                raise BhavError("line %d: %s %r is not a date" % (n, dcol, raw))
            seen_dates[raw] = d
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

def invalid_shas(records):
    return {r["sha256"] for r in records if r.get("kind") == "invalid" and r.get("sha256")}


def _key(row):
    return row["code"] if row["exchange"] == "BSE" else row["symbol"]


def index_path(cache, sha):
    return os.path.join(cache.root, "derived", "v%d" % INDEX_VERSION, sha[:2], sha + ".json")


def summary(cache, rec):
    """The derived index entry for one valid price file, built from its bytes on first use."""
    p = index_path(cache, rec["sha256"])
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            s = json.load(fh)
        if s.get("v") == INDEX_VERSION and s.get("sha256") == rec["sha256"]:
            return s
    layout, rows = parse(cache.read(rec["sha256"]), rec.get("date"))
    s = {"v": INDEX_VERSION, "sha256": rec["sha256"], "date": rows[0]["date"], "layout": layout, "rows": len(rows),
         "funds": [x for x in rows if (x["isin"] or "").startswith("INF")],
         "keys": [[x["exchange"], _key(x), x["series"], x["isin"]] for x in rows if x["isin"]]}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, separators=(",", ":"))
    os.replace(tmp, p)
    return s


def _price_records(cache, venue):
    recs = cache.records()
    bad = invalid_shas(recs)
    return [r for r in recs if r.get("ok") and r.get("source") in SOURCE_VENUE and r.get("sha256")
            and r["sha256"] not in bad and (not venue or SOURCE_VENUE[r["source"]] == venue)]


def observations(cache, venue=None, verify=True, funds_only=False):
    """[(record, layout, rows)] for every valid price file in the cache, oldest first.
    The record carries the file's verified date. funds_only reads the derived
    index: fund rows in full, plus identity-only rows for keys that were ever
    a fund."""
    out = []
    if not funds_only:
        for r in _price_records(cache, venue):
            layout, rows = parse(cache.read(r["sha256"]), r.get("date") if verify else None)
            out.append((dict(r, date=rows[0]["date"]), layout, rows))
    else:
        sums = [(r, summary(cache, r)) for r in _price_records(cache, venue)]
        fund_keys = {(x["exchange"], _key(x)) for _, s in sums for x in s["funds"]}
        for r, s in sums:
            rows = list(s["funds"])
            for ex, key, series, isin in s["keys"]:
                if (ex, key) in fund_keys and not isin.startswith("INF"):
                    rows.append({"date": s["date"], "exchange": ex, "code": key if ex == "BSE" else None,
                                 "symbol": key if ex == "NSE" else None, "series": series, "name": None,
                                 "isin": isin, "identity_only": True,
                                 "open": None, "high": None, "low": None, "close": None, "prev_close": None,
                                 "volume": None, "value": None, "trades": None, "lot": None})
            out.append((dict(r, date=s["date"]), s["layout"], rows))
    out.sort(key=lambda x: x[0]["date"])
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

def layout_order(venue, day):
    """The venue's sources, the one published on this day first."""
    order = list(VENUES[venue])
    sw = SWITCH.get(venue)
    if sw and day <= sw[0]:
        order.reverse()  # legacy layout first
    return order


def resolution(records, venue):
    """({date: sha256} of valid files, {date: sources that refused}, {date: sources whose file was invalid})."""
    bad = invalid_shas(records)
    ok, refused, invalid = {}, {}, {}
    for r in records:
        if SOURCE_VENUE.get(r.get("source")) != venue or not r.get("date"):
            continue
        if r.get("kind") == "invalid":
            invalid.setdefault(r["date"], set()).add(r["source"])
        elif r.get("ok") and r.get("sha256") and r["sha256"] not in bad:
            ok[r["date"]] = r["sha256"]
        elif not r.get("ok") and r.get("reason") in fetch.REFUSALS:
            refused.setdefault(r["date"], set()).add(r["source"])
    return ok, refused, invalid


def documented_gaps(records, venue, gaps_path=None):
    """{date: sha256} of gaps in india/data_gaps.json whose checksum matches an invalid file on record.
    The same rule calendar_in applies, so both tools classify a day alike."""
    import calendar_in  # imported here: calendar_in imports this module
    listed = {d.isoformat(): sha for d, sha in calendar_in.read_gaps(gaps_path or calendar_in.GAPS, venue).items()}
    seen = {}
    for r in records:
        if r.get("kind") == "invalid" and SOURCE_VENUE.get(r.get("source")) == venue and r.get("date"):
            seen.setdefault(r["date"], set()).add(r.get("sha256"))
    return {d: sha for d, sha in listed.items() if sha in seen.get(d, set())}


def classify(records, venue, start, end, gaps_path=None):
    """{weekday: class} with class one of: valid-file, refused-all-layouts, documented-gap,
    invalid-undocumented, unresolved. A documented gap is accounted for but stays unresolved data."""
    ok, refused, invalid = resolution(records, venue)
    gaps = documented_gaps(records, venue, gaps_path)
    every = set(VENUES[venue])
    out = {}
    for d in (x.isoformat() for x in weekdays(start, end)):
        if d in ok:
            out[d] = "valid-file"
        elif d in gaps:
            out[d] = "documented-gap"
        elif d in invalid:
            out[d] = "invalid-undocumented"
        elif refused.get(d, set()) >= every:
            out[d] = "refused-all-layouts"
        else:
            out[d] = "unresolved"
    return out


def unresolved(records, venue, start, end, gaps_path=None):
    """Weekdays with no valid file, no refusal in every layout and no documented gap."""
    return [d for d, c in classify(records, venue, start, end, gaps_path).items()
            if c in ("unresolved", "invalid-undocumented")]


def collect(fetcher, venue, start, end, max_requests, max_network_failures=3, log=None):
    """Fetch unresolved weekdays until the request budget is spent. Resumable."""
    cache = fetcher.cache
    n0 = len(cache.records())
    out = {"requests": 0, "files": 0, "refused": 0, "invalid": 0, "network": 0, "stopped": None}
    streak = 0
    for day in unresolved(cache.records(), venue, start, end):
        _, refused, invalid = resolution(cache.records(), venue)
        for sid in layout_order(venue, day):
            if sid in refused.get(day, ()) or sid in invalid.get(day, ()):
                continue  # already answered; an invalid file waits for a person, not a re-fetch
            if len(cache.records()) - n0 >= max_requests:
                out["stopped"] = "request budget"
                break
            try:
                rec = fetcher.get(sid, day)
            except fetch.FetchError as e:
                if e.reason == "network":
                    out["network"] += 1
                    streak += 1
                    if streak >= max_network_failures:
                        out["stopped"] = "%d network failures in a row" % streak
                        break
                    continue
                out["refused"] += 1
                streak = 0
                continue
            streak = 0
            try:
                parse(cache.read(rec["sha256"]), day)
            except BhavError as e:
                cache.note({"kind": "invalid", "ok": False, "source": sid, "url": rec["url"], "date": day,
                            "sha256": rec["sha256"], "detail": str(e), "retrieved_at": fetch.utc_now()})
                out["invalid"] += 1
                continue
            out["files"] += 1
            if log:
                log("%s %s" % (day, sid))
            break
        if out["stopped"]:
            break
    out["requests"] = len(cache.records()) - n0 - out["invalid"]
    out["unresolved"] = len(unresolved(cache.records(), venue, start, end))
    return out


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
    for name in ("collect", "status"):
        pc = sub.add_parser(name)
        pc.add_argument("venue", choices=sorted(VENUES))
        pc.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
        pc.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
        if name == "collect":
            pc.add_argument("--max-requests", type=int, required=True)
            pc.add_argument("--max-network-failures", type=int, default=3)
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "show":
            for r, layout, rows in observations(cache, a.venue):
                if r["date"] != a.date:
                    continue
                for x in rows:
                    if not a.isin or x["isin"] in a.isin:
                        print(json.dumps(x, sort_keys=True))
            return 0
        if a.cmd in ("collect", "status"):
            start, end = datetime.date.fromisoformat(a.start), datetime.date.fromisoformat(a.end)
            if a.cmd == "status":
                cls = classify(cache.records(), a.venue, start, end)
                n = {k: sum(1 for c in cls.values() if c == k) for k in
                     ("valid-file", "refused-all-layouts", "documented-gap", "invalid-undocumented", "unresolved")}
                gaps = sorted(d for d, c in cls.items() if c == "documented-gap")
                print("%d weekdays: %d with a valid file, %d refused in every layout, %d documented data gaps "
                      "(excluded, unresolved, checksum-matched: %s), %d invalid files not documented, "
                      "%d not yet resolved" % (len(cls), n["valid-file"], n["refused-all-layouts"],
                                                n["documented-gap"], ", ".join(gaps) or "none",
                                                n["invalid-undocumented"], n["unresolved"]))
                return 0
            res = collect(fetch.Fetcher(src.read(a.registry), cache), a.venue, start, end, a.max_requests,
                          a.max_network_failures)
            print(json.dumps(res, sort_keys=True))
            return 0 if not res["network"] else 1
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
