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
  invalid           a file came back but failed validation (an error)
  gap               an invalid file documented in india/data_gaps.json with
                    this exact checksum: a real session whose data is
                    unusable. Accounted for, never tradable, never priced.
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

    python3 calendar_in.py bse-notice 20241213-30

fetches a BSE trading-holiday notice by its number (sources.json
bse-notice), checks that its subject is a calendar-year holiday list for
the equity segment, reads the dates from the notice text (weekday and
weekend holidays, and a Muhurat session if the notice announces one), and
imports them as the official BSE list with the notice URL and the checksum
of the page it came from. Nothing is typed by hand.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import bhav  # noqa: E402
import fetch  # noqa: E402

OFFICIAL = "official-calendar"
GAPS = os.path.join(HERE, "data_gaps.json")
SHA = re.compile(r"^[0-9a-f]{64}$")
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


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
NOTICE_ROW = re.compile(r"\d+ ([A-Z][^0-9]*?) (%s) ?(\d{1,2}), ?(\d{4})" % MONTHS)


def parse_bse_holiday_notice(text):
    """(notice number, year, rows) from a BSE trading-holiday notice page, or CalendarError."""
    import html as htmlmod
    x = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", text)))
    m = re.search(r"Notice No\.? (\d{8}-\d+)", x)
    subj = re.search(r"Trading Holidays for the Calendar Year (\d{4}) for Equity", x)
    if not m or not subj:
        raise CalendarError("not a BSE trading-holiday notice for the equity segment")
    year = int(subj.group(1))
    start = x.find("during the Calendar Year")
    end = x.find("For and on behalf")
    if start < 0 or end < 0:
        raise CalendarError("the notice text does not have the expected holiday table")
    body = x[start:end]
    mu = re.search(r"Muhurat Trading will be conducted on \w+, ((?:%s) \d{1,2}, ?\d{4})" % MONTHS, body)
    mud = datetime.datetime.strptime(mu.group(1).replace(", ", ",").replace(",", ", "), "%B %d, %Y").date() if mu else None
    rows, seen = [], set()
    for name, mon, dd, yy in NOTICE_ROW.findall(body.split("*Muhurat")[0] if "*Muhurat" in body else body):
        d = datetime.datetime.strptime("%s %s %s" % (mon, dd, yy), "%B %d %Y").date()
        if d.year != year:
            raise CalendarError("%s is outside the notice's year %d" % (d, year))
        if d in seen:
            raise CalendarError("%s appears twice in the notice" % d)
        seen.add(d)
        if d == mud:
            rows.append((d, "session", "%s (Muhurat trading session per the notice)" % name.strip()))
        else:
            rows.append((d, "holiday", name.strip()))
    if not rows:
        raise CalendarError("no holiday dates found in the notice")
    return m.group(1), year, sorted(rows)


def import_bse_notice(fetcher, notice):
    rec = fetcher.get("bse-notice", params={"NOTICE": notice})
    text = fetcher.cache.read(rec["sha256"]).decode("utf-8", "replace")
    number, year, rows = parse_bse_holiday_notice(text)
    if number != notice:
        raise CalendarError("asked for notice %s but the page is notice %s" % (notice, number))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["date", "type", "description"])
    for d, t, desc in rows:
        w.writerow([d.isoformat(), t, desc])
    data = out.getvalue().encode("utf-8")
    return fetcher.cache.put(data, {"kind": "manual-import", "ok": True, "source": OFFICIAL, "exchange": "BSE",
                                    "source_url": rec["url"], "retrieved_on": rec["retrieved_at"][:10],
                                    "imported_at": fetch.utc_now(), "filename": "bse-notice-%s.csv" % notice,
                                    "derived_from": rec["sha256"], "notice": number, "year": year,
                                    "rows": len(rows), "first": rows[0][0].isoformat(),
                                    "last": rows[-1][0].isoformat()})


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


def read_gaps(path=GAPS, exchange=None):
    """{date: sha256} of documented data gaps (for one exchange if given)."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for i, g in enumerate(doc.get("gaps", [])):
        for k in ("venue", "date", "sha256", "reason", "status", "handling"):
            if not g.get(k):
                raise CalendarError("data gap #%d has no %s" % (i, k))
        if g["venue"] not in bhav.VENUES or not SHA.match(g["sha256"]):
            raise CalendarError("data gap #%d needs a known venue and a full sha256" % i)
        if g["status"] != "unresolved" or g["handling"] != "excluded":
            raise CalendarError("data gap #%d: a gap is unresolved and excluded; nothing else is supported" % i)
        d = _d(g["date"])
        if exchange is None or g["venue"] == exchange:
            if d in out:
                raise CalendarError("data gap %s is listed twice" % d)
            out[d] = g["sha256"]
    return out


class Calendar(object):
    def __init__(self, exchange, files, attempts, official=(), invalid=(), gaps=None):
        """files: {date: sha256} of verified price files;
        attempts: [(date, source, reason)] refusals and network failures;
        official: [(date, type, description, sha256)];
        invalid: dates, or (date, sha256), of files that failed validation;
        gaps: {date: sha256} documented gaps."""
        self.exchange = exchange
        self.files = {_d(k): v for k, v in files.items()}
        self.sources = set(bhav.VENUES[exchange])
        self.refused, self.network = {}, set()
        self.invalid = {}
        for x in invalid:
            d, sha = (x if isinstance(x, (tuple, list)) else (x, None))
            self.invalid.setdefault(_d(d), set()).add(sha)
        self.gaps = {_d(k): v for k, v in (gaps or {}).items()}
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
        if d in self.invalid:
            return "gap" if self.gaps.get(d) in self.invalid[d] else "invalid"
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
        out["complete_weekdays"] = not any(wd.get(s) for s in ("unobserved", "unreachable", "conflict", "invalid"))
        out["unconfirmed_closures"] = wd.get("no-file", 0)
        out["documented_gaps"] = [d.isoformat() for d in self.days(start, end) if self.status(d) == "gap"]
        return out

    def tradable(self, day):
        """Only an observed session with a valid file can carry a signal or a trade."""
        return self.status(day) == "trading"

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


def from_cache(cache, exchange, gaps_path=None):
    files = {r["date"]: r["sha256"] for r, _, _ in bhav.observations(cache, exchange, funds_only=True)}
    attempts = [(r["date"], r["source"], r.get("reason"))
                for r in cache.records()
                if r.get("kind") == "fetch" and not r.get("ok") and r.get("date")
                and bhav.SOURCE_VENUE.get(r.get("source")) == exchange]
    invalid = [(r["date"], r.get("sha256")) for r in cache.records() if r.get("kind") == "invalid" and r.get("date")
               and bhav.SOURCE_VENUE.get(r.get("source")) == exchange]
    official = []
    for r in cache.records():
        if r.get("source") == OFFICIAL and r.get("exchange") == exchange and r.get("ok"):
            for d, t, desc in parse_official(cache.read(r["sha256"]).decode("utf-8-sig")):
                official.append((d, t, desc, r["sha256"]))
    return Calendar(exchange, files, attempts, official, invalid, read_gaps(gaps_path or GAPS, exchange))


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
    pn = sub.add_parser("bse-notice")
    pn.add_argument("notice", help="BSE notice number, e.g. 20241213-30")
    pn.add_argument("--registry", default=None)
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "bse-notice":
            import sources as src
            f = fetch.Fetcher(src.read(a.registry or src.DEFAULT_PATH), cache)
            rec = import_bse_notice(f, a.notice)
            print("Imported notice %s (%d) as the official BSE list: %d rows, %s to %s, sha256 %s"
                  % (rec["notice"], rec["year"], rec["rows"], rec["first"], rec["last"], rec["sha256"]))
            return 0
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
            print("documented data gaps (excluded, unresolved): %s" % (", ".join(c["documented_gaps"]) or "none"))
            print("last observed session: %s" % cal.last_observed)
    except (CalendarError, bhav.BhavError, fetch.FetchError) as e:
        print("calendar_in.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
