#!/usr/bin/env python3
"""Historical transaction costs from an evidence-backed, effective-dated schedule.

A schedule (india/cost_schedule.json) lists components. Each component says
where it applies (venues, sides, instrument classes), what it is charged on
(basis) and holds dated periods, each with its rate, rounding rule, source
and verification status. Nothing else is assumed.

  basis           turnover      a proportion of the fill value (units x price)
                  per_fill      a fixed amount per fill
                  per_debit     a fixed amount per sale debited from the demat
                                account (one per sell fill here)
                  on_components a proportion of other components' amounts
                                (GST); unresolved if any of them is
  rate            {"numerator": n, "denominator": d} for proportions, exact;
                  {"paise": n} for fixed amounts
  rounding        half_up | ceil | floor, to whole paise, applied per
                  component per fill; or nearest_rupee_half_up

A component applies to a fill if the venue, side and instrument class match.
Its amount is known only when one period covers the trade date, that period
is verified (or the whole schedule is a labelled synthetic fixture), and its
inputs are known. Otherwise it is unresolved. An unresolved amount is never
zero: the fill's Charges carry the known part, list the unresolved
components, and have status "unresolved".

Investor-level taxes (capital gains) are not transaction costs: they depend
on the holding period, the legal classification of the units, the tax year
and the investor. investor_taxes() reports their status; this module never
computes a tax liability.
"""

import argparse
import datetime
import decimal
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import simbroker  # noqa: E402

SCHEDULE = os.path.join(HERE, "cost_schedule.json")
BASES = ("turnover", "per_fill", "per_debit", "on_components")
ROUNDING = ("half_up", "ceil", "floor", "nearest_rupee_half_up")
VERIFICATION = ("verified", "unverified", "ambiguous")
SIDES = ("buy", "sell")


class CostError(Exception):
    pass


def _date(x, where):
    try:
        return datetime.date.fromisoformat(x)
    except (TypeError, ValueError):
        raise CostError("%s: %r is not YYYY-MM-DD" % (where, x))


def _round(value, rule):
    """Exact Decimal paise -> int paise by the stated rule."""
    q = decimal.Decimal(1)
    if rule == "half_up":
        return int(value.quantize(q, rounding=decimal.ROUND_HALF_UP))
    if rule == "ceil":
        return int(value.to_integral_value(rounding=decimal.ROUND_CEILING))
    if rule == "floor":
        return int(value.to_integral_value(rounding=decimal.ROUND_FLOOR))
    if rule == "nearest_rupee_half_up":
        return int((value / 100).quantize(q, rounding=decimal.ROUND_HALF_UP)) * 100
    raise CostError("unknown rounding %r" % rule)


class Schedule(object):
    def __init__(self, doc, source="inline"):
        self.doc, self.source = doc, source
        self.synthetic = bool(doc.get("synthetic"))
        if self.synthetic and "SYNTHETIC" not in str(doc.get("label", "")).upper():
            raise CostError("a synthetic schedule must say SYNTHETIC in its label")
        self.components = doc.get("components", [])
        self.validate()

    @classmethod
    def load(cls, path=SCHEDULE):
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), path)

    def validate(self):
        ids = set()
        for c in self.components:
            cid = c.get("id")
            if not cid or cid in ids:
                raise CostError("component ids must be present and unique (%r)" % cid)
            ids.add(cid)
            if c.get("basis") not in BASES:
                raise CostError("%s: basis must be one of %s" % (cid, ", ".join(BASES)))
            ap = c.get("applies") or {}
            if not ap.get("venues") or not ap.get("sides") or not ap.get("instruments"):
                raise CostError("%s: applies needs venues, sides and instruments" % cid)
            if any(s not in SIDES for s in ap["sides"]):
                raise CostError("%s: sides are buy and sell" % cid)
            if c["basis"] == "on_components" and not c.get("of"):
                raise CostError("%s: an on_components charge names the components it is levied on" % cid)
            spans = []
            for i, p in enumerate(c.get("periods", [])):
                where = "%s period %d" % (cid, i)
                a = _date(p.get("effective_from"), where)
                b = _date(p["effective_to"], where) if p.get("effective_to") else datetime.date.max
                if b < a:
                    raise CostError("%s ends before it starts" % where)
                if p.get("verification") not in VERIFICATION:
                    raise CostError("%s: verification must be one of %s" % (where, ", ".join(VERIFICATION)))
                if p.get("rounding") not in ROUNDING:
                    raise CostError("%s: rounding must be one of %s" % (where, ", ".join(ROUNDING)))
                r = p.get("rate") or {}
                if c["basis"] in ("turnover", "on_components"):
                    if not (isinstance(r.get("numerator"), int) and isinstance(r.get("denominator"), int)
                            and r["numerator"] >= 0 and r["denominator"] > 0):
                        raise CostError("%s: a proportional rate is {numerator, denominator} integers" % where)
                elif not (isinstance(r.get("paise"), int) and r["paise"] >= 0):
                    raise CostError("%s: a fixed rate is {paise: integer}" % where)
                if not self.synthetic and p["verification"] == "verified":
                    src = p.get("source") or {}
                    for k in ("document", "publisher", "url", "sha256", "retrieved_on", "citation"):
                        if not src.get(k):
                            raise CostError("%s: a verified period cites source.%s" % (where, k))
                spans.append((a, b))
            spans.sort()
            for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
                if a2 <= b1:
                    raise CostError("%s has overlapping periods (%s..%s and %s..)" % (cid, a1, b1, a2))
        for c in self.components:
            for o in c.get("of", []):
                if o not in ids:
                    raise CostError("%s is levied on unknown component %s" % (c["id"], o))

    def period(self, component, day):
        d = _date(day, "trade date")
        for p in component.get("periods", []):
            a = _date(p["effective_from"], "x")
            b = _date(p["effective_to"], "x") if p.get("effective_to") else datetime.date.max
            if a <= d <= b:
                return p
        return None

    def applicable(self, side, venue, instrument):
        return [c for c in self.components
                if side in c["applies"]["sides"] and venue in c["applies"]["venues"]
                and instrument in c["applies"]["instruments"]]

    def coverage(self, start, end, venue, instrument):
        """[(component id, side, problem)] for every applicable component not verified over the whole range."""
        out = []
        for side in SIDES:
            for c in self.applicable(side, venue, instrument):
                d, e = _date(start, "start"), _date(end, "end")
                periods = sorted(c.get("periods", []), key=lambda p: p["effective_from"])
                ok = [p for p in periods if p["verification"] == "verified" or self.synthetic]
                cursor = d
                for p in ok:
                    a = _date(p["effective_from"], "x")
                    b = _date(p["effective_to"], "x") if p.get("effective_to") else datetime.date.max
                    if a <= cursor <= b:
                        cursor = b + datetime.timedelta(days=1) if b != datetime.date.max else datetime.date.max
                if cursor <= e:
                    why = c.get("unresolved_reason") or "no verified rate"
                    out.append((c["id"], side, "no verified rate from %s; %s" % (cursor if cursor != datetime.date.max
                                                                             else e, why)))
        return out


class ScheduleCosts(simbroker.CostModel):
    """Stage 5 cost model: Charges for one fill from a Schedule, component by component."""

    def __init__(self, schedule, instrument="etf"):
        self.schedule, self.instrument = schedule, instrument

    def charges(self, side, units, price_paise, date, venue, key):
        if side not in SIDES or not isinstance(units, int) or units < 0 or not isinstance(price_paise, int):
            raise CostError("bad fill: %r %r units at %r paise" % (side, units, price_paise))
        value = decimal.Decimal(units * price_paise)
        comps, amounts = [], {}
        applicable = self.schedule.applicable(side, venue, self.instrument)
        # plain components first, then those levied on others
        for c in sorted(applicable, key=lambda c: c["basis"] == "on_components"):
            p = self.schedule.period(c, date)
            entry = {"component": c["id"], "category": c.get("category"), "basis": c["basis"]}
            if p is None:
                entry.update(status="unresolved", why="no period covers %s; %s" % (
                    date, c.get("unresolved_reason") or "no rate"))
            elif not (p["verification"] == "verified" or self.schedule.synthetic):
                entry.update(status="unresolved", why="the rate for %s is %s" % (date, p["verification"]))
            else:
                r = p["rate"]
                if c["basis"] == "turnover":
                    raw = value * r["numerator"] / r["denominator"]
                elif c["basis"] == "per_fill":
                    raw = decimal.Decimal(r["paise"] if units else 0)
                elif c["basis"] == "per_debit":
                    raw = decimal.Decimal(r["paise"] if units and side == "sell" else 0)
                else:
                    base = [amounts.get(o) for o in c["of"] if o in [x["id"] for x in applicable]]
                    if any(b is None for b in base):
                        entry.update(status="unresolved", why="levied on a component that is unresolved")
                        comps.append(entry)
                        amounts[c["id"]] = None
                        continue
                    raw = decimal.Decimal(sum(base)) * r["numerator"] / r["denominator"]
                amt = _round(raw, p["rounding"])
                entry.update(status="synthetic" if self.schedule.synthetic else "evidenced", paise=amt,
                             period=p["effective_from"], rounding=p["rounding"],
                             source=(p.get("source") or {}).get("citation", "synthetic fixture"))
                amounts[c["id"]] = amt
                comps.append(entry)
                continue
            amounts[c["id"]] = None
            comps.append(entry)
        known = sum(a for a in amounts.values() if a is not None)
        if not applicable:
            return simbroker.Charges(None, "unresolved", [{"component": "all", "status": "unresolved",
                                                           "why": "no component applies to %s %s on %s"
                                                                  % (venue, side, self.instrument)}], known_paise=0)
        if any(a is None for a in amounts.values()):
            return simbroker.Charges(None, "unresolved", comps, known_paise=known)
        return simbroker.Charges(known, "synthetic" if self.schedule.synthetic else "evidenced", comps,
                                 known_paise=known)


def investor_taxes(schedule):
    """Investor-level tax items and what each depends on. Never a computed liability."""
    return list(schedule.doc.get("investor_taxes", []))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Inspect the historical cost schedule.")
    ap.add_argument("--schedule", default=SCHEDULE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("coverage")
    pc.add_argument("start")
    pc.add_argument("end")
    pc.add_argument("--venue", default="BSE")
    a = ap.parse_args(argv)
    try:
        s = Schedule.load(a.schedule)
    except (CostError, OSError, ValueError) as e:
        print("costs.py: %s" % e, file=sys.stderr)
        return 1
    gaps = s.coverage(a.start, a.end, a.venue, "etf")
    for cid, side, why in gaps:
        print("UNRESOLVED %-34s %-4s %s" % (cid, side, why))
    for t in investor_taxes(s):
        print("INVESTOR TAX %-30s %s: %s" % (t["id"], t["status"], "; ".join(t.get("depends_on", []))))
    print("%d applicable component-side pairs without verified rates over %s to %s." % (len(gaps), a.start, a.end))
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
