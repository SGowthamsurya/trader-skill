#!/usr/bin/env python3
"""Metrics for one simulated run, computed only from the broker's journal and orders.

Every figure is exact (integer paise, Decimal ratios rendered as strings) so
two runs on the same inputs produce byte-identical output. Nothing here
decides whether a figure is a valid result: the caller passes the label the
readiness gate allowed (a READY real run, or MECHANICS ONLY). A metric that
cannot be computed honestly is reported as null with the reason, never
estimated:

  return        net of known costs; when any fill carries an unresolved fee
                the label says the true return is lower by an unknown amount
  CAGR          only over at least 365 days between the first and last mark
  drawdown      over the mark dates (start capital included as the first peak);
                a mark with no value (an ETF with no price) is skipped and
                counted
  volatility    annualised from monthly returns between decision-date marks,
                only with at least 24 of them
  turnover      traded value of fills over the mean known mark value
  invested      share of calendar days between the first and last mark on
                which any unit was held
  benchmarks    only the series the caller passes, which must already be
                valid; cash is the zero-return control
"""

import decimal
import hashlib
import json

D = decimal.Decimal
Q = D("0.000001")
CTX = decimal.Context(prec=34)
MIN_CAGR_DAYS = 365
MIN_VOL_RETURNS = 24


def _s(x):
    return None if x is None else str(x.quantize(Q, rounding=decimal.ROUND_HALF_EVEN, context=CTX))


def _days(a, b):
    import datetime
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def marks(journal):
    return [(e["date"], e["value_paise"], e["flags"], e.get("gaps_since_previous_mark", []))
            for e in journal if e["type"] == "mark"]


def drawdown(points):
    """points: [(date, value)] with known values. (max drawdown as a positive fraction, peak date, trough date)."""
    peak, peak_d, worst, at = None, None, D(0), (None, None)
    for d, v in points:
        v = D(v)
        if peak is None or v > peak:
            peak, peak_d = v, d
        elif peak > 0:
            dd = (peak - v) / peak
            if dd > worst:
                worst, at = dd, (peak_d, d)
    return worst, at[0], at[1]


def period_return(a, b):
    return None if a in (None, 0) or b is None else D(b) / D(a) - 1


def cagr(ret, days):
    if ret is None or days < MIN_CAGR_DAYS or ret <= -1:
        return None
    return CTX.power(1 + ret, D(365) / D(days)) - 1


def volatility(returns):
    if len(returns) < MIN_VOL_RETURNS:
        return None
    n = D(len(returns))
    mean = sum(returns, D(0)) / n
    var = sum(((r - mean) ** 2 for r in returns), D(0)) / (n - 1)
    return CTX.sqrt(var) * CTX.sqrt(D(12))


def invested_share(journal, first, last):
    """Share of calendar days in [first, last) on which a unit was held, replaying fills in journal order."""
    if not first or not last or first >= last:
        return None
    held, changes = {}, []
    for e in journal:
        if e["type"] in ("buy", "sell"):
            held[e["key"]] = held.get(e["key"], 0) + (e["units"] if e["type"] == "buy" else -e["units"])
            changes.append((e["date"], any(v for v in held.values())))
    state, days, cursor = False, 0, first
    for d, s in changes:
        if d <= first:
            state = s
            continue
        if d >= last:
            break
        if state:
            days += _days(cursor, d)
        cursor, state = d, s
    if state:
        days += _days(cursor, last)
    return D(days) / D(_days(first, last))


def benchmark(series, first, last):
    """series: [(date, value)] oldest first, already validated. Return and drawdown over [first, last], using the
    last value on or before each end."""
    inside = [(d, v) for d, v in series if first <= d <= last]
    before = [(d, v) for d, v in series if d <= first]
    if not before or not inside:
        return {"return": None, "max_drawdown": None, "why": "the series does not cover %s to %s" % (first, last)}
    start_v, end_v = before[-1][1], inside[-1][1]
    dd, _, _ = drawdown([(d, D(str(v))) for d, v in [before[-1]] + inside])
    return {"return": _s(period_return(D(str(start_v)), D(str(end_v)))), "max_drawdown": _s(dd),
            "start": before[-1][0], "end": inside[-1][0]}


def evaluate(result, journal, decision_dates, label, benchmarks=None):
    """result: Broker.result(); journal: the portfolio journal; decision_dates: the ledger's signal dates;
    benchmarks: {name: [(date, value)]} of valid benchmark series only."""
    capital = journal[0]["capital_paise"]
    ms = marks(journal)
    known = [(d, v) for d, v, _, _ in ms if v is not None]
    first = ms[0][0] if ms else None
    last = ms[-1][0] if ms else None
    end_value = ms[-1][1] if ms else None
    orders = result["orders"]
    filled = [o for o in orders if o["status"] == "filled"]
    unresolved_fills = sum(1 for o in filled if o.get("cost_status") == "unresolved")
    ret = period_return(capital, end_value)
    span = _days(first, last) if first and last else 0

    dd, peak_d, trough_d = drawdown([(first, capital)] + known if first else [])
    monthly, prev = [], None
    by_date = dict((d, v) for d, v in known)
    for d in list(decision_dates) + ([last] if last and last not in decision_dates else []):
        v = by_date.get(d)
        if v is None:
            prev = None
            continue
        if prev is not None:
            monthly.append(D(v) / D(prev) - 1)
        prev = v
    traded = sum(o["filled"] * o["price_paise"] for o in filled)
    mean_mark = (sum(D(v) for _, v in known) / len(known)) if known else None

    counts = {}
    for o in orders:
        k = "%s %s" % (o["side"] or "none", o["status"])
        counts[k] = counts.get(k, 0) + 1
    fees = {}
    for o in filled:
        for c in o.get("cost_components") or []:
            f = fees.setdefault(c["component"], {"known_paise": 0, "unresolved_fills": 0, "status": set()})
            if c.get("paise") is None:
                f["unresolved_fills"] += 1
            else:
                f["known_paise"] += c["paise"]
            f["status"].add(c.get("status"))
    for f in fees.values():
        f["status"] = sorted(x for x in f["status"] if x)

    stale = sum(1 for _, _, flags, _ in ms for x in flags if "stale" in x)
    gaps = sorted({g for _, _, _, gs in ms for g in gs})
    out = {
        "label": label,
        "window": {"first_mark": first, "last_mark": last, "calendar_days": span,
                   "decisions": len(decision_dates)},
        "start_value_paise": capital,
        "end_value_paise": end_value,
        "end_cash_paise": result["cash_paise"] + result["unsettled_paise"],
        "end_holdings": result["holdings"],
        "return": _s(ret),
        "return_label": ("net of known costs only: %d fill(s) carry unresolved fees, so the true return is lower by "
                         "an unknown amount" % unresolved_fills) if unresolved_fills else "net of all costs (pre-tax)",
        "cagr": _s(cagr(ret, span)),
        "cagr_note": None if span >= MIN_CAGR_DAYS else "fewer than %d days; not annualised" % MIN_CAGR_DAYS,
        "max_drawdown": _s(dd), "max_drawdown_from": peak_d, "max_drawdown_to": trough_d,
        "monthly_returns": len(monthly),
        "volatility_annualised": _s(volatility(monthly)),
        "volatility_note": None if len(monthly) >= MIN_VOL_RETURNS else
        "fewer than %d monthly returns; not computed" % MIN_VOL_RETURNS,
        "traded_value_paise": traded,
        "turnover": _s(D(traded) / mean_mark) if mean_mark else None,
        "orders": dict(sorted(counts.items())),
        "fills_with_unresolved_fees": unresolved_fills,
        "fees_known_paise": result["fees_known_paise"],
        "fees_by_component": dict(sorted(fees.items())),
        "invested_share": _s(invested_share(journal, first, last)),
        "marks": len(ms), "marks_without_value": len(ms) - len(known),
        "stale_mark_flags": stale, "gaps_crossed": gaps,
        "benchmarks": {"uninvested cash": {"return": _s(D(0)), "max_drawdown": _s(D(0)),
                                           "note": "zero by construction; a control, not a market benchmark"}},
    }
    for name, series in sorted((benchmarks or {}).items()):
        b = benchmark(series, first, last) if first else {"return": None, "why": "no marks"}
        if b.get("return") is not None and ret is not None:
            b["relative_return"] = _s(ret - D(b["return"]))
        out["benchmarks"][name] = b
    return out


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
