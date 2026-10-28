#!/usr/bin/env python3
"""Reproducible runner for the pre-registered India experiment (protocol.json).

Commands:
  readiness   run the readiness gate on the real cache and write
              india/READINESS_REPORT.md and india/readiness_report.json
  run         the real-market experiment. Refuses (exit 3, "BLOCKED - NO
              VALID PERFORMANCE RESULT") unless the gate is READY, and again
              if the loaded market data is not from the verified cache.
              Writes into an output folder under the ignored india/data/.
  mechanics   the same pipeline on SYNTHETIC data built in code, to show
              the machinery works. Every output carries
              "MECHANICS ONLY - NOT A PERFORMANCE RESULT".

The pipeline, in order, for either mode:
  1. decision dates: the first tradable session of each calendar month in
     the window (knowable on the day, no later information);
  2. signals: each date's point-in-time universe and past-only view go to
     the strategy (in a separate process); one ledger record per date;
     the ledger is sealed and the seal hash is written to a separate
     anchor file;
  3. execution: the broker opens the ledger only with that anchor, and
     simulates orders, fills, fees and settlement with the protocol's
     policies; the portfolio is marked at the last session of the window;
  4. sensitivity: the same sealed ledger re-executed under each
     pre-specified alternative policy (signals do not depend on them);
  5. evaluation: evaluate.py metrics from the journal; benchmarks only
     when the gate found them valid; uninvested cash always, as a control;
  6. manifest: everything needed to rerun and check the result.

Two runs on the same inputs produce the same output digest (the seal time
is the only timestamp, and it is outside the digest).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import evaluate  # noqa: E402
import readiness  # noqa: E402
import signals  # noqa: E402
import simbroker  # noqa: E402

MECHANICS = "MECHANICS ONLY - NOT A PERFORMANCE RESULT"
REAL = "REAL-MARKET RESULT (readiness READY)"
BLOCKED = "BLOCKED - NO VALID PERFORMANCE RESULT"
RUNS = os.path.join(HERE, "data", "runs")
REPORT_MD = os.path.join(HERE, "READINESS_REPORT.md")
REPORT_JSON = os.path.join(HERE, "readiness_report.json")
SYNTHETIC_SEAL_TIME = "2000-01-01T00:00:00Z"


class Refused(Exception):
    """The runner will not produce a real-market result."""

    def __init__(self, msg, report=None):
        Exception.__init__(self, msg)
        self.report = report


def first_sessions(data, start, end):
    """The first tradable session of each calendar month in [start, end]."""
    out, seen = [], set()
    for d in data.sessions_between(start, end):
        if d[:7] not in seen:
            seen.add(d[:7])
            out.append(d)
    return out


def sha_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def code_hashes():
    names = sorted(n for n in os.listdir(HERE) if n.endswith((".py", ".json")) and n != "readiness_report.json")
    return {n: sha_file(os.path.join(HERE, n)) for n in names}


# ---------------------------------------------------------------- pipeline

def execute(data, ledger_path, seal, capital, cost_model, policies, end):
    """Broker run on a sealed ledger, marked at the last session on or before end (or the last fill)."""
    b = simbroker.Broker(data, ledger_path, seal, capital_inr=capital, cost_model=cost_model, **policies).run()
    sessions = data.sessions_between("0000-01-01", end)
    final = sessions[-1] if sessions else None
    if b.last_mark is not None and (final is None or b.last_mark > final):
        final = b.last_mark
    if final:
        b.mark_at(final)
    b.portfolio.reconcile()
    return b


def pipeline(data, universe, strategy_path, class_name, dates, end, out_dir, capital, cost_model, policies,
             sensitivity, label, isolation="subprocess", sealed_at=None, benchmarks=None, extra=None):
    """Signals, sealed ledger, execution, sensitivity runs and metrics. Returns (results, run facts)."""
    if not dates:
        raise Refused("no decision date in the window")
    os.makedirs(out_dir, exist_ok=False)
    ledger_path = os.path.join(out_dir, "ledger.jsonl")
    seal = signals.generate(data, universe, strategy_path, class_name, dates, ledger_path, isolation, sealed_at)
    anchor = os.path.join(out_dir, "seal_anchor.txt")
    with open(anchor, "x", encoding="ascii") as fh:
        fh.write(seal + "\n")
    with open(anchor, encoding="ascii") as fh:
        anchored = fh.read().strip()
    led = signals.open_sealed(ledger_path, anchored)
    decided = [s["decision_date"] for s in led.signals()]

    main = execute(data, ledger_path, anchored, capital, cost_model, policies, end)
    results = {"headline": evaluate.evaluate(main.result(), main.portfolio.journal, decided, label, benchmarks)}
    results["headline"]["policies"] = dict(policies)
    for alt in sensitivity:
        pol = dict(policies)
        pol.update({k: v for k, v in alt.items() if k != "name"})
        b = execute(data, ledger_path, anchored, capital, cost_model, pol, end)
        m = evaluate.evaluate(b.result(), b.portfolio.journal, decided, label + " | SENSITIVITY: " + alt["name"],
                              benchmarks)
        m["policies"] = pol
        results["sensitivity: " + alt["name"]] = m
    for name, run in sorted((extra or {}).items()):
        results[name] = run
    orders = main.result()["orders"]
    facts = {"ledger": os.path.basename(ledger_path), "ledger_head": led.seal_hash, "signal_head": led.signal_head,
             "anchor_file": os.path.basename(anchor), "decisions": len(decided), "first_decision": decided[0],
             "last_decision": decided[-1], "journal_digest": main.portfolio.digest()}
    out = {"label": label, "results": results, "orders": orders}
    with open(os.path.join(out_dir, "orders.json"), "x", encoding="utf-8") as fh:
        json.dump(orders, fh, indent=1, sort_keys=True, default=str)
    with open(os.path.join(out_dir, "journal.json"), "x", encoding="utf-8") as fh:
        json.dump(main.portfolio.journal, fh, indent=1, sort_keys=True, default=str)
    return out, facts


def manifest(mode, label, protocol, facts, out, report=None, inventory=None, extra=None):
    commit, dirty = readiness.git_state()
    m = {"mode": mode, "label": label, "venue": protocol["venue"], "window": protocol["windows"]["evaluation"],
         "capital_inr": protocol["capital_inr"], "policies": protocol["execution"],
         "sensitivity": protocol["sensitivity"], "python": "%d.%d.%d" % sys.version_info[:3],
         "git_commit": commit, "git_dirty": dirty, "code_sha256": code_hashes(),
         "protocol_sha256": sha_file(readiness.PROTOCOL),
         "strategy_sha256": protocol["strategy"]["sha256"], "criteria_sha256": protocol["criteria"]["sha256"],
         "cost_schedule_sha256": sha_file(os.path.join(HERE, "cost_schedule.json")),
         "inventory_sha256": sha_file(os.path.join(HERE, "cache_inventory.json")),
         "readiness": None if report is None else {"classification": report.classification,
                                                   "digest": report.digest()},
         "output_digest": evaluate.digest({"results": out["results"], "orders": out["orders"]})}
    m.update(facts)
    if inventory is not None:
        m["inventory_check"] = inventory
    m.update(extra or {})
    return m


def _write(path, obj):
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


# ---------------------------------------------------------------- real mode

def run_real(cache, out_dir, protocol_path=readiness.PROTOCOL, report=None):
    """The pre-registered experiment. Refused unless every mandatory readiness check passes, before and after
    the market data is loaded."""
    report = report or readiness.assess(cache, protocol_path)
    if report.classification != "READY":
        raise Refused("%s: %d mandatory check(s) not passing: %s" % (
            BLOCKED, len(report.blockers()), ", ".join(c.id for c in report.blockers())), report)
    import costs
    import pit
    import tri
    import universe
    protocol = readiness.load_protocol(protocol_path)
    data = pit.from_cache(cache, protocol["venue"])
    report = readiness.assess(cache, protocol_path, provenance=data.provenance, phase="data-loaded")
    if report.classification != "READY":
        raise Refused("%s: the gate failed after loading market data: %s" % (
            BLOCKED, ", ".join(c.id for c in report.blockers())), report)
    return _real(cache, data, protocol, protocol_path, report, out_dir, costs, tri, universe)


def _real(cache, data, protocol, protocol_path, report, out_dir, costs, tri, universe):
    if data.provenance != "exchange-cache":
        raise Refused("%s: market data provenance %r is not the verified cache" % (BLOCKED, data.provenance))
    w = protocol["windows"]["evaluation"]
    here = os.path.dirname(os.path.abspath(protocol_path))
    rules = universe.Rules.from_criteria(os.path.join(here, protocol["criteria"]["file"]))
    uni = universe.Universe(data, universe.Evidence.from_files(cache), rules)
    dates = first_sessions(data, w["start"], w["end"])
    cost_model = costs.ScheduleCosts(costs.Schedule.load())
    policies = {k: protocol["execution"][k] for k in ("liquidity_cap", "on_unconfirmed_closure",
                                                       "on_insufficient_cash")}
    tri_series = tri.load_series(cache)
    bench_dir = os.path.join(out_dir, "benchmark-niftybees")
    os.makedirs(out_dir, exist_ok=False)
    bench, _ = pipeline(data, uni, os.path.join(HERE, "strategy_buyhold.py"), "BuyAndHoldNiftyBees", dates[:1],
                        w["end"], bench_dir, protocol["capital_inr"], cost_model, policies, [],
                        REAL + " | BENCHMARK: cost-matched NIFTYBEES buy-and-hold", protocol["isolation"],
                        benchmarks={"NIFTY 50 TRI": tri_series})
    run_dir = os.path.join(out_dir, "baseline")
    out, facts = pipeline(data, uni, os.path.join(here, protocol["strategy"]["file"]), protocol["strategy"]["class"],
                          dates, w["end"], run_dir, protocol["capital_inr"], cost_model, policies,
                          protocol["sensitivity"], REAL, protocol["isolation"],
                          benchmarks={"NIFTY 50 TRI": tri_series},
                          extra={"benchmark: cost-matched NIFTYBEES buy-and-hold": bench["results"]["headline"]})
    head = out["results"]["headline"]
    nb = bench["results"]["headline"]
    if nb["return"] is not None and head["return"] is not None:
        head["benchmarks"]["cost-matched NIFTYBEES buy-and-hold"] = {
            "return": nb["return"], "max_drawdown": nb["max_drawdown"],
            "relative_return": evaluate._s(evaluate.D(head["return"]) - evaluate.D(nb["return"]))}
    import inventory
    with open(inventory.INVENTORY, encoding="utf-8") as fh:
        inv = inventory.check(cache, json.load(fh))
    m = manifest("real", REAL, protocol, facts, out, report,
                 {k: len(v) for k, v in inv.items()},
                 {"tri_files": [r["sha256"] for r in tri.status(cache)[1]["files"]],
                  "benchmark_strategy_sha256": sha_file(os.path.join(HERE, "strategy_buyhold.py"))})
    _write(os.path.join(out_dir, "results.json"), out["results"])
    _write(os.path.join(out_dir, "manifest.json"), m)
    return out, m


# ---------------------------------------------------------------- mechanics mode (synthetic)

SYN_KEYS = (("900001", "INF000SYN011", "SYNTHETIC-A", "nifty50", 100.0),
            ("900002", "INF000SYN022", "SYNTHETIC-B", "niftynext50", 40.0))


def synthetic_market(start="2090-01-02", end="2091-12-29"):
    """SYNTHETIC market built in code: two made-up ETFs on made-up dates (the 2090s, so no one can mistake
    them for history). Prices follow a fixed formula; no real price is involved."""
    import calendar_in
    import pit
    days, d = [], datetime.date.fromisoformat(start)
    while d.isoformat() <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    rows = {}
    for i, day in enumerate(days):
        rows[day] = {}
        for j, (key, isin, sym, _, base) in enumerate(SYN_KEYS):
            close = round(base * (1 + 0.0004 * i) * (1 + 0.03 * math.sin(i / (9.0 + 4 * j))), 2)
            op = round(close * (1 - 0.002 * math.cos(i / 5.0)), 2)
            rows[day][key] = {"date": day, "exchange": "BSE", "code": key, "symbol": sym, "isin": isin,
                              "close": close, "open": op, "low": min(op, close), "high": max(op, close),
                              "value": 20000000.0}
    cal = calendar_in.Calendar("BSE", {x: "synthetic" for x in days}, [])
    return pit.MarketData(cal, rows, "BSE", provenance="SYNTHETIC")


def synthetic_evidence():
    import universe
    doc = {"title": "SYNTHETIC evidence for mechanics only", "url": "https://example.org/synthetic",
           "retrieved_on": "2000-01-01", "sha256": "0" * 64}
    acts = [{"id": "syn-review-%s" % k, "exchange": "BSE", "key": k, "type": "distributions-reviewed",
             "verification": "verified", "documents": [doc], "covers_from": "2090-01-01", "covers_to": "2099-12-31",
             "distributions": []} for k, _, _, _, _ in SYN_KEYS]
    cats = {isin: [(cat, "2090-01-01", "SYNTHETIC")] for _, isin, _, cat, _ in SYN_KEYS}
    return universe.Evidence(cats, acts, [])


class SyntheticCosts(simbroker.CostModel):
    """SYNTHETIC MECHANICS-ONLY COSTS: Rs 2.00 per fill. Not a real or historical charge."""

    def charges(self, side, units, price_paise, date, venue, key):
        return simbroker.Charges(200, "synthetic", [{"component": "SYNTHETIC flat fee", "status": "synthetic",
                                                     "paise": 200}], known_paise=200)


def run_mechanics(out_dir, protocol_path=readiness.PROTOCOL, isolation="subprocess", data=None):
    """The real pipeline on synthetic data. Refuses market data from the cache: mechanics never touches it."""
    import universe
    protocol = readiness.load_protocol(protocol_path)
    data = data or synthetic_market()
    if data.provenance == "exchange-cache":
        raise Refused("mechanics mode runs only on synthetic data")
    here = os.path.dirname(os.path.abspath(protocol_path))
    rules = universe.Rules.from_criteria(os.path.join(here, protocol["criteria"]["file"]))
    uni = universe.Universe(data, synthetic_evidence(), rules)
    dates = first_sessions(data, "2090-07-01", "2091-12-29")
    policies = {k: protocol["execution"][k] for k in ("liquidity_cap", "on_unconfirmed_closure",
                                                       "on_insufficient_cash")}
    label = MECHANICS + " (SYNTHETIC data, SYNTHETIC costs)"
    out, facts = pipeline(data, uni, os.path.join(here, protocol["strategy"]["file"]), protocol["strategy"]["class"],
                          dates, "2091-12-29", out_dir, protocol["capital_inr"], SyntheticCosts(), policies,
                          protocol["sensitivity"], label, isolation, SYNTHETIC_SEAL_TIME)
    m = manifest("mechanics", label, protocol, facts, out)
    _write(os.path.join(out_dir, "results.json"), out["results"])
    _write(os.path.join(out_dir, "manifest.json"), m)
    return out, m


# ---------------------------------------------------------------- CLI

def main(argv=None):
    import fetch
    ap = argparse.ArgumentParser(description="Readiness gate and reproducible runner for the India experiment.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("readiness")
    pr.add_argument("--no-write", action="store_true", help="print only; do not update the committed report")
    for name in ("run", "mechanics"):
        p = sub.add_parser(name)
        p.add_argument("--out", help="new output folder (default: india/data/runs/<mode>-<UTC time>)")
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    if a.cmd == "readiness":
        rep = readiness.assess(cache)
        if not a.no_write:
            with open(REPORT_MD, "w", encoding="utf-8") as fh:
                fh.write(rep.markdown())
            with open(REPORT_JSON, "w", encoding="utf-8") as fh:
                fh.write(rep.to_json())
        print("%s; %d mandatory check(s) not passing: %s" % (
            rep.classification, len(rep.blockers()), ", ".join(c.id for c in rep.blockers()) or "none"))
        if rep.classification != "READY":
            print(BLOCKED)
        return 0 if rep.classification == "READY" else 3
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = a.out or os.path.join(RUNS, "%s-%s" % (a.cmd, stamp))
    try:
        if a.cmd == "run":
            out, m = run_real(cache, out_dir)
        else:
            out, m = run_mechanics(out_dir)
    except Refused as e:
        print(str(e))
        return 3
    h = out["results"]["headline"]
    print(m["label"])
    print("output in %s; output digest %s" % (out_dir, m["output_digest"]))
    print("start %s paise, end %s paise, return %s (%s)" % (h["start_value_paise"], h["end_value_paise"],
                                                           h["return"], h["return_label"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
