#!/usr/bin/env python3
"""Historical security master: who was who, when, and how we know.

Built only from what the price files show (from_cache reads fund
instruments and the identity of every key that was ever a fund; equity-only
duplicates are out of scope). An instrument is followed on
each exchange by its listing key (the BSE scrip code; the NSE symbol), and
every key carries a history of (symbol, ISIN) identities, each with the
first and last date it was observed and the SHA-256 of the files that show
those dates. Nothing assumes an ISIN or a symbol is permanent.

  identity change   the same key shows a new ISIN or a new symbol. Its
                    date is a window: after the last day the old identity
                    was seen, on or before the first day the new one was
                    seen. An exact effective date only comes from a
                    confirmation record that cites its source.
  link              listings that share an ISIN are the same instrument.
                    A key whose ISIN changed is only PROPOSED to be the same
                    instrument until a confirmation record says so (a fund
                    can change ISIN when it moves to another AMC; a code
                    could in principle be reused).
  conflict          one key with two ISINs, or one ISIN under two keys, on
                    the same exchange and day; or two identities of one key
                    seen in overlapping periods; or a confirmation that does
                    not fit what was observed.

Confirmations live in india/identity_confirmations.json:

    {"exchange": "BSE", "key": "590103", "from_isin": "...", "to_isin": "...",
     "effective_on": "YYYY-MM-DD" or null, "verification": "verified" or "corroborated",
     "source_url": "https://...", "retrieved_on": "YYYY-MM-DD", "documents": [...], "note": "..."}

verified      an official document states this change (old and new ISIN).
              Only this confirms a link.
corroborated  official sources together point to it (an issuer notice for
              the old ISIN, AMFI for the new one, the switch observed on one
              listing on the split day) but no single document states it.
              Shown, never treated as confirmed.

    python3 secmaster.py history BSE 590103
    python3 secmaster.py isin INF204KB14I2
    python3 secmaster.py conflicts BSE
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import bhav  # noqa: E402
import fetch  # noqa: E402

CONFIRMATIONS = os.path.join(HERE, "identity_confirmations.json")
VERIFICATION = ("verified", "corroborated")


class SecError(Exception):
    pass


def key_of(row):
    return row["code"] if row["exchange"] == "BSE" else row["symbol"]


def label_of(row):
    return " ".join((row["symbol"] or "").split()).upper()


def read_confirmations(path=CONFIRMATIONS):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    items = doc.get("confirmations", []) if isinstance(doc, dict) else doc
    for i, c in enumerate(items):
        for k in ("exchange", "key", "from_isin", "to_isin", "source_url", "retrieved_on", "verification"):
            if not c.get(k):
                raise SecError("confirmation #%d has no %s" % (i, k))
        if c["verification"] not in VERIFICATION:
            raise SecError("confirmation #%d: verification must be verified or corroborated" % i)
        if not str(c["source_url"]).startswith("https://"):
            raise SecError("confirmation #%d: source_url must be https" % i)
        for k in ("retrieved_on", "effective_on"):
            if c.get(k):
                try:
                    datetime.date.fromisoformat(c[k])
                except ValueError:
                    raise SecError("confirmation #%d: %s is not YYYY-MM-DD" % (i, k))
    return items


class Master(object):
    def __init__(self, files, confirmations=()):
        """files: [(sha256, rows)] for one or more exchanges, any order."""
        self.ident = {}       # (exchange, key) -> {(label, isin): {first, last, n, first_sha, last_sha}}
        self.days = {}        # (exchange, key) -> sorted list of (date, label, isin)
        self.by_isin = {}     # isin -> {(exchange, key)}
        self.conflicts = []
        for sha, rows in sorted((f for f in files if f[1]), key=lambda f: f[1][0]["date"]):
            self._file(sha, rows)
        self._overlaps()
        self.events = self._events()
        self._confirm(list(confirmations))

    def _file(self, sha, rows):
        seen_key, seen_isin = {}, {}
        for r in rows:
            if not r["isin"]:
                continue
            ex, key, lab = r["exchange"], key_of(r), label_of(r)
            kk = (ex, key, r["series"] if ex == "NSE" else None)
            for table, a, b, what in ((seen_key, kk, r["isin"], "ISIN"), (seen_isin, (ex, r["isin"]), key, "key")):
                prev = table.setdefault(a, b)
                if prev != b:
                    self.conflicts.append({"type": "duplicate-mapping", "exchange": ex, "date": r["date"],
                                           "detail": "%s %s maps to two %ss: %s and %s" % (ex, a[1], what, prev, b),
                                           "sha256": sha})
            slot = self.ident.setdefault((ex, key), {}).setdefault((lab, r["isin"]), {
                "first": r["date"], "last": r["date"], "n": 0, "first_sha": sha, "last_sha": sha})
            slot["last"], slot["last_sha"] = r["date"], sha
            slot["n"] += 1
            self.days.setdefault((ex, key), []).append((r["date"], lab, r["isin"]))
            self.by_isin.setdefault(r["isin"], set()).add((ex, key))

    def _overlaps(self):
        for (ex, key), ids in self.ident.items():
            isins = {}
            for (lab, isin), s in ids.items():
                a = isins.setdefault(isin, [s["first"], s["last"]])
                a[0], a[1] = min(a[0], s["first"]), max(a[1], s["last"])
            spans = sorted((v[0], v[1], i) for i, v in isins.items())
            for (f1, l1, i1), (f2, l2, i2) in zip(spans, spans[1:]):
                if f2 <= l1:
                    self.conflicts.append({"type": "overlapping-isins", "exchange": ex, "key": key,
                                           "detail": "%s %s shows %s until %s and %s from %s" % (ex, key, i1, l1, i2, f2)})

    def _events(self):
        out = []
        for (ex, key), days in self.days.items():
            days = sorted(days)
            for (d1, l1, i1), (d2, l2, i2) in zip(days, days[1:]):
                if i1 != i2 or l1 != l2:
                    kind = "isin-change" if i1 != i2 else "symbol-change"
                    ids = self.ident[(ex, key)]
                    out.append({"type": kind, "exchange": ex, "key": key, "from_isin": i1, "to_isin": i2,
                                "from_symbol": l1, "to_symbol": l2, "after": d1, "on_or_before": d2,
                                "evidence": [ids[(l1, i1)]["last_sha"], ids[(l2, i2)]["first_sha"]],
                                "status": "observed" if kind == "symbol-change" else "proposed",
                                "effective_on": None, "confirmation": None})
        return sorted(out, key=lambda e: (e["exchange"], e["key"], e["after"]))

    def _confirm(self, confirmations):
        for c in confirmations:
            hit = [e for e in self.events if e["type"] == "isin-change" and e["exchange"] == c["exchange"]
                   and e["key"] == str(c["key"]) and e["from_isin"] == c["from_isin"] and e["to_isin"] == c["to_isin"]]
            if not hit:
                self.conflicts.append({"type": "confirmation-unmatched", "exchange": c["exchange"], "key": c["key"],
                                       "detail": "no observed change %s -> %s" % (c["from_isin"], c["to_isin"])})
                continue
            e = hit[0]
            eff = c.get("effective_on")
            if eff and not (e["after"] < eff <= e["on_or_before"]):
                self.conflicts.append({"type": "confirmation-outside-window", "exchange": e["exchange"],
                                       "key": e["key"], "detail": "effective %s is outside the observed window "
                                       "(after %s, on or before %s)" % (eff, e["after"], e["on_or_before"])})
                continue
            if c.get("verification", "verified") == "verified":
                e["status"], e["effective_on"] = "confirmed", eff
            else:
                e["status"] = "corroborated"
            e["confirmation"] = c["source_url"]

    def history(self, exchange, key):
        ids = self.ident.get((exchange, key))
        if not ids:
            raise SecError("%s %s was never observed" % (exchange, key))
        return sorted(({"symbol": l, "isin": i, **s} for (l, i), s in ids.items()), key=lambda h: h["first"])

    def chain(self, isin):
        """Every listing and ISIN connected to this ISIN, and whether each link is confirmed."""
        isins, keys, todo = set(), set(), [isin]
        while todo:
            i = todo.pop()
            if i in isins:
                continue
            isins.add(i)
            for k in self.by_isin.get(i, ()):
                keys.add(k)
                for (_, other) in self.ident[k]:
                    if other not in isins:
                        todo.append(other)
        links = [e for e in self.events if e["type"] == "isin-change" and (e["exchange"], e["key"]) in keys]
        return {"isins": sorted(isins), "listings": sorted(keys), "links": links,
                "confirmed": all(e["status"] == "confirmed" for e in links)}

    def conflicts_for(self, isins):
        isins = set(isins)
        keys = {k for i in isins for k in self.by_isin.get(i, ())}
        return [c for c in self.conflicts
                if (c.get("exchange"), c.get("key")) in keys or any(i in c.get("detail", "") for i in isins)]


def from_cache(cache, venue=None, confirmations=CONFIRMATIONS):
    files = [(r["sha256"], rows) for r, _, rows in bhav.observations(cache, venue, funds_only=True)]
    return Master(files, read_confirmations(confirmations) if isinstance(confirmations, str) else confirmations)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Symbol and ISIN history from the cached price files.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    ap.add_argument("--confirmations", default=CONFIRMATIONS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ph = sub.add_parser("history")
    ph.add_argument("exchange", choices=("NSE", "BSE"))
    ph.add_argument("key")
    pi = sub.add_parser("isin")
    pi.add_argument("isin")
    pc = sub.add_parser("conflicts")
    pc.add_argument("exchange", choices=("NSE", "BSE"), nargs="?")
    a = ap.parse_args(argv)
    try:
        m = from_cache(fetch.Cache(a.cache), confirmations=a.confirmations)
        if a.cmd == "history":
            for h in m.history(a.exchange, a.key):
                print("%s .. %s  %-14s %s  (%d files)" % (h["first"], h["last"], h["symbol"], h["isin"], h["n"]))
            for e in m.events:
                if (e["exchange"], e["key"]) == (a.exchange, a.key):
                    print("%s %s: %s %s -> %s %s, after %s, on or before %s%s" % (
                        e["status"].upper(), e["type"], e["from_symbol"], e["from_isin"], e["to_symbol"], e["to_isin"],
                        e["after"], e["on_or_before"], ", effective %s" % e["effective_on"] if e["effective_on"] else ""))
        elif a.cmd == "isin":
            print(json.dumps(m.chain(a.isin), indent=2))
        else:
            cs = [c for c in m.conflicts if not a.exchange or c.get("exchange") == a.exchange]
            for c in cs:
                print("%s: %s" % (c["type"], c["detail"]))
            print("%d conflicts." % len(cs))
    except (SecError, bhav.BhavError, fetch.FetchError) as e:
        print("secmaster.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
