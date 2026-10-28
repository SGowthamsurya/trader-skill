#!/usr/bin/env python3
"""Point-in-time ETF universe: which instruments could be held on a decision date.

A decision on session t uses only what was knowable on t. Prices come only
through pit.View (past-only). Evidence is usable on t only if the fact it
states is dated on or before t: a corporate action from its announced_on, an
identity change from its stated effective date, a distribution review over
the period it covers. A fact a document states without a date (the AMFI
file's scheme names) is knowable only from the day the file was retrieved.

Candidates on t are every fund instrument (ISIN starting INF) that traded on
a valid session up to t. One that later stops trading stays a candidate
until it falls out of the trailing window, then is reported inactive; it is
never dropped because of what happened after t. Completeness is limited to
what the surviving price files show: an instrument absent from every
collected file cannot appear, so survivorship bias is reduced, not shown to
be eliminated.

Each candidate gets every test below, with an outcome (pass, fail or
unresolved) and the date-specific reason:

  history       at least min_history_sessions sessions since first seen
  data          at most max_unknown_share of its window has unknown state
                (documented gap, invalid file, unobserved or unreachable
                weekday); otherwise presence and liquidity are unresolved
  presence      traded on at least min_presence of its window's sessions
  liquidity     median traded value over its window at or above the
                threshold; a valid session on which it did not trade counts
                as an observed zero, an unknown day is left out, never zero
  identity      every ISIN change on its key up to t is confirmed by a
                verified record dated on or before t; no other key carried
                its ISIN on the same session
  corporate     every price break beyond break_ratio between adjacent
                sessions up to t matches a verified action announced on or
                before t; the document's units multiplier must agree with
                the observed change
  distributions a verified distribution review covers its window up to t
  category      a category fact for its ISIN knowable on t; a fact placing it
                outside the study categories (gold, debt, sector ...) fails
  affordable    its last close up to t is at most capital_inr

status: eligible (all pass), excluded (a test fails), insufficient-data
(nothing fails, something unresolved), inactive (not seen in the window).

    python3 universe.py explain 2020-06-01           # one decision date
    python3 universe.py explain 2020-06-01 --json    # with the digest
"""

import argparse
import datetime
import hashlib
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import pit  # noqa: E402

UNKNOWN = ("gap", "invalid", "unobserved", "unreachable", "conflict")
CATEGORIES = ("nifty50", "niftynext50", "bank")
TESTS = ("history", "data", "presence", "liquidity", "identity", "corporate", "distributions", "category",
         "affordable")


class UniverseError(Exception):
    pass


def _d(x):
    return x if isinstance(x, datetime.date) else datetime.date.fromisoformat(str(x))


class Rules(object):
    def __init__(self, venue, threshold, min_presence, break_ratio, capital, trailing, min_history, max_unknown,
                 ratio_tolerance=0.15):
        self.venue, self.threshold, self.min_presence = venue, threshold, min_presence
        self.break_ratio, self.capital = break_ratio, capital
        self.trailing, self.min_history, self.max_unknown = trailing, min_history, max_unknown
        self.ratio_tolerance = ratio_tolerance

    @classmethod
    def from_criteria(cls, path):
        with open(path, encoding="utf-8") as fh:
            c = json.load(fh)
        v = lambda x: x["value"] if isinstance(x, dict) else x  # noqa: E731
        p = c["point_in_time"]
        if c["liquidity"].get("min_median_value_inr") is None:
            raise UniverseError("no liquidity threshold in %s" % path)
        return cls(c["venue"], c["liquidity"]["min_median_value_inr"], v(c["min_presence"]), v(c["break_ratio"]),
                   c["capital_inr"], p["trailing_sessions"], p["min_history_sessions"], p["max_unknown_share"])


class Evidence(object):
    """Dated facts. categories: {isin: [(category, knowable_from, source)]};
    actions, confirmations, reviews: dicts as in corporate_actions.json and identity_confirmations.json."""

    def __init__(self, categories=None, actions=(), confirmations=()):
        self.categories = categories or {}
        self.splits = [a for a in actions if a.get("type") == "unit-split"]
        self.reviews = [a for a in actions if a.get("type") == "distributions-reviewed"]
        self.confirmations = list(confirmations)

    @classmethod
    def from_files(cls, cache, actions_path=None, confirmations_path=None):
        import etf
        import secmaster
        cats = {}
        for r in cache.records():
            if r.get("source") == "amfi-nav-latest" and r.get("ok"):
                names = etf.amfi_names(cache.read(r["sha256"]).decode("utf-8", "replace"))
                when = r["retrieved_at"][:10]
                for isin, name in names.items():
                    c = etf.category(name) or "other"
                    cats.setdefault(isin, []).append((c, when, "AMFI NAVAll.txt sha256 %s (undated; knowable "
                                                                 "from retrieval)" % r["sha256"][:12]))
        return cls(cats, etf.read_actions(actions_path), secmaster.read_confirmations(
            confirmations_path or secmaster.CONFIRMATIONS))


class Decision(object):
    def __init__(self, key, isin, symbol, status, tests, first_seen, last_seen):
        self.key, self.isin, self.symbol, self.status = key, isin, symbol, status
        self.tests, self.first_seen, self.last_seen = tests, first_seen, last_seen

    def to_dict(self):
        return {"key": self.key, "isin": self.isin, "symbol_hint": self.symbol, "status": self.status,
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "tests": {k: {"outcome": o, "why": w} for k, (o, w) in self.tests.items()}}


class Snapshot(object):
    def __init__(self, as_of, venue, decisions):
        self.as_of, self.venue = as_of, venue
        self.decisions = sorted(decisions, key=lambda x: x.key)

    def eligible(self):
        return [x.key for x in self.decisions if x.status == "eligible"]

    def to_json(self):
        return json.dumps({"as_of": self.as_of, "venue": self.venue,
                           "decisions": [x.to_dict() for x in self.decisions]}, sort_keys=True, separators=(",", ":"))

    def digest(self):
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


class Universe(object):
    def __init__(self, data, evidence, rules):
        if data.venue != rules.venue:
            raise UniverseError("market data is %s but the rules are for %s" % (data.venue, rules.venue))
        self._data, self.evidence, self.rules = data, evidence, rules

    def decide(self, as_of):
        view = self._data.view(as_of)
        t = view.as_of
        sessions = view.sessions()
        window = sessions[-self.rules.trailing:]
        bars, ids, owners = {}, {}, {}
        for d in sessions:                       # one past-only pass per decision
            for k, row in view.rows(d).items():
                bars.setdefault(k, {})[d] = row
            for k, isin in view.idents(d).items():
                ids.setdefault(k, []).append((d, isin))
                owners.setdefault((d, isin), set()).add(k)
        decisions = []
        for key, b in bars.items():
            days = sorted(b)
            isin = b[days[-1]].get("isin")
            if not (isin or "").startswith("INF"):
                continue
            ctx = {"bars": b, "ids": ids.get(key, []), "owners": owners}
            decisions.append(self._decide_one(view, t, sessions, window, key, days, isin, ctx))
        return Snapshot(t, self.rules.venue, decisions)

    def _decide_one(self, view, t, sessions, window, key, days, isin, ctx):
        r = self.rules
        bars = ctx["bars"]
        symbol = bars[days[-1]].get("symbol")
        if days[-1] < window[0]:
            return Decision(key, isin, symbol, "inactive",
                            {"active": ("fail", "last traded %s, before the trailing window starting %s"
                                        % (days[-1], window[0]))}, days[0], days[-1])
        tests = {}
        since = [d for d in sessions if d >= days[0]]
        tests["history"] = (("pass" if len(since) >= r.min_history else "fail"),
                            "%d sessions since first seen %s (need %d)" % (len(since), days[0], r.min_history))

        mine = [d for d in window if d >= days[0]]
        unknown = self._unknown_days(view, mine[0], t)
        share = len(unknown) / float(len(mine) + len(unknown))
        traded = set(days)
        if share > r.max_unknown:
            why = "%d of %d days in its window have unknown state (%s ...)" % (len(unknown), len(mine) + len(unknown),
                                                                                ", ".join(unknown[:3]))
            tests["data"] = ("unresolved", why)
            tests["presence"] = ("unresolved", "not measured: " + why)
            tests["liquidity"] = ("unresolved", "not measured: " + why)
        else:
            tests["data"] = ("pass", "%d unknown-state days of %d (%s)" % (
                len(unknown), len(mine) + len(unknown), ", ".join(unknown) or "none"))
            pres = sum(1 for d in mine if d in traded) / float(len(mine))
            tests["presence"] = (("pass" if pres >= r.min_presence else "fail"),
                                 "traded on %d of %d sessions in its window" % (sum(1 for d in mine if d in traded),
                                                                                len(mine)))
            vals = []
            for d in mine:
                vals.append((bars.get(d) or {}).get("value") or 0.0)   # a valid session without its trade: observed zero
            med = statistics.median(vals)
            tests["liquidity"] = (("pass" if med >= r.threshold else "fail"),
                                  "median traded value Rs %s over %d sessions %s to %s (unknown days left out)"
                                  % ("{:,.0f}".format(med), len(mine), mine[0], mine[-1]))

        tests["identity"] = self._identity(t, key, isin, ctx)
        tests["corporate"] = self._corporate(view, t, key, days, bars)
        tests["distributions"] = self._distributions(t, key, mine[0])
        tests["category"] = self._category(t, isin, symbol)
        price, flags = view.mark(key)
        if price is None:
            tests["affordable"] = ("unresolved", "no close on or before %s" % t)
        else:
            tests["affordable"] = (("pass" if price <= r.capital else "fail"), "last close Rs %.2f%s (capital Rs %s)"
                                   % (price, " (%s)" % flags[0] if flags else "", r.capital))
        outcomes = [o for o, _ in tests.values()]
        status = "excluded" if "fail" in outcomes else "insufficient-data" if "unresolved" in outcomes else "eligible"
        return Decision(key, isin, symbol, status, tests, days[0], days[-1])

    def _unknown_days(self, view, start, end):
        out, d = [], _d(start)
        while d <= _d(end):
            if d.weekday() < 5 and view.status(d.isoformat()) in UNKNOWN:
                out.append(d.isoformat())
            d += datetime.timedelta(days=1)
        return out

    def _identity(self, t, key, isin, ctx):
        hist = ctx["ids"]
        changes = [(a, b, x, y) for (a, x), (b, y) in zip(hist, hist[1:]) if x != y]
        for d, i in hist:
            others = ctx["owners"].get((d, i), set()) - {key}
            if others:
                return ("fail", "%s was also carried by key %s on %s" % (i, sorted(others)[0], d))
        open_ = []
        for a, b, x, y in changes:
            ok = [c for c in self.evidence.confirmations if c.get("verification") == "verified"
                  and c.get("exchange") == self.rules.venue and str(c.get("key")) == key
                  and c.get("from_isin") == x and c.get("to_isin") == y
                  and (c.get("effective_on") or c.get("announced_on") or "9999") <= t]
            if not ok:
                open_.append("%s -> %s between %s and %s has no verified confirmation dated on or before %s"
                             % (x, y, a, b, t))
        if open_:
            return ("unresolved", "; ".join(open_))
        return ("pass", "no ISIN change up to %s" % t if not changes else "%d confirmed change(s)" % len(changes))

    def _corporate(self, view, t, key, days, bars):
        r = self.rules
        closes = [(d, bars[d].get("close")) for d in days]
        closes = [(d, c) for d, c in closes if c]
        bad, open_, done = [], [], []
        for (a, x), (b, y) in zip(closes, closes[1:]):
            ratio = y / x
            if 1 / r.break_ratio <= ratio <= r.break_ratio:
                continue
            if not view.adjacent(a, b)[0]:
                open_.append("%s .. %s ratio %.4f across sessions that are not adjacent" % (a, b, ratio))
                continue
            act = self._action_for(view, t, key, a, b)
            if not act:
                open_.append("%s -> %s ratio %.4f: no verified action announced on or before %s" % (a, b, ratio, t))
            elif abs(ratio * act["units_multiplier"] - 1) > r.ratio_tolerance:
                bad.append("%s -> %s moved %.4fx but %s states 1/%s" % (a, b, ratio, act["id"], act["units_multiplier"]))
            else:
                done.append("%s -> %s by %s (announced %s)" % (a, b, act["id"], act["announced_on"]))
        if bad:
            return ("fail", "; ".join(bad))
        if open_:
            return ("unresolved", "; ".join(open_))
        return ("pass", "; ".join(done) or "no unexplained price break up to %s (this does not show that no "
                                            "corporate action occurred)" % t)

    def _action_for(self, view, t, key, a, b):
        for x in self.evidence.splits:
            if x.get("verification") != "verified" or x.get("exchange") != self.rules.venue or str(x.get("key")) != key:
                continue
            if not x.get("announced_on") or x["announced_on"] > t:
                continue
            if x.get("ex_date"):
                if x["ex_date"] == b:
                    return x
                continue
            rd = x.get("record_date")
            if not rd:
                continue
            if b == rd:
                return x
            if b < rd and (_d(rd) - datetime.timedelta(days=1)).isoformat() <= t:
                between = [s for s in view.sessions() if b < s < rd]
                if not between:
                    return x
        return None

    def _distributions(self, t, key, start):
        for x in self.evidence.reviews:
            if (x.get("verification") == "verified" and x.get("exchange") == self.rules.venue and str(x.get("key")) == key
                    and x.get("covers_from", "9999") <= start and x.get("covers_to", "") >= t):
                return ("pass", "%s covers %s to %s" % (x["id"], x["covers_from"], x["covers_to"]))
        return ("unresolved", "no verified distribution review covering %s to %s; price files cannot show that no "
                              "distribution occurred" % (start, t))

    def _category(self, t, isin, symbol):
        facts = self.evidence.categories.get(isin, [])
        usable = [f for f in facts if f[1] <= t and f[0] in CATEGORIES]
        if usable:
            return ("pass", "%s, knowable from %s (%s)" % usable[0])
        outside = [f for f in facts if f[1] <= t]
        if outside:
            return ("fail", "outside the study categories, knowable from %s (%s)" % (outside[0][1], outside[0][2]))
        later = [f for f in facts if f[1] > t]
        hint = " (exchange name hint, not evidence: %s)" % symbol if symbol else ""
        if later:
            return ("unresolved", "category %s is known only from %s, after %s%s"
                    % (later[0][0], later[0][1], t, hint))
        return ("unresolved", "no category evidence for %s%s" % (isin, hint))


def main(argv=None):
    import fetch
    ap = argparse.ArgumentParser(description="Point-in-time ETF universe for one decision date.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    ap.add_argument("--criteria", default=os.path.join(HERE, "etf_criteria.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("explain")
    pe.add_argument("date")
    pe.add_argument("--json", action="store_true")
    pe.add_argument("--key", action="append", default=[])
    a = ap.parse_args(argv)
    try:
        rules = Rules.from_criteria(a.criteria)
        cache = fetch.Cache(a.cache)
        u = Universe(pit.from_cache(cache, rules.venue), Evidence.from_files(cache), rules)
        snap = u.decide(a.date)
    except (UniverseError, pit.PitError, ValueError, OSError) as e:
        print("universe.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"digest": snap.digest(), "snapshot": json.loads(snap.to_json())}, indent=1, sort_keys=True))
        return 0
    counts = {}
    for x in snap.decisions:
        counts[x.status] = counts.get(x.status, 0) + 1
    print("%s %s: %s. digest %s" % (snap.venue, snap.as_of, ", ".join("%s %d" % kv for kv in sorted(counts.items())),
                                     snap.digest()[:16]))
    for x in snap.decisions:
        if a.key and x.key not in a.key:
            continue
        if not a.key and x.status == "inactive":
            continue
        print("%-17s %-8s %-13s %s" % (x.status, x.key, x.isin, x.symbol or ""))
        for k, (o, why) in x.tests.items():
            if o != "pass" or a.key:
                print("    %-13s %-10s %s" % (k, o, why))
    print("Mechanics only: an eligibility list, not a performance result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
