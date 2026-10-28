#!/usr/bin/env python3
"""How a position is valued on any day, including days without a usable price.

These rules exist so that no later phase invents a price. They are pure
functions over a calendar (calendar_in.Calendar) and one instrument's
verified closes ({date: close}, from valid files only).

  mark          the close on that day if the day is a valid session with a
                price for the instrument. Otherwise the last valid close
                strictly before the day, flagged "stale"; never a later
                price (that would look ahead), never an interpolation. With
                no earlier price, there is no mark (None), flagged.
  return        between two marks. Flagged "stale-endpoint" when either mark
                is stale, and "spans-gap" when a documented data gap lies
                after the start and on or before the end. A flagged return
                is reported with its flags; callers must not present it as
                a clean return.
  signals and   only on tradable days (calendar.tradable): a documented
  trades        gap, a no-file day or an unobserved day carries neither.

Carrying the last valid close is the only valuation available on a gap day
that uses no later information and no invented number; the flag keeps every
figure that depends on it visible.
"""

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _iso(d):
    return d.isoformat() if isinstance(d, datetime.date) else str(d)


def mark(closes, day, cal):
    """(price or None, flags) for one instrument on one day."""
    d = _iso(day)
    if cal.tradable(d) and closes.get(d) is not None:
        return closes[d], []
    earlier = [k for k in closes if k < d and closes[k] is not None]
    status = cal.status(d)
    why = {"gap": "documented data gap", "invalid": "invalid file", "no-file": "no file (closure unconfirmed)",
           "unobserved": "not observed", "unreachable": "not observed (network)"}.get(status, status)
    if cal.tradable(d):
        why = "no trade in this instrument"
    if not earlier:
        return None, ["no-mark: %s on %s and no earlier valid close" % (why, d)]
    last = max(earlier)
    return closes[last], ["stale: %s on %s; carrying the valid close of %s" % (why, d, last)]


def gaps_between(cal, start, end):
    """Documented gaps after start and on or before end."""
    return [d.isoformat() for d in cal.days(start, end) if d.isoformat() > _iso(start) and cal.status(d) == "gap"]


def holding_return(closes, start, end, cal):
    """(return or None, flags) from the mark on start to the mark on end."""
    a, fa = mark(closes, start, cal)
    b, fb = mark(closes, end, cal)
    flags = fa + fb
    if a is None or b is None:
        return None, flags
    if any(f.startswith("stale") for f in flags):
        flags.append("stale-endpoint")
    g = gaps_between(cal, start, end)
    if g:
        flags.append("spans-gap: %s" % ", ".join(g))
    return b / a - 1.0, flags
