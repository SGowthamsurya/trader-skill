#!/usr/bin/env python3
"""Import and validate a NIFTY 50 Total Returns Index file. Standard library only.

The TRI history cannot be fetched automatically (sources.json,
nifty50-tri-history), so the user downloads it from niftyindices.com in a
browser, saves it as CSV, and imports it here. The import records where it
came from, when, and its SHA-256, and refuses a file that fails validation.
Until a valid file is imported, TRI benchmarking is BLOCKED. Nothing here
estimates a TRI from a price index and dividend yields.

    python3 tri.py status --need-from 2018-01-01 --need-to 2026-10-27 --against-sessions BSE
    python3 tri.py validate ~/Downloads/NIFTY50_TRI.csv
    python3 tri.py import ~/Downloads/NIFTY50_TRI.csv \\
        --source-url https://www.niftyindices.com/reports/historical-data \\
        --retrieved-on 2026-10-29
    python3 tri.py import FILE ... --date-col "Date" --value-col "Total Returns Index"

Validation. Errors refuse the import:

  structure     no data rows, no date column or no value column found, or
                more than one candidate (then name it with --date-col /
                --value-col)
  dates         a date that does not parse in one consistent format (day
                first: 02 Jan 2018, 02-Jan-2018, 02-01-2018, 2018-01-02,
                02/01/2018), or the same date twice
  values        a value that is not a positive finite number

Warnings are reported and recorded but do not refuse the file:

  gap           more than --max-gap-days calendar days between two rows
  move          a day-to-day change larger than --max-move (a fraction)
  coverage      the file starts more than --max-gap-days after --want-from
                (default 2018-01-01, the primary evaluation window); the
                first trading day of a window is rarely its first date

The thresholds are data-sanity checks for a broken file, not claims about
how the index behaves.

Several files may be imported (if the download page limits the date range,
import each range). They are merged by date. The same file imported twice
counts once; two files that give different values for one date make TRI
benchmarking BLOCKED until the wrong file is identified.
"""

import argparse
import csv
import datetime
import io
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fetch  # noqa: E402

SOURCE_ID = "nifty50-tri-history"
DATE_FORMATS = ("%d %b %Y", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")
MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULTS = {"max_gap_days": 10, "max_move": 0.15, "want_from": "2018-01-01"}


class TriError(Exception):
    pass


def _norm(h):
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def pick_columns(header, date_col=None, value_col=None):
    """(date column, value column) from the header, or a TriError that says what to pass."""
    def one(name, given, test):
        if given is not None:
            if given not in header:
                raise TriError("no column %r; the columns are: %s" % (given, ", ".join(header)))
            return given
        hits = [h for h in header if test(_norm(h))]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise TriError("no %s column found in: %s. Name it with --%s" % (name, ", ".join(header), name.replace(" ", "-")))
        raise TriError("more than one %s column (%s). Name it with --%s" % (name, ", ".join(hits), name.replace(" ", "-")))

    d = one("date col", date_col, lambda n: n == "date" or n.endswith(" date") or n.startswith("date "))
    v = one("value col", value_col, lambda n: "total return" in n or n == "tri" or n.endswith(" tri"))
    if d == v:
        raise TriError("the date and value columns are the same column %r" % d)
    return d, v


def parse_dates(raw):
    """The one format, from DATE_FORMATS, that reads every date; and the dates."""
    for fmt in DATE_FORMATS:
        try:
            return fmt, [datetime.datetime.strptime(x.strip(), fmt).date() for x in raw]
        except ValueError:
            continue
    bad = raw[0]
    for x in raw:
        if not any(_parses(x, f) for f in DATE_FORMATS):
            bad = x
            break
    raise TriError("dates are not in one consistent day-first format (first problem: %r)" % bad)


def _parses(x, fmt):
    try:
        datetime.datetime.strptime(x.strip(), fmt)
        return True
    except ValueError:
        return False


def parse_value(x):
    try:
        v = float(x.replace(",", "").strip())
    except (AttributeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def validate_text(text, date_col=None, value_col=None, max_gap_days=None, max_move=None, want_from=None):
    """Parse and check a TRI CSV. Returns a report; raises TriError on any error."""
    max_gap_days = DEFAULTS["max_gap_days"] if max_gap_days is None else max_gap_days
    max_move = DEFAULTS["max_move"] if max_move is None else max_move
    want_from = DEFAULTS["want_from"] if want_from is None else want_from
    rd = csv.reader(io.StringIO(text))
    rows = [r for r in rd if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise TriError("the file has no data rows")
    header = [h.strip() for h in rows[0]]
    dcol, vcol = pick_columns(header, date_col, value_col)
    di, vi = header.index(dcol), header.index(vcol)
    body = rows[1:]
    short = [n for n, r in enumerate(body, 2) if len(r) <= max(di, vi)]
    if short:
        raise TriError("line %d has too few columns" % short[0])
    fmt, dates = parse_dates([r[di] for r in body])
    values = []
    for n, r in enumerate(body, 2):
        v = parse_value(r[vi])
        if v is None:
            raise TriError("line %d: %r is not a positive number" % (n, r[vi]))
        values.append(v)
    series = sorted(zip(dates, values))
    for (a, _), (b, _) in zip(series, series[1:]):
        if a == b:
            raise TriError("the date %s appears twice" % a.isoformat())
    warnings = []
    for (a, x), (b, y) in zip(series, series[1:]):
        if (b - a).days > max_gap_days:
            warnings.append("gap: %d days between %s and %s" % ((b - a).days, a, b))
        if abs(y / x - 1) > max_move:
            warnings.append("move: %.1f%% from %s to %s" % ((y / x - 1) * 100, a, b))
    first, last = series[0][0], series[-1][0]
    if (first - datetime.date.fromisoformat(want_from)).days > max_gap_days:
        warnings.append("coverage: starts %s, well after the wanted %s" % (first, want_from))
    return {"rows": len(series), "first": first.isoformat(), "last": last.isoformat(),
            "date_col": dcol, "value_col": vcol, "date_format": fmt,
            "newest_first": dates[0] > dates[-1], "warnings": warnings,
            "series": [(d.isoformat(), v) for d, v in series]}


def read_file(path):
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise TriError("cannot read %s: %s" % (path, e))
    if size > MAX_FILE_BYTES:
        raise TriError("%s is %d bytes; a TRI CSV is far smaller, so this is the wrong file" % (path, size))
    with open(path, "rb") as fh:
        data = fh.read()
    why = fetch.sniff(data, "csv")
    if why:
        raise TriError("%s: %s (export the history as CSV)" % (path, why))
    try:
        return data, data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TriError("%s is not UTF-8 text" % path)


def import_file(path, cache, source_url, retrieved_on, **opts):
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise TriError("--source-url must be the https page the file was downloaded from")
    try:
        datetime.date.fromisoformat(retrieved_on)
    except (TypeError, ValueError):
        raise TriError("--retrieved-on must be YYYY-MM-DD")
    data, text = read_file(path)
    rep = validate_text(text, **opts)
    summary = {k: v for k, v in rep.items() if k != "series"}
    return cache.put(data, {"kind": "manual-import", "ok": True, "source": SOURCE_ID,
                            "source_url": source_url, "retrieved_on": retrieved_on,
                            "imported_at": fetch.utc_now(), "filename": os.path.basename(path),
                            "validation": summary})


def _merged(cache):
    """(files, {date: value}) over every valid import, or TriError."""
    files, seen, merged = [], set(), {}
    for rec in cache.records():
        if rec.get("source") != SOURCE_ID or rec.get("kind") != "manual-import" or not rec.get("ok"):
            continue
        if rec["sha256"] in seen:
            continue
        seen.add(rec["sha256"])
        try:
            text = cache.read(rec["sha256"]).decode("utf-8-sig")
        except fetch.FetchError as e:
            raise TriError("the imported file %s failed its checksum: %s" % (rec.get("filename"), e))
        v = rec["validation"]
        for d, x in validate_text(text, date_col=v["date_col"], value_col=v["value_col"])["series"]:
            if d in merged and abs(merged[d] - x) > 1e-9 * max(abs(x), 1.0):
                raise TriError("imported files disagree on %s (%s vs %s)" % (d, merged[d], x))
            merged[d] = x
        files.append(rec)
    return files, merged


def status(cache):
    """('BLOCKED', why) or ('AVAILABLE', summary of the merged imports), re-checking every stored file."""
    try:
        files, merged = _merged(cache)
    except TriError as e:
        return "BLOCKED", str(e)
    if not files:
        return "BLOCKED", "no NIFTY 50 TRI file has been imported; download it and run tri.py import"
    days = sorted(merged)
    return "AVAILABLE", {"files": files, "rows": len(days), "first": days[0], "last": days[-1]}


def coverage(series_dates, need_from, need_to, sessions=None, tolerance_days=None):
    """Plain findings on how the merged series covers the required window."""
    tol = DEFAULTS["max_gap_days"] if tolerance_days is None else tolerance_days
    out = []
    first, last = min(series_dates), max(series_dates)
    if need_from and (datetime.date.fromisoformat(first) - datetime.date.fromisoformat(need_from)).days > tol:
        out.append("starts %s, after the required %s" % (first, need_from))
    if need_to and last < need_to:
        out.append("ends %s, before the required %s" % (last, need_to))
    if sessions:
        have = set(series_dates)
        missing = [d for d in sessions if (not need_from or d >= need_from) and (not need_to or d <= need_to)
                   and first <= d <= last and d not in have]
        if missing:
            out.append("%d exchange sessions inside the covered range have no TRI value (first: %s)"
                       % (len(missing), ", ".join(missing[:5])))
    return out


def load_series(cache):
    """The merged TRI as [(date, value)], oldest first. TriError while BLOCKED."""
    st, info = status(cache)
    if st != "AVAILABLE":
        raise TriError("TRI benchmarking is BLOCKED: %s" % info)
    return sorted(_merged(cache)[1].items())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import and validate the NIFTY 50 TRI history.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("status")
    ps.add_argument("--need-from", default=DEFAULTS["want_from"])
    ps.add_argument("--need-to")
    ps.add_argument("--against-sessions", choices=("BSE", "NSE"),
                    help="also list exchange sessions (from the price files) with no TRI value")
    for name in ("validate", "import"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--date-col")
        p.add_argument("--value-col")
        p.add_argument("--max-gap-days", type=int)
        p.add_argument("--max-move", type=float)
        p.add_argument("--want-from")
        if name == "import":
            p.add_argument("--source-url", required=True)
            p.add_argument("--retrieved-on", required=True, help="YYYY-MM-DD, the day you downloaded it")
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "status":
            st, info = status(cache)
            if st == "BLOCKED":
                print("TRI benchmarking: BLOCKED. %s" % info)
                return 1
            print("TRI benchmarking: AVAILABLE. %d rows %s to %s from %d file(s):"
                  % (info["rows"], info["first"], info["last"], len(info["files"])))
            for rec in info["files"]:
                print("  %s sha256 %s, retrieved %s from %s" % (rec["filename"], rec["sha256"], rec["retrieved_on"],
                                                               rec["source_url"]))
            sessions = None
            if a.against_sessions:
                import calendar_in
                cal = calendar_in.from_cache(cache, a.against_sessions)
                sessions = [d.isoformat() for d in cal.sessions(info["first"], info["last"])]
            gaps = coverage([d for d, _ in load_series(cache)], a.need_from, a.need_to, sessions)
            for g in gaps:
                print("COVERAGE " + g)
            if not gaps:
                print("Coverage: the required window is covered.")
            return 0 if not gaps else 2
        opts = {"date_col": a.date_col, "value_col": a.value_col, "max_gap_days": a.max_gap_days,
                "max_move": a.max_move, "want_from": a.want_from}
        if a.cmd == "validate":
            rep = validate_text(read_file(a.file)[1], **opts)
        else:
            rec = import_file(a.file, cache, a.source_url, a.retrieved_on, **opts)
            rep = rec["validation"]
            print("Imported, sha256 %s" % rec["sha256"])
        print("%d rows, %s to %s; date column %r (%s), value column %r."
              % (rep["rows"], rep["first"], rep["last"], rep["date_col"], rep["date_format"], rep["value_col"]))
        for w in rep["warnings"]:
            print("WARNING " + w)
    except (TriError, fetch.FetchError) as e:
        print("tri.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
