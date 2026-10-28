#!/usr/bin/env python3
"""Find candidate ETFs and test them against india/etf_criteria.json.

discover   reads the latest AMFI NAV file (scheme names by ISIN, a primary
           source) and the latest price file of the venue, and writes every
           listed ETF whose official scheme name puts it in a category to
           india/etf_candidates.json, with the checksums of both files.
evaluate   tests every candidate against the criteria and prints a verdict:

  ELIGIBLE           every criterion passes
  NOT ELIGIBLE       a criterion fails on the evidence there is
  INSUFFICIENT DATA  nothing fails, but something cannot be decided yet

Criteria (thresholds in etf_criteria.json):

  category      the AMFI scheme name puts it in the category
  identity      no duplicate or overlapping mappings for its ISINs, and
                every ISIN change on its listing is confirmed with a source
                (india/identity_confirmations.json)
  start         first observed on the venue within start_tolerance_days of
                the window start
  complete      every weekday of the window is accounted for in the venue
                calendar (a file, or a refusal of every layout)
  presence      it has a row on at least min_presence of the venue's
                observed sessions in the window
  corporate     every break beyond break_ratio between adjacent sessions
                matches a verified action (india/corporate_actions.json): on
                its ex-date, or, where the document gives only a record date,
                on the record date or the session before it. The ratio is the
                document's; the observed price change must agree with it within
                ratio_tolerance or the criterion fails. A break between
                sessions that are not adjacent cannot be located and stays
                unresolved
  liquidity     median daily traded value on the venue at or above the
                threshold (unresolved while no threshold is set). Sessions
                from the first observed day on which it did not trade count
                as zero: a buyer cannot trade on them.
  affordable    no observed close above capital_inr; a pass needs a
                complete history

    python3 etf.py discover BSE
    python3 etf.py evaluate
    python3 etf.py evaluate --markdown > india/ETF_REVIEW.md
    python3 etf.py liquidity --thresholds 50000 100000 300000 1000000   # what each threshold would do
"""

import argparse
import datetime
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import bhav  # noqa: E402
import calendar_in  # noqa: E402
import fetch  # noqa: E402
import secmaster  # noqa: E402

CRITERIA = os.path.join(HERE, "etf_criteria.json")
CANDIDATES = os.path.join(HERE, "etf_candidates.json")
ACTIONS = os.path.join(HERE, "corporate_actions.json")

RULES = (
    ("nifty50", re.compile(r"\bnifty\s*50\b", re.I),
     re.compile(r"next|value|quality|equal|alpha|momentum|low vol|shariah|index fund|fund of fund|\bfof\b|"
                r"arbitrage|dividend|nifty 500|nifty 50 value", re.I)),
    ("niftynext50", re.compile(r"\bnext\s*50\b", re.I),
     re.compile(r"sensex|bse|index fund|fund of fund|\bfof\b", re.I)),
    ("bank", re.compile(r"\bnifty\s*bank\b", re.I),
     re.compile(r"psu|private|pvt|top 10|10 banks|bse|sensex|index fund|fund of fund|\bfof\b", re.I)),
)


ACTION_FIELDS = ("id", "exchange", "key", "type", "units_multiplier", "record_date", "ex_date", "verification",
                 "documents")
RATIO_TOLERANCE = 0.15


class EtfError(Exception):
    pass


def read_actions(path=None):
    doc = read_json(path or ACTIONS, "corporate actions")
    acts = doc.get("actions", [])
    for i, a in enumerate(acts):
        for k in ACTION_FIELDS:
            if k not in a:
                raise EtfError("corporate action #%d has no %s" % (i, k))
        if a["verification"] not in ("verified", "unverified"):
            raise EtfError("corporate action %s: verification must be verified or unverified" % a["id"])
        if not a["record_date"] and not a["ex_date"]:
            raise EtfError("corporate action %s needs a record date or an ex-date" % a["id"])
        if a["verification"] == "verified":
            if not (isinstance(a["units_multiplier"], (int, float)) and a["units_multiplier"] > 0):
                raise EtfError("corporate action %s: a verified action states its units multiplier" % a["id"])
            if not a["documents"]:
                raise EtfError("corporate action %s: a verified action cites a document" % a["id"])
        for d in a["documents"]:
            if not str(d.get("url", "")).startswith("https://") or not re.match(r"^[0-9a-f]{64}$", d.get("sha256", "")):
                raise EtfError("corporate action %s: each document needs an https url and a sha256" % a["id"])
    return acts


def match_action(actions, cal, venue, key, a, b, ratio):
    """('confirmed', action), ('mismatch', action) or (None, None) for a break between a and b."""
    for x in actions:
        if x.get("verification") != "verified" or x.get("exchange") != venue or str(x.get("key")) != key:
            continue
        hit = x.get("ex_date") == b
        if not hit and not x.get("ex_date") and x.get("record_date"):
            rd = datetime.date.fromisoformat(x["record_date"])
            before = cal.sessions(rd - datetime.timedelta(days=10), rd - datetime.timedelta(days=1))
            hit = b == x["record_date"] or (bool(before) and b == before[-1].isoformat())
        if hit:
            ok = abs(ratio * x["units_multiplier"] - 1) <= RATIO_TOLERANCE
            return ("confirmed" if ok else "mismatch"), x
    return None, None


def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise EtfError("cannot read %s (%s): %s" % (what, path, e))


def _value(c):
    return c["value"] if isinstance(c, dict) else c


def amfi_names(text):
    """{isin: scheme name} from AMFI's NAVAll.txt."""
    out = {}
    for line in text.splitlines():
        p = line.split(";")
        if len(p) < 6 or not p[0].strip().isdigit():
            continue
        for isin in (p[1].strip(), p[2].strip()):
            if isin.startswith("INF"):
                out[isin] = p[3].strip()
    return out


def category(name):
    if not name or not re.search(r"\betf\b|bees", name, re.I):
        return None
    for cat, inc, exc in RULES:
        if inc.search(name) and not exc.search(name):
            return cat
    return None


def discover(cache, venue):
    a = cache.latest(source="amfi-nav-latest", ok=True)
    if not a:
        raise EtfError("no AMFI NAV file cached; run: fetch.py get amfi-nav-latest")
    names = amfi_names(cache.read(a["sha256"]).decode("utf-8", "replace"))
    obs = bhav.observations(cache, venue)
    if not obs:
        raise EtfError("no %s price files cached" % venue)
    rec, _, rows = obs[-1]
    out = []
    for r in rows:
        cat = category(names.get(r["isin"]))
        if cat:
            out.append({"category": cat, "isin": r["isin"], "exchange": r["exchange"], "key": secmaster.key_of(r),
                        "symbol": secmaster.label_of(r), "amfi_name": names[r["isin"]]})
    out.sort(key=lambda c: (c["category"], c["symbol"]))
    return {"about": "ETFs whose official AMFI scheme name places them in a study category, as listed on the "
                     "venue's latest cached price file. Discovery only: inclusion is decided by etf.py evaluate.",
            "venue": venue, "price_file": {"date": rows[0]["date"], "sha256": rec["sha256"], "url": rec["url"]},
            "amfi_file": {"sha256": a["sha256"], "url": a["url"], "retrieved_at": a["retrieved_at"]},
            "candidates": out}


def evaluate(cand, crit, cal, master, rows_by_date, actions, end):
    start = crit["window"]["start"]
    sessions = [d.isoformat() for d in cal.sessions(start, end)]
    mine = []
    for d in sessions:
        x = rows_by_date.get(d, {}).get(cand["key"])
        if x:
            mine.append(x)
    res = {}

    res["category"] = ("pass", cand["amfi_name"])

    chain = master.chain(cand["isin"])
    conf = master.conflicts_for(chain["isins"])
    pending = [e for e in chain["links"] if e["status"] != "confirmed"]
    if conf:
        res["identity"] = ("fail", "; ".join(c["detail"] for c in conf))
    elif pending:
        res["identity"] = ("unresolved", "; ".join("%s %s: %s -> %s between %s and %s, %s" % (
            e["exchange"], e["key"], e["from_isin"], e["to_isin"], e["after"], e["on_or_before"],
            "corroborated but not verified" if e["status"] == "corroborated" else "not confirmed") for e in pending))
    else:
        res["identity"] = ("pass", "ISINs %s" % ", ".join(chain["isins"]))

    tol = _value(crit["start_tolerance_days"])
    if not mine:
        res["start"] = ("fail", "never observed on %s in the window" % crit["venue"])
    else:
        first = mine[0]["date"]
        late = (datetime.date.fromisoformat(first) - datetime.date.fromisoformat(start)).days
        res["start"] = ("pass" if late <= tol else "fail", "first observed %s" % first)

    cov = cal.coverage(start, end)
    missing = cov["weekday"].get("unobserved", 0) + cov["weekday"].get("unreachable", 0)
    res["complete"] = ("pass", "every weekday accounted for") if cov["complete_weekdays"] else (
        "unresolved", "%d weekdays in the window have no file and no refusal on record" % missing)

    share = len(mine) / float(len(sessions)) if sessions else 0.0
    need = _value(crit["min_presence"])
    res["presence"] = ("pass" if share >= need else "fail",
                       "on %d of %d observed sessions (%.1f%%)" % (len(mine), len(sessions), share * 100))

    ratio = _value(crit["break_ratio"])
    located, unlocated = [], []
    for a, b in zip(mine, mine[1:]):
        if not (a["close"] and b["close"]):
            continue
        r = b["close"] / a["close"]
        if r > ratio or r < 1 / ratio:
            ok, assumed = cal.adjacent(a["date"], b["date"])
            (located if ok else unlocated).append((a["date"], b["date"], r, assumed))
    open_breaks, mismatched, explained = [], [], []
    for a, b, r, assumed in located:
        how, act = match_action(actions, cal, crit["venue"], cand["key"], a, b, r)
        if how == "confirmed":
            explained.append("%s -> %s by %s" % (a, b, act["id"]))
        elif how == "mismatch":
            mismatched.append("%s -> %s moved %.4fx but %s states 1/%s" % (a, b, r, act["id"], act["units_multiplier"]))
        else:
            open_breaks.append("%s -> %s ratio %.4f, not confirmed" % (a, b, r))
    for a, b, r, _ in unlocated:
        open_breaks.append("%s .. %s ratio %.4f, between sessions that are not adjacent" % (a, b, r))
    done = "; ".join(explained) or "no breaks beyond %.2fx between observed sessions" % ratio
    res["corporate"] = ("fail", "; ".join(mismatched)) if mismatched else (
        ("unresolved", "; ".join(open_breaks)) if open_breaks else
        ("pass", done) if cov["complete_weekdays"] else
        ("unresolved", "%s, but the history is incomplete" % done))

    med = daily_values(sessions, mine)["median"]
    thr = crit["liquidity"].get("min_median_value_inr")
    shown = "median traded value Rs %s on %s" % ("{:,.0f}".format(med) if med is not None else "n/a", crit["venue"])
    if thr is None:
        res["liquidity"] = ("unresolved", shown + "; threshold not chosen")
    else:
        res["liquidity"] = ("pass" if med is not None and med >= thr else "fail", shown)

    cap = crit["capital_inr"]
    over = [x for x in mine if x["close"] and x["close"] > cap]
    if over:
        top = max(over, key=lambda x: x["close"])
        res["affordable"] = ("fail", "one unit closed at Rs %.2f on %s (%d observed days above Rs %s)"
                             % (top["close"], top["date"], len(over), cap))
    else:
        hi = max((x["close"] for x in mine if x["close"]), default=None)
        res["affordable"] = (("pass" if cov["complete_weekdays"] else "unresolved"),
                             "highest observed close Rs %s" % ("%.2f" % hi if hi else "n/a"))

    outcomes = [v[0] for v in res.values()]
    verdict = "NOT ELIGIBLE" if "fail" in outcomes else "INSUFFICIENT DATA" if "unresolved" in outcomes else "ELIGIBLE"
    return verdict, res


def daily_values(sessions, mine):
    """Traded value per venue session from the first day observed, a missed session counting as zero."""
    if not mine:
        return {"median": None, "p10": None, "sessions": 0, "traded": 0}
    by = {x["date"]: (x["value"] or 0.0) for x in mine}
    span = [d for d in sessions if d >= mine[0]["date"]]
    vals = sorted(by.get(d, 0.0) for d in span)
    return {"median": statistics.median(vals), "p10": vals[len(vals) // 10], "sessions": len(span),
            "traded": sum(1 for d in span if by.get(d, 0.0) > 0)}


def liquidity_table(cache, crit, cands, thresholds):
    venue = crit["venue"]
    obs = bhav.observations(cache, venue)
    cal = calendar_in.from_cache(cache, venue)
    end = crit["window"]["end"] or (cal.last_observed.isoformat() if cal.last_observed else crit["window"]["start"])
    sessions = [d.isoformat() for d in cal.sessions(crit["window"]["start"], end)]
    rows_by_date = {rows[0]["date"]: {secmaster.key_of(x): x for x in rows if x["exchange"] == venue}
                    for _, _, rows in obs}
    out = []
    for c in cands:
        if c["exchange"] != venue:
            continue
        mine = [rows_by_date[d][c["key"]] for d in sessions if c["key"] in rows_by_date.get(d, {})]
        v = daily_values(sessions, mine)
        v.update({"candidate": c, "first": mine[0]["date"] if mine else None,
                  "capital_share": (crit["capital_inr"] / v["median"]) if v["median"] else None,
                  "passes": {t: bool(v["median"] is not None and v["median"] >= t) for t in thresholds}})
        out.append(v)
    return end, out


def evaluate_all(cache, crit, cands, actions, confirmations=secmaster.CONFIRMATIONS):
    venue = crit["venue"]
    obs = bhav.observations(cache, venue)
    cal = calendar_in.from_cache(cache, venue)
    master = secmaster.Master([(r["sha256"], rows) for r, _, rows in obs],
                              secmaster.read_confirmations(confirmations) if isinstance(confirmations, str)
                              else confirmations)
    rows_by_date = {}
    for _, _, rows in obs:
        rows_by_date[rows[0]["date"]] = {secmaster.key_of(x): x for x in rows if x["exchange"] == venue}
    end = crit["window"]["end"] or (cal.last_observed.isoformat() if cal.last_observed else crit["window"]["start"])
    out = []
    for c in cands:
        if c["exchange"] != venue:
            continue
        verdict, res = evaluate(c, crit, cal, master, rows_by_date, actions, end)
        out.append({"candidate": c, "verdict": verdict, "criteria": res})
    return end, out


def markdown(end, results, crit, cands_doc):
    lines = ["# ETF eligibility review (%s, %s to %s)" % (crit["venue"], crit["window"]["start"], end), "",
             "Generated by `python3 india/etf.py evaluate --markdown` from the local cache. Candidates from "
             "AMFI file sha256 `%s` and %s price file of %s (sha256 `%s`)."
             % (cands_doc["amfi_file"]["sha256"][:16], cands_doc["venue"], cands_doc["price_file"]["date"],
                cands_doc["price_file"]["sha256"][:16]), "",
             "Venue note: %s" % crit["venue_note"], "",
             "| category | symbol | key | ISIN | verdict | failing or unresolved |", "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        c = r["candidate"]
        bad = ["**%s** %s: %s" % (k, v[0], v[1]) for k, v in r["criteria"].items() if v[0] != "pass"]
        lines.append("| %s | %s | %s | %s | %s | %s |" % (c["category"], c["symbol"], c["key"], c["isin"],
                                                         r["verdict"], "<br>".join(bad).replace("|", "/")))
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    lines += ["", "Verdicts: %s." % ", ".join("%s %d" % kv for kv in sorted(counts.items())), "",
              "Not financial advice. Verdicts describe data quality and evidence, not investment merit."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Discover and evaluate candidate ETFs for the India study.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    ap.add_argument("--criteria", default=CRITERIA)
    ap.add_argument("--candidates", default=CANDIDATES)
    ap.add_argument("--actions", default=ACTIONS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pd = sub.add_parser("discover")
    pd.add_argument("venue", choices=sorted(bhav.VENUES))
    pe = sub.add_parser("evaluate")
    pe.add_argument("--markdown", action="store_true")
    pe.add_argument("--json", action="store_true")
    pl = sub.add_parser("liquidity")
    pl.add_argument("--thresholds", type=float, nargs="+", default=[50000, 100000, 300000, 1000000, 10000000])
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "discover":
            doc = discover(cache, a.venue)
            with open(a.candidates, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
                fh.write("\n")
            print("%d candidates written to %s" % (len(doc["candidates"]), a.candidates))
            return 0
        crit = read_json(a.criteria, "criteria")
        cands_doc = read_json(a.candidates, "candidates (run etf.py discover first)")
        if a.cmd == "liquidity":
            end, rows = liquidity_table(cache, crit, cands_doc["candidates"], a.thresholds)
            ts = a.thresholds
            print("%s, %s to %s; daily traded value in rupees, missed sessions counted as zero" % (
                crit["venue"], crit["window"]["start"], end))
            print("%-12s %-12s %10s %13s %13s %9s  %s" % ("category", "symbol", "first", "median", "10th pct",
                                                       "traded", "  ".join("%g" % t for t in ts)))
            for r in sorted(rows, key=lambda r: -(r["median"] or 0)):
                c = r["candidate"]
                print("%-12s %-12s %10s %13s %13s %8.1f%%  %s" % (
                    c["category"], c["symbol"], r["first"] or "-",
                    "{:,.0f}".format(r["median"]) if r["median"] is not None else "-",
                    "{:,.0f}".format(r["p10"]) if r["p10"] is not None else "-",
                    100.0 * r["traded"] / r["sessions"] if r["sessions"] else 0.0,
                    "  ".join(("pass" if r["passes"][t] else "fail").rjust(len("%g" % t)) for t in ts)))
            for t in ts:
                print("threshold Rs %s: %d of %d pass" % ("{:,.0f}".format(t), sum(r["passes"][t] for r in rows), len(rows)))
            return 0
        actions = read_actions(a.actions)
        end, results = evaluate_all(cache, crit, cands_doc["candidates"], actions)
        if a.markdown:
            sys.stdout.write(markdown(end, results, crit, cands_doc))
        elif a.json:
            print(json.dumps({"end": end, "results": results}, indent=2))
        else:
            for r in results:
                c = r["candidate"]
                print("%-17s %-12s %-12s %-7s %s" % (r["verdict"], c["category"], c["symbol"], c["key"], c["isin"]))
                for k, (o, why) in r["criteria"].items():
                    if o != "pass":
                        print("    %-10s %-10s %s" % (k, o, why))
    except (EtfError, bhav.BhavError, fetch.FetchError, secmaster.SecError, calendar_in.CalendarError) as e:
        print("etf.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
