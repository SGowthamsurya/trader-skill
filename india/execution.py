#!/usr/bin/env python3
"""Whether a simulated order could have been filled on a day. Feasibility only.

Rules (india/etf_criteria.json "execution"):

  session       the day must be tradable (calendar.tradable): a valid file
                was observed for it. A documented data gap, a no-file day
                or an unobserved day fills nothing.
  traded        the ETF must have a row in that day's verified file with a
                positive traded value. No row means no verified trade.
  size          the order's value must not exceed max_share_of_day_value
                (5 percent) of the ETF's traded value that day.

An order that breaks any rule is unfilled, with the reason. This module
does not set a fill price or any cost: those belong to a later phase and
must come from verified sources.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MAX_SHARE = 0.05


def check(order_value, day, row, cal, max_share=MAX_SHARE):
    """(filled, reason) for an order of order_value rupees on day."""
    if order_value is None or order_value <= 0:
        return False, "order value must be positive"
    if not cal.tradable(day):
        return False, "not a tradable session (%s)" % cal.status(day)
    if not row or row.get("identity_only") or not row.get("value") or row["value"] <= 0:
        return False, "no verified trade in this ETF on %s" % day
    share = order_value / row["value"]
    if share > max_share:
        return False, "order is %.1f%% of the day's traded value, above the %.0f%% limit" % (share * 100, max_share * 100)
    return True, "within %.0f%% of the day's traded value" % (max_share * 100)


def fillable_share(order_value, sessions, rows_by_date, key, cal, max_share=MAX_SHARE):
    """Share of the given sessions on which an order of this size would fill."""
    if not sessions:
        return None
    ok = sum(1 for d in sessions if check(order_value, d, rows_by_date.get(d, {}).get(key), cal, max_share)[0])
    return ok / float(len(sessions))
