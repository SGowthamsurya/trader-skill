#!/usr/bin/env python3
"""Portfolio accounting in integer paise: cash, unsettled proceeds, whole units, fees, a journal.

Money is never a binary float here. A price from a price file becomes paise
through Decimal(str(price)) * 100 and must be a whole number of paise; one
that is not is refused, never rounded. Quantities are whole units.

State:
  cash         settled cash, usable for buys
  unsettled    sale proceeds not yet usable (they settle on the next
               tradable session after the sale; never usable the same day)
  holdings     {key: units}, never negative
  fees_known   fees whose amount is known (paise)
  fees_unknown number of fills whose fees are unresolved

Every change goes through the journal (canonical JSON entries with integer
money and a running SHA-256), and reconcile() recomputes cash, unsettled
proceeds, holdings and fees from the journal alone and compares them with
the state exactly; the broker calls it after every event.

Valuation (value()): every holding is marked through marks.mark on a
past-only pit.View. A stale mark keeps its flag; a holding with no mark
makes the total None (unknown), never zero.
"""

import decimal
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import signals  # noqa: E402  (canonical JSON)


class AccountingError(Exception):
    pass


def to_paise(rupees):
    """Exact paise for a price or amount in rupees; AccountingError if it is not a whole number of paise."""
    if rupees is None:
        raise AccountingError("no price")
    if isinstance(rupees, bool):
        raise AccountingError("not a price: %r" % rupees)
    d = decimal.Decimal(str(rupees)) * 100
    if not d.is_finite() or d != d.to_integral_value():
        raise AccountingError("%r rupees is not a whole number of paise" % rupees)
    return int(d)


def rupees(paise):
    """A display string; never used for arithmetic."""
    sign = "-" if paise < 0 else ""
    return "%s%d.%02d" % (sign, abs(paise) // 100, abs(paise) % 100)


class Portfolio(object):
    def __init__(self, capital_paise, meta=None):
        if not isinstance(capital_paise, int) or capital_paise <= 0:
            raise AccountingError("capital must be a positive whole number of paise")
        self.cash, self.unsettled, self.holdings = capital_paise, [], {}
        self.fees_known, self.fees_unknown = 0, 0
        self.journal, self.chain = [], "0" * 64
        self._post({"type": "start", "capital_paise": capital_paise, "meta": meta or {}})

    def _post(self, entry):
        e = dict(entry, seq=len(self.journal), prev=self.chain)
        body = signals.canonical(e)
        self.chain = hashlib.sha256(body.encode("ascii")).hexdigest()
        self.journal.append(e)
        return e

    def note(self, entry):
        """A journal entry that moves no money (orders, unfilled orders, marks)."""
        if entry.get("type") in ("buy", "sell", "settle", "start"):
            raise AccountingError("money moves only through buy(), sell() and settle()")
        return self._post(entry)

    def buy(self, day, key, units, price_paise, fee_paise, order_seq):
        """fee_paise None means unresolved: excluded from cash and counted, never treated as zero."""
        if not isinstance(units, int) or units <= 0:
            raise AccountingError("a buy is a positive whole number of units")
        cost = units * price_paise + (fee_paise or 0)
        if cost > self.cash:
            raise AccountingError("buy of %d %s costs %s, settled cash is %s" % (units, key, rupees(cost),
                                                                                rupees(self.cash)))
        self.cash -= cost
        self.holdings[key] = self.holdings.get(key, 0) + units
        self._fee(fee_paise)
        return self._post({"type": "buy", "date": day, "key": key, "units": units, "price_paise": price_paise,
                           "fee_paise": fee_paise, "cash_paise": -cost, "order": order_seq})

    def sell(self, day, key, units, price_paise, fee_paise, order_seq):
        if not isinstance(units, int) or units <= 0:
            raise AccountingError("a sell is a positive whole number of units")
        if units > self.holdings.get(key, 0):
            raise AccountingError("cannot sell %d %s, holding %d" % (units, key, self.holdings.get(key, 0)))
        proceeds = units * price_paise - (fee_paise or 0)
        self.holdings[key] -= units
        if not self.holdings[key]:
            del self.holdings[key]
        self.unsettled.append((day, proceeds))
        self._fee(fee_paise)
        return self._post({"type": "sell", "date": day, "key": key, "units": units, "price_paise": price_paise,
                           "fee_paise": fee_paise, "unsettled_paise": proceeds, "order": order_seq})

    def _fee(self, fee_paise):
        if fee_paise is None:
            self.fees_unknown += 1
        else:
            if not isinstance(fee_paise, int) or fee_paise < 0:
                raise AccountingError("a fee is a whole, non-negative number of paise")
            self.fees_known += fee_paise

    def settle(self, day):
        """Proceeds of sales made before `day` become cash on `day` (the next tradable session)."""
        due = [(d, p) for d, p in self.unsettled if d < day]
        if not due:
            return None
        self.unsettled = [(d, p) for d, p in self.unsettled if d >= day]
        amount = sum(p for _, p in due)
        self.cash += amount
        return self._post({"type": "settle", "date": day, "cash_paise": amount,
                           "from_sales_on": sorted({d for d, _ in due})})

    def reconcile(self):
        """Recompute everything from the journal; AccountingError on any difference."""
        cash, unsettled, holdings, known, unknown = 0, 0, {}, 0, 0
        for e in self.journal:
            t = e["type"]
            if t == "start":
                cash += e["capital_paise"]
            elif t == "buy":
                cash += e["cash_paise"]
                holdings[e["key"]] = holdings.get(e["key"], 0) + e["units"]
            elif t == "sell":
                unsettled += e["unsettled_paise"]
                holdings[e["key"]] = holdings.get(e["key"], 0) - e["units"]
            elif t == "settle":
                cash += e["cash_paise"]
                unsettled -= e["cash_paise"]
            if t in ("buy", "sell"):
                if e["fee_paise"] is None:
                    unknown += 1
                else:
                    known += e["fee_paise"]
        holdings = {k: v for k, v in holdings.items() if v}
        state = (self.cash, sum(p for _, p in self.unsettled), self.holdings, self.fees_known, self.fees_unknown)
        if (cash, unsettled, holdings, known, unknown) != state:
            raise AccountingError("journal and state disagree: journal %r, state %r"
                                  % ((cash, unsettled, holdings, known, unknown), state))
        if cash < 0 or unsettled < 0 or any(v < 0 for v in holdings.values()):
            raise AccountingError("negative cash, proceeds or holdings")
        return True

    def value(self, view):
        """(total paise or None, {key: (units, mark paise or None, flags)}, flags) on the view's date."""
        out, flags, total = {}, [], self.cash + sum(p for _, p in self.unsettled)
        for key in sorted(self.holdings):
            price, f = view.mark(key)
            if price is None:
                out[key] = (self.holdings[key], None, f)
                flags.append("%s has no mark: total unknown" % key)
                total = None
                continue
            p = to_paise(price)
            out[key] = (self.holdings[key], p, f)
            flags.extend("%s: %s" % (key, x) for x in f)
            if total is not None:
                total += self.holdings[key] * p
        if self.fees_unknown:
            flags.append("%d fill(s) with unresolved fees: cash excludes them" % self.fees_unknown)
        return total, out, flags

    def digest(self):
        return self.chain
