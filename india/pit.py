#!/usr/bin/env python3
"""Point-in-time market data: what a strategy may know on a given session.

The engine holds a MarketData (every verified price row, the calendar and
the documented gaps). A strategy never receives it. It receives a View made
for one decision session, and a View answers only about sessions on or
before that session: asking about a later date raises LookAheadError, and
anything it returns is a copy, so a strategy cannot reach or change the
data behind it.

  sessions()          tradable sessions up to the view date (a documented
                      gap, a no-file day or an unobserved day is never one)
  bar(key, day)       the verified row for an ETF on a tradable session, or
                      None if it did not trade; no identity-only rows
  closes(key, n)      the last n closes of the ETF up to the view date, by
                      tradable session (a gap simply has no close: nothing
                      is filled in)
  mark(key)           the marks.mark valuation on the view date, using no
                      close after it
  status(day)         the calendar status of a day on or before the view
  gaps()              documented gaps on or before the view date
  keys(day)           listing keys with a verified trade on a tradable session
  rows(day)           copies of every verified fund row on a tradable session
  idents(day)         {key: ISIN} on a tradable session, identity-only rows
                      included (for identity checks, never for prices)
  isin(key, day)      the ISIN a key carried on a session, including keys seen
                      only with a non-fund ISIN (identity, never a price)
  adjacent(a, b)      calendar adjacency of two sessions, both on or before
                      the view date

The view date itself must be a tradable session: no decision is taken on a
gap. Prices only: corporate actions, eligibility evidence and evaluator
output are not reachable from here.

Limit: Python cannot hide the private attributes from code that goes looking
for them. The View stops accidental look-ahead through its interface; the
signal ledger (hashed and sealed before outcomes are computed) and running
the strategy in a separate process with only serialized past data are the
stronger protections, built in later stages.
"""

import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import marks  # noqa: E402


class LookAheadError(Exception):
    pass


class PitError(Exception):
    pass


def _iso(d):
    return d.isoformat() if isinstance(d, datetime.date) else str(d)


class MarketData(object):
    """Everything the engine knows. Held by the engine; never given to a strategy."""

    def __init__(self, calendar, rows_by_date, venue):
        self._cal = calendar
        self._venue = venue
        self._rows, self._ident = {}, {}
        for day, rows in rows_by_date.items():
            keep = {k: dict(r) for k, r in rows.items() if not r.get("identity_only")}
            self._rows[_iso(day)] = keep
            self._ident[_iso(day)] = {k: r.get("isin") for k, r in rows.items() if r.get("isin")}
        self._sessions = sorted(d for d in self._rows if calendar.tradable(d))
        self._digests = {}

    @property
    def venue(self):
        return self._venue

    def view(self, as_of):
        d = _iso(as_of)
        if not self._cal.tradable(d):
            raise PitError("%s is not a tradable session (%s): no decision is taken on it" % (d, self._cal.status(d)))
        return View(self, d)

    def digest_through(self, as_of):
        """SHA-256 over every tradable session's rows (and documented gaps) up to and including as_of,
        chained session by session, so it depends on nothing after as_of. Engine-side; built incrementally."""
        d = _iso(as_of)
        h = "0" * 64                              # self._digests caches the session chain only
        for s in self._sessions:
            if s > d:
                break
            if s not in self._digests:
                body = json.dumps({"date": s, "rows": self._rows.get(s, {}), "ident": self._ident.get(s, {})},
                                  sort_keys=True, separators=(",", ":"))
                self._digests[s] = hashlib.sha256((h + body).encode("utf-8")).hexdigest()
            h = self._digests[s]
        gaps = sorted(g.isoformat() for g in getattr(self._cal, "gaps", {}) if g.isoformat() <= d)
        return hashlib.sha256((h + json.dumps(gaps)).encode("utf-8")).hexdigest()

    def sessions_between(self, start, end):
        """Engine-only: tradable sessions in a closed range."""
        return [d for d in self._sessions if _iso(start) <= d <= _iso(end)]


class View(object):
    """What a strategy may know on one session. Read-only, past-only."""

    __slots__ = ("_data", "_as_of")

    def __init__(self, data, as_of):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_as_of", as_of)

    def __setattr__(self, name, value):
        raise AttributeError("a View is read-only")

    @property
    def as_of(self):
        return self._as_of

    @property
    def venue(self):
        return self._data.venue

    def _guard(self, day):
        d = _iso(day)
        if d > self._as_of:
            raise LookAheadError("%s is after the view date %s" % (d, self._as_of))
        return d

    def sessions(self, n=None):
        s = [d for d in self._data._sessions if d <= self._as_of]
        return list(s if n is None else s[-n:])

    def status(self, day):
        return self._data._cal.status(self._guard(day))

    def bar(self, key, day=None):
        d = self._guard(day or self._as_of)
        if not self._data._cal.tradable(d):
            return None
        row = self._data._rows.get(d, {}).get(str(key))
        return dict(row) if row else None   # rows hold only scalars: a shallow copy is a full copy

    def closes(self, key, n):
        if n <= 0:
            raise PitError("n must be positive")
        out = []
        for d in reversed(self.sessions()):
            r = self._data._rows.get(d, {}).get(str(key))
            if r and r.get("close") is not None:
                out.append((d, r["close"]))
                if len(out) == n:
                    break
        return list(reversed(out))

    def mark(self, key):
        closes = {}
        for d in self.sessions():
            r = self._data._rows.get(d, {}).get(str(key))
            if r and r.get("close") is not None:
                closes[d] = r["close"]
        return marks.mark(closes, self._as_of, _PastCalendar(self._data._cal, self._as_of))

    def keys(self, day=None):
        d = self._guard(day or self._as_of)
        if not self._data._cal.tradable(d):
            return []
        return sorted(self._data._rows.get(d, {}))

    def rows(self, day):
        d = self._guard(day)
        if not self._data._cal.tradable(d):
            return {}
        return {k: dict(r) for k, r in self._data._rows.get(d, {}).items()}

    def idents(self, day):
        d = self._guard(day)
        if not self._data._cal.tradable(d):
            return {}
        return dict(self._data._ident.get(d, {}))

    def isin(self, key, day):
        d = self._guard(day)
        if not self._data._cal.tradable(d):
            return None
        return self._data._ident.get(d, {}).get(str(key))

    def adjacent(self, a, b):
        self._guard(a)
        self._guard(b)
        return self._data._cal.adjacent(_iso(a), _iso(b))

    def gaps(self):
        cal = self._data._cal
        return [d.isoformat() for d in sorted(cal.gaps) if d.isoformat() <= self._as_of]


class _PastCalendar(object):
    """The calendar, cut off at the view date, for marks.mark."""

    def __init__(self, cal, as_of):
        self._cal, self._as_of = cal, as_of

    def _g(self, d):
        if _iso(d) > self._as_of:
            raise LookAheadError("%s is after the view date %s" % (_iso(d), self._as_of))
        return d

    def tradable(self, d):
        return self._cal.tradable(self._g(d))

    def status(self, d):
        return self._cal.status(self._g(d))


def from_cache(cache, venue):
    """MarketData from the verified price files and calendar in the local cache."""
    import bhav
    import calendar_in
    import secmaster
    cal = calendar_in.from_cache(cache, venue)
    rows = {}
    for r, _, rs in bhav.observations(cache, venue, funds_only=True):
        rows[r["date"]] = {secmaster.key_of(x): x for x in rs if x["exchange"] == venue}
    return MarketData(cal, rows, venue)
