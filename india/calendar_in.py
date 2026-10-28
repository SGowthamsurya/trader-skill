#!/usr/bin/env python3
"""Exchange calendar from evidence: what was observed, and what is official.

Two kinds of evidence are kept apart and never mixed:

  observed      a price file was fetched for the day and every row in it
                carries that date (bhav.py checks this). The day was a
                trading session. Nothing else is learned from files.
  official      an exchange holiday or special-session list, imported by
                hand with its source URL, retrieval date and SHA-256.

Every date gets one status:

  trading           an observed file
  official-holiday  on an imported official list, and no file contradicts it
  official-session  an official special session (Muhurat, a Saturday session)
                    that has no file yet
  no-file           every layout of this exchange's price file was asked for
                    and refused. Often a holiday, but a missing archive file
                    looks the same, so it is never called a holiday.
  unreachable       only network failures were recorded
  unobserved        nothing was asked
  conflict          an official holiday that has a trading file, or a file
                    dated differently from the day it was fetched for

Weekends are not assumed closed: a weekend is "unobserved" unless a file or
an official record says otherwise. Dates after the last observed session are
never predicted from past patterns; they stay "unobserved" until a file or
an official list covers them.

    python3 calendar_in.py status BSE 2019-12-18 2019-12-20
    python3 calendar_in.py coverage BSE 2018-01-01 2026-10-28
    python3 calendar_in.py import-official holidays.csv --exchange BSE \\
        --source-url https://... --retrieved-on 2026-10-29

An official list is a CSV with columns date (YYYY-MM-DD), type (holiday or
session) and description.
"""

import argparse
import csv
import datetime
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import bhav  # noqa: E402
import fetch  # noqa: E402

OFFICIAL = "official-calendar"
TYPES = ("holiday", "session")


class CalendarError(Exception):
    pass


def _d(x):
    if isinstance(x, datetime.date):
        return x
    try:
        return datetime.date.fromisoformat(x)
    except (TypeError, ValueError):
        raise CalendarError("dates are YYYY-MM-DD, not %r" % (x,))


def parse_official(text):
    """[(date, type, description)] from an official-list CSV, or CalendarError."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise CalendarError("the official list has no rows")
    for c in ("date", "type", "description"):
        if c not in rows[0]:
            raise CalendarError("the official list needs a %r column" % c)
    out, seen = [], set()
    for n, r in enumerate(rows, 2):
        d = _d((r["date"] or "").strip())
        t = (r["type"] or "").strip().lower()
        if t not in TYPES:
            raise CalendarError("line %d: type must be holiday or session, not %r" % (n, r["type"]))
        if d in seen:
            raise CalendarError("line %d: %s is listed twice" % (n, d))
        seen.add(d)
        out.append((d, t, (r["description"] or "").strip()))
    return out


def import_official(path, cache, exchange, source_url, retrieved_on):
    if exchange not in bhav.VENUES:
        raise CalendarError("exchange must be NSE or BSE")
    if not str(source_url).startswith("https://"):
        raise CalendarError("--source-url must be the https page the list came from")
    _d(retrieved_on)
    with open(path, "rb") as fh:
        data = fh.read()
    if fetch.looks_html(data):
        raise CalendarError("%s is a web page; save the list as CSV" % path)
    items = parse_official(data.decode("utf-8-sig"))
    return cache.put(data, {"kind": "manual-import", "ok": True, "source": OFFICIAL, "exchange": exchange,
                            "source_url": source_url, "retrieved_on": retrieved_on, "imported_at": fetch.utc_now(),
                            "filename": os.path.basename(path), "rows": len(items),
                            "first": min(i[0] for i in items).isoformat(), "last": max(i[0] for i in items).isoformat()})


class Calendar(object):
    def __init__(self, exchange, files, attempts, official=()):
        """files: {date: sha256} of verified price files;
        attempts: [(date, source, reason)] refusals and network failures;
        official: [(date, type, description, sha256)]."""
        self.exchange = exchange
        self.files = {_d(k): v for k, v in files.items()}
        self.sources = set(bhav.VENUES[exchange])
        self.refused, self.network = {}, set()
        for d, s, reason in attempts:
            d = _d(d)
            if reason == "network":
                self.network.add(d)
            else:
                self.refused.setdefault(d, set()).add(s)
        self.official = {}
        for d, t, desc, sha in official:
            self.official[_d(d)] = (t, desc, sha)
        self.last_observed = max(self.files) if self.files else None

    def status(self, day):
        d = _d(day)
        off = self.official.get(d)
        if d in self.files:
            return "conflict" if off and off[0] == "holiday" else "trading"
        if off:
            return "official-holiday" if off[0] == "holiday" else "official-session"
        if self.refused.get(d, set()) >= self.sources:
            return "no-file"
        if d in self.network:
            return "unreachable"
        return "unobserved"

    def days(self, start, end):
        d, end = _d(start), _d(end)
        while d <= end:
            yield d
            d += datetime.timedelta(days=1)

    def sessions(self, start, end):
        return [d for d in self.days(start, end) if self.status(d) == "trading"]

    def coverage(self, start, end):
        out = {"weekday": {}, "weekend": {}}
        for d in self.days(start, end):
            part = out["weekend" if d.weekday() >= 5 else "weekday"]
            s = self.status(d)
            part[s] = part.get(s, 0) + 1
        wd = out["weekday"]
        out["complete_weekdays"] = not any(wd.get(s) for s in ("unobserved", "unreachable", "conflict"))
        out["unconfirmed_closures"] = wd.get("no-file", 0)
        return out

    def adjacent(self, a, b):
        """(True, assumptions) if no session can lie strictly between a and b.
        Weekdays between must be no-file or official holidays; a weekend
        between is taken as closed unless an official record lists a session
        on it, and that assumption is returned so it can be reported."""
        a, b = _d(a), _d(b)
        assumptions = []
        for d in self.days(a + datetime.timedelta(days=1), b - datetime.timedelta(days=1)):
            s = self.status(d)
            if d.weekday() >= 5:
                if s in ("trading", "official-session", "conflict"):
                    return False, []
                if s != "official-holiday":
                    assumptions.append("no unrecorded weekend session on %s" % d)
            elif s == "no-file":
                assumptions.append("%s had no file (closure not officially confirmed)" % d)
            elif s != "official-holiday":
                return False, []
        return True, assumptions


def from_cache(cache, exchange):
    files = {r["date"] if r.get("date") else rows[0]["date"]: r["sha256"]
             for r, _, rows in bhav.observations(cache, exchange)}
    attempts = [(r["date"], r["source"], r.get("reason"))
                for r in cache.records()
                if r.get("kind") == "fetch" and not r.get("ok") and r.get("date")
                and bhav.SOURCE_VENUE.get(r.get("source")) == exchange]
    official = []
    for r in cache.records():
        if r.get("source") == OFFICIAL and r.get("exchange") == exchange and r.get("ok"):
            for d, t, desc in parse_official(cache.read(r["sha256"]).decode("utf-8-sig")):
                official.append((d, t, desc, r["sha256"]))
    return Calendar(exchange, files, attempts, official)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exchange calendar from observed files and official lists.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "coverage"):
        p = sub.add_parser(name)
        p.add_argument("exchange", choices=sorted(bhav.VENUES))
        p.add_argument("start")
        p.add_argument("end")
    pi = sub.add_parser("import-official")
    pi.add_argument("file")
    pi.add_argument("--exchange", required=True, choices=sorted(bhav.VENUES))
    pi.add_argument("--source-url", required=True)
    pi.add_argument("--retrieved-on", required=True)
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "import-official":
            rec = import_official(a.file, cache, a.exchange, a.source_url, a.retrieved_on)
            print("Imported %d rows (%s to %s), sha256 %s" % (rec["rows"], rec["first"], rec["last"], rec["sha256"]))
            return 0
        cal = from_cache(cache, a.exchange)
        if a.cmd == "status":
            for d in cal.days(a.start, a.end):
                print("%s %s %s" % (d, d.strftime("%a"), cal.status(d)))
        else:
            c = cal.coverage(a.start, a.end)
            print("weekdays: %s" % c["weekday"])
            print("weekends: %s" % c["weekend"])
            print("every weekday accounted for: %s; weekdays with no file (closure unconfirmed): %d"
                  % ("yes" if c["complete_weekdays"] else "no", c["unconfirmed_closures"]))
            print("last observed session: %s" % cal.last_observed)
    except (CalendarError, bhav.BhavError, fetch.FetchError) as e:
        print("calendar_in.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
