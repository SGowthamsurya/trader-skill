#!/usr/bin/env python3
"""Simulated broker: turns a sealed signal ledger into orders, fills and a journal.

Input: only a sealed, verified ledger, opened with the seal hash recorded
outside it (expected_seal). Without that anchor nothing is read. The ledger
file is read, never written.

Timing (one signal at a time, in ledger order):
  decision   after the close of the signal's decision session t. The
             portfolio is marked on a past-only view of t; each target gets
             floor(weight x value / reference price) whole units, the
             reference price being t's close or a flagged stale mark.
             Holdings not in the targets are sold to zero.
  execution  the first weekday after t that can be resolved:
               trading                   fill at that session's open
               official holiday, weekend skip to the next day
               gap, invalid, unobserved, unfilled, with the reason
               unreachable, conflict
               no-file (closure          unfilled by default; the policy
               unconfirmed)              on_unconfirmed_closure="roll" treats
                                         it as a closure and records that
             Orders are day orders: an unfilled order is never retried.
  price      the execution session's open: printed after the order was
             placed, never the decision day's close. It must be positive
             and inside that day's low-high range, or the order is unfilled.
  liquidity  liquidity_cap="ex-ante" (default): when the order is placed,
             its value at the reference price must be at most max_share (5%)
             of the ETF's traded value on the decision day, a figure known
             then; otherwise the order is not placed. Each fill also records,
             as an audit field only, whether it was within 5% of the
             execution day's traded value. liquidity_cap="ex-post" applies
             the 5% test to the execution day's full traded value instead,
             which is only known after that day's close: a sensitivity mode,
             never the default, because it uses same-day information.
  checks     sells before buys; the ETF must have a verified trade that
             session with the ISIN the signal named; no partial fills; no
             short sales; a buy needs units x
             price + known fees within settled cash, else it is rejected
             (on_insufficient_cash="reject", the default) or cut to the most
             whole units affordable ("reduce", recorded).
  settlement sale proceeds become usable on the next tradable session.

Costs come from a CostModel. The default, UnresolvedCosts, knows no fee:
every fill carries status "unresolved" and cash excludes the unknown fee,
which the journal and every valuation say. Nothing here treats a fee as
zero. A historical, evidence-backed schedule is stage 5.

Not modelled, because the price files do not support it: price bands and
circuit limits, slippage from the open, queue position and partial fills.
Deviation recorded in Phase 3 completion: the 5% rule was first applied to
the execution day's full traded value (not known at the open). The default
is now the causally valid ex-ante rule above; the old rule survives only as
the labelled ex-post sensitivity mode.

Every result is MECHANICS ONLY until the stage 6 readiness gate exists.
"""

import decimal
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import datetime  # noqa: E402
import execution  # noqa: E402
import portfolio as P  # noqa: E402
import signals  # noqa: E402

LABEL = "MECHANICS ONLY - NOT A PERFORMANCE RESULT (no readiness gate yet)"
SKIP = ("official-holiday",)
UNFILLABLE = ("gap", "invalid", "unobserved", "unreachable", "conflict", "official-session")


class BrokerError(Exception):
    pass


class Charges(object):
    STATUSES = ("evidenced", "synthetic", "unresolved")

    def __init__(self, paise, status, components, known_paise=None):
        if status not in self.STATUSES:
            raise BrokerError("charge status must be one of %s" % ", ".join(self.STATUSES))
        if (paise is None) != (status == "unresolved"):
            raise BrokerError("only unresolved charges have no amount")
        if paise is not None and (not isinstance(paise, int) or paise < 0):
            raise BrokerError("charges are a whole, non-negative number of paise")
        if known_paise is None:
            known_paise = paise if paise is not None else 0
        if not isinstance(known_paise, int) or known_paise < 0 or (paise is not None and known_paise != paise):
            raise BrokerError("known_paise is the whole fee when resolved, the known part when not")
        self.paise, self.status, self.components, self.known_paise = paise, status, components, known_paise


class CostModel(object):
    """Interface for stage 5: charges for one fill, or Charges(None, 'unresolved', ...)."""

    def charges(self, side, units, price_paise, date, venue, key):
        raise NotImplementedError


class UnresolvedCosts(CostModel):
    def charges(self, side, units, price_paise, date, venue, key):
        return Charges(None, "unresolved", [{"component": "all", "status": "unresolved",
                                             "why": "no evidence-backed historical cost schedule (stage 5)"}])


class Broker(object):
    def __init__(self, data, ledger_path, expected_seal, capital_inr=3000, max_share=execution.MAX_SHARE,
                 cost_model=None, on_unconfirmed_closure="unfilled", on_insufficient_cash="reject",
                 liquidity_cap="ex-ante"):
        if not isinstance(expected_seal, str) or not re.match(r"^[0-9a-f]{64}$", expected_seal):
            raise BrokerError("the seal hash recorded outside the ledger is required (64 hex characters)")
        if on_unconfirmed_closure not in ("unfilled", "roll"):
            raise BrokerError("on_unconfirmed_closure is unfilled or roll")
        if on_insufficient_cash not in ("reject", "reduce"):
            raise BrokerError("on_insufficient_cash is reject or reduce")
        if liquidity_cap not in ("ex-ante", "ex-post"):
            raise BrokerError("liquidity_cap is ex-ante or ex-post")
        self.cap_mode = liquidity_cap
        self.ledger = signals.open_sealed(ledger_path, expected_seal)
        if self.ledger.venue != data.venue:
            raise BrokerError("the ledger is for %s, the market data for %s" % (self.ledger.venue, data.venue))
        self.data, self.max_share = data, decimal.Decimal(str(max_share))
        self.costs = cost_model or UnresolvedCosts()
        self.closure_policy, self.cash_policy = on_unconfirmed_closure, on_insufficient_cash
        self.portfolio = P.Portfolio(P.to_paise(capital_inr), {
            "ledger_seal": self.ledger.seal_hash, "venue": data.venue, "max_share": str(max_share),
            "on_unconfirmed_closure": on_unconfirmed_closure, "on_insufficient_cash": on_insufficient_cash,
            "liquidity_cap": liquidity_cap, "cost_model": type(self.costs).__name__})
        self.orders, self.statuses, self._last_mark = [], set(), None
        self.held_isin = {}   # the ISIN each holding was bought under; sells must match it

    # ------------------------------------------------------------ the clock

    def run(self):
        pending, clock = [], None
        for s in self.ledger.signals():
            t = s["decision_date"]
            pending, clock = self._advance(pending, clock, t)
            if self._last_mark != t:
                self._mark(t)
            pending = self._decide(s)
            clock = t
        while pending:
            last = self.data.last_session()
            if clock is None or last is None or clock >= last:
                for o in pending:
                    self._unfilled(o, None, "no later session in the data")
                pending = []
                break
            pending, clock = self._advance(pending, clock, last, stop_when_resolved=True)
        return self

    def _advance(self, pending, clock, until, stop_when_resolved=False):
        """Process each day after clock up to and including until: settle, then execute pending orders.
        After the last signal the clock stops at the day the last pending order is resolved."""
        if clock is None:
            return pending, until
        d = datetime.date.fromisoformat(clock) + datetime.timedelta(days=1)
        end = datetime.date.fromisoformat(until)
        while d <= end:
            day = d.isoformat()
            status = self.data.status_on(day)
            if status == "trading":
                self.portfolio.settle(day)
                self.portfolio.reconcile()
                if pending:
                    self._execute(pending, day)
                    pending = []
                    self._mark(day)
                    if stop_when_resolved:
                        return pending, day
            elif pending:
                if status in SKIP or (d.weekday() >= 5 and status == "unobserved"):
                    pass
                elif status == "no-file" and self.closure_policy == "roll":
                    for o in pending:
                        o["assumptions"].append("%s had no price file; treated as a closure (policy roll)" % day)
                elif status == "no-file" or status in UNFILLABLE:
                    for o in pending:
                        self._unfilled(o, day, "the next exchange day %s is %s; no fill is assumed" % (day, status))
                    pending = []
                    if stop_when_resolved:
                        return pending, day
                else:
                    raise BrokerError("unexpected calendar status %r on %s" % (status, day))
            d += datetime.timedelta(days=1)
        return pending, until

    # ------------------------------------------------------------ decisions

    def _decide(self, s):
        t = s["decision_date"]
        view = self.data.view(t)
        total, _, flags = self.portfolio.value(view)
        targets = {x["key"]: x for x in s["targets"]}
        keys = sorted(set(targets) | set(self.portfolio.holdings))
        pending = []
        for key in keys:
            held = self.portfolio.holdings.get(key, 0)
            weight = decimal.Decimal(targets[key]["weight"]) if key in targets else decimal.Decimal(0)
            order = {"signal_date": t, "submitted": t, "key": key,
                     "isin": self.held_isin.get(key) or targets[key]["isin"],
                     "requested": 0, "side": None, "status": "pending", "execution_date": None, "filled": 0,
                     "price_paise": None, "fee_paise": None, "cost_status": None, "reason": None,
                     "assumptions": [], "reference_flags": []}
            if weight == 0:
                desired = 0
            else:
                price, pflags = view.mark(key)
                if total is None or price is None:
                    order.update(status="not-created", reason="no %s on %s to size the order" % (
                        "portfolio value" if total is None else "reference price", t))
                    self._record(order)
                    continue
                order["reference_flags"] = pflags
                desired = int((weight * total) // P.to_paise(price))
            delta = desired - held
            if delta == 0:
                continue
            order.update(side="buy" if delta > 0 else "sell", requested=abs(delta))
            if self.cap_mode == "ex-ante":
                why = self._ex_ante(view, t, key, abs(delta))
                if why:
                    order.update(status="not-placed", reason=why)
                    self._record(order)
                    continue
            self._record(order)
            pending.append(order)
        return pending

    def _ex_ante(self, view, t, key, units):
        """None if the order is within max_share of the decision day's traded value, else why not."""
        bar = view.bar(key, t)
        price, _ = view.mark(key)
        if not bar or not bar.get("value") or price is None:
            return "no traded value for %s observed on the decision day %s; liquidity unknown" % (key, t)
        value_paise = decimal.Decimal(str(bar["value"])) * 100
        order = decimal.Decimal(units * P.to_paise(price))
        if order > self.max_share * value_paise:
            return "order value %s is above %s%% of the decision day's traded value %s" % (
                P.rupees(int(order)), self.max_share * 100, P.rupees(int(value_paise)))
        return None

    # ------------------------------------------------------------ execution

    def _execute(self, pending, day):
        view = self.data.view(day)
        for side in ("sell", "buy"):
            for o in [x for x in pending if x["side"] == side]:
                self._fill(o, view, day)
                self.portfolio.reconcile()

    def _fill(self, o, view, day):
        row = view.bar(o["key"], day)
        if not row or not row.get("value"):
            return self._unfilled(o, day, "no verified trade in %s on %s" % (o["key"], day))
        if row.get("isin") != o["isin"]:
            return self._unfilled(o, day, "%s carried %s on %s, not %s as when the order was placed"
                                  % (o["key"], row.get("isin"), day, o["isin"]))
        op, lo, hi = row.get("open"), row.get("low"), row.get("high")
        if not op or op <= 0 or (lo is not None and op < lo) or (hi is not None and op > hi):
            return self._unfilled(o, day, "the open %r is missing or outside the day's range" % (op,))
        price = P.to_paise(op)
        value_paise = decimal.Decimal(str(row["value"])) * 100
        units = o["requested"]
        if o["side"] == "sell":
            units = min(units, self.portfolio.holdings.get(o["key"], 0))
            if not units:
                return self._unfilled(o, day, "nothing held to sell")
        if self.cap_mode == "ex-post" and decimal.Decimal(units * price) > self.max_share * value_paise:
            return self._unfilled(o, day, "order value %s is above %s%% of the day's traded value %s" % (
                P.rupees(units * price), self.max_share * 100, P.rupees(int(value_paise))))
        ch = self._charges(o, units, price, day)
        if o["side"] == "sell":
            self.portfolio.sell(day, o["key"], units, price, ch.known_paise, o["seq"], ch.status == "unresolved")
        else:
            cash = self.portfolio.cash
            if units * price + ch.known_paise > cash:
                if self.cash_policy == "reject":
                    return self._unfilled(o, day, "needs %s, settled cash is %s" % (
                        P.rupees(units * price + ch.known_paise), P.rupees(cash)))
                while units > 0:
                    units -= 1
                    ch = self._charges(o, units, price, day) if units else ch
                    if units and units * price + ch.known_paise <= cash:
                        break
                if not units:
                    return self._unfilled(o, day, "cannot afford one unit at %s" % P.rupees(price))
                o["assumptions"].append("reduced from %d to %d units for settled cash (policy reduce)"
                                        % (o["requested"], units))
            self.portfolio.buy(day, o["key"], units, price, ch.known_paise, o["seq"], ch.status == "unresolved")
            self.held_isin[o["key"]] = o["isin"]
        audit = None
        if self.cap_mode == "ex-ante":
            audit = bool(decimal.Decimal(units * price) <= self.max_share * value_paise)
        o.update(status="filled", execution_date=day, filled=units, price_paise=price, fee_paise=ch.paise,
                 fee_known_paise=ch.known_paise, cost_status=ch.status, cost_components=ch.components,
                 within_cap_of_execution_day=audit)
        self.statuses.add(ch.status)
        self.portfolio.note({"type": "filled", "order": o["seq"], "date": day, "units": units, "price_paise": price,
                             "fee_paise": ch.paise, "fee_known_paise": ch.known_paise, "cost_status": ch.status,
                             "within_cap_of_execution_day": audit})

    def _charges(self, o, units, price, day):
        ch = self.costs.charges(o["side"], units, price, day, self.data.venue, o["key"])
        if not isinstance(ch, Charges):
            raise BrokerError("a cost model returns Charges")
        return ch

    def _unfilled(self, o, day, why):
        o.update(status="unfilled", execution_date=day, reason=why)
        self.portfolio.note({"type": "unfilled", "order": o["seq"], "date": day, "reason": why})

    def _record(self, o):
        o["seq"] = len(self.orders)
        self.orders.append(o)
        self.portfolio.note({"type": "order", "order": o["seq"], "signal_date": o["signal_date"], "key": o["key"],
                             "isin": o["isin"], "side": o["side"], "requested": o["requested"], "status": o["status"],
                             "reason": o["reason"], "reference_flags": o["reference_flags"]})

    def _mark(self, day):
        view = self.data.view(day)
        total, _, flags = self.portfolio.value(view)
        gaps = [g for g in view.gaps() if self._last_mark is None or g > self._last_mark]
        self.portfolio.note({"type": "mark", "date": day, "value_paise": total, "flags": flags,
                             "gaps_since_previous_mark": gaps})
        self._last_mark = day

    # ------------------------------------------------------------ results

    def result(self):
        p = self.portfolio
        return {"label": LABEL, "ledger_seal": self.ledger.seal_hash, "journal_digest": p.digest(),
                "cash_paise": p.cash, "unsettled_paise": sum(x for _, x in p.unsettled), "holdings": dict(p.holdings),
                "fees_known_paise": p.fees_known, "fills_with_unresolved_fees": p.fees_unknown,
                "cost_statuses": sorted(self.statuses), "orders": [dict(o) for o in self.orders]}
