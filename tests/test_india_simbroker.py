"""Simulated broker mechanics. SYNTHETIC DATA ONLY: every price, value, fee and signal is made
up to exercise the rules. Nothing in this file is a trading or performance result."""

import os
import tempfile
import unittest

import _india_fixtures as fx
import calendar_in as cal_mod  # noqa: E402
import pit  # noqa: E402
import portfolio as P  # noqa: E402
import signals as S  # noqa: E402
import simbroker as B  # noqa: E402
from test_india_signals import evidence as two_evidence
from test_india_universe import A, B as ISIN_B, rules
import universe as U  # noqa: E402

IDENT = {"name": "synthetic-plan-MECHANICS-TEST-ONLY", "version": "0", "code_sha256": "0" * 64}
SEAL_TIME = "2026-10-28T00:00:00Z"
DAYS = list(fx.weekdays("2024-01-01", "2024-02-16"))
BOTH = ("bse-bhavcopy-isin", "bse-bhavcopy-udiff")


def row(d, key, isin, close, open_=None, value=10_000_000.0, low=None, high=None):
    o = close if open_ is None else open_
    return {"date": d, "exchange": "BSE", "code": key, "symbol": "SYN" + key, "isin": isin, "close": close,
            "open": o, "low": min(o, close) if low is None else low, "high": max(o, close) if high is None else high,
            "value": value}


def market(rows_for, gaps=(), nofile=(), holidays=(), unobserved=()):
    """Every weekday has a valid file except documented gaps, no-file days, official holidays and unobserved days."""
    skip = set(gaps) | set(nofile) | set(holidays) | set(unobserved)
    files = [d for d in DAYS if d not in skip]
    sha = {g: "%064x" % (i + 1) for i, g in enumerate(gaps)}
    cal = cal_mod.Calendar("BSE", {d: "sha" for d in files}, [(d, s, "not-data") for d in nofile for s in BOTH],
                           official=[(h, "holiday", "synthetic holiday", "sha") for h in holidays],
                           invalid=[(g, sha[g]) for g in gaps], gaps=sha)
    return pit.MarketData(cal, {d: rows_for(d) for d in files}, "BSE")


def flat(d, k1_open=None, k1_value=10_000_000.0, close1=259.49):
    """Two synthetic ETFs; 590901 closes at 259.49 (open may differ), 590902 at 100.00."""
    return {"590901": row(d, "590901", A, close1, k1_open, k1_value), "590902": row(d, "590902", ISIN_B, 100.0)}


def ledger(tmp, data, plan, seal=True):
    """plan: [(decision_date, {key: weight})] -> (path, seal hash). Weights validated as for a real strategy."""
    path = os.path.join(tmp, "plan-%d.jsonl" % len(os.listdir(tmp)))
    led = S.Ledger.create(path, "BSE", IDENT, "in-process")
    u = U.Universe(data, two_evidence(), rules())
    for d, w in plan:
        inp = S.StrategyInput.build(data.view(d), u.decide(d), data.digest_through(d), 3)
        led.append_signal(inp, S.validate_targets(w, inp), {"name": IDENT["name"], "version": "0"})
    return path, (led.seal(SEAL_TIME) if seal else None)


class SyntheticFlatCosts(B.CostModel):
    """SYNTHETIC MECHANICS-ONLY COSTS: Rs 5.00 per fill. Not a real or historical charge."""

    def charges(self, side, units, price_paise, date, venue, key):
        return B.Charges(500, "synthetic", [{"component": "synthetic flat fee", "paise": 500}])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def run_broker(self, data, plan, **kw):
        path, seal = ledger(self.tmp.name, data, plan)
        return B.Broker(data, path, seal, **kw).run()

    def order(self, broker, key="590901", n=0):
        return [o for o in broker.orders if o["key"] == key][n]


class Anchoring(Base):
    def test_the_external_seal_is_required(self):
        data = market(flat)
        path, seal = ledger(self.tmp.name, data, [("2024-01-19", {"590901": "1"})])
        for bad in (None, "", "abc", seal.upper()):
            with self.assertRaisesRegex(B.BrokerError, "seal hash recorded outside"):
                B.Broker(data, path, bad)
        with self.assertRaisesRegex(S.LedgerError, "not the one recorded"):
            B.Broker(data, path, "f" * 64)

    def test_an_unsealed_ledger_is_refused_and_the_ledger_is_never_written(self):
        data = market(flat)
        path, _ = ledger(self.tmp.name, data, [("2024-01-19", {"590901": "1"})], seal=False)
        with self.assertRaisesRegex(S.LedgerError, "not sealed"):
            B.Broker(data, path, "a" * 64)
        sealed, seal = ledger(self.tmp.name, data, [("2024-01-19", {"590901": "1"})])
        def read():
            with open(sealed, "rb") as fh:
                return fh.read()
        before = read()
        B.Broker(data, sealed, seal).run()
        self.assertEqual(read(), before)


class Timing(Base):
    def test_a_friday_decision_fills_at_mondays_open_not_fridays_close(self):
        data = market(lambda d: flat(d, k1_open=260.00 if d == "2024-01-22" else None))
        o = self.order(self.run_broker(data, [("2024-01-19", {"590901": "1"})]))
        self.assertEqual((o["signal_date"], o["submitted"], o["execution_date"]), ("2024-01-19", "2024-01-19", "2024-01-22"))
        self.assertEqual((o["requested"], o["filled"], o["price_paise"]), (11, 11, 26000))   # floor(3000 / 259.49) = 11

    def test_an_official_holiday_is_skipped(self):
        data = market(flat, holidays=("2024-01-22",))
        self.assertEqual(self.order(self.run_broker(data, [("2024-01-19", {"590901": "1"})]))["execution_date"],
                         "2024-01-23")

    def test_no_fill_on_a_gap_an_unobserved_day_or_by_default_a_no_file_day(self):
        for kw, status in ((dict(gaps=("2024-01-22",)), "gap"), (dict(unobserved=("2024-01-22",)), "unobserved"),
                           (dict(nofile=("2024-01-22",)), "no-file")):
            o = self.order(self.run_broker(market(flat, **kw), [("2024-01-19", {"590901": "1"})]))
            self.assertEqual((o["status"], o["execution_date"]), ("unfilled", "2024-01-22"), status)
            self.assertIn(status, o["reason"])

    def test_the_roll_policy_records_its_assumption(self):
        o = self.order(self.run_broker(market(flat, nofile=("2024-01-22",)), [("2024-01-19", {"590901": "1"})],
                                       on_unconfirmed_closure="roll"))
        self.assertEqual((o["status"], o["execution_date"]), ("filled", "2024-01-23"))
        self.assertIn("treated as a closure (policy roll)", o["assumptions"][0])

    def test_an_unfilled_order_is_never_retried(self):
        b = self.run_broker(market(flat, gaps=("2024-01-22",)), [("2024-01-19", {"590901": "1"})])
        self.assertEqual([o["status"] for o in b.orders], ["unfilled"])
        self.assertEqual(b.portfolio.holdings, {})

    def test_no_later_session_means_unfilled(self):
        o = self.order(self.run_broker(market(flat), [("2024-02-16", {"590901": "1"})]))
        self.assertEqual((o["status"], o["reason"]), ("unfilled", "no later session in the data"))


class Checks(Base):
    def test_the_ex_post_sensitivity_mode_keeps_the_execution_day_rule(self):
        # 11 units at 259.49 = 2,854.39; 5% of 57,087.80 is exactly that
        exact = market(lambda d: flat(d, k1_value=57087.80 if d == "2024-01-22" else 10_000_000.0))
        o = self.order(self.run_broker(exact, [("2024-01-19", {"590901": "1"})], liquidity_cap="ex-post"))
        self.assertEqual(o["status"], "filled")
        thin = market(lambda d: flat(d, k1_value=57087.79 if d == "2024-01-22" else 10_000_000.0))
        o = self.order(self.run_broker(thin, [("2024-01-19", {"590901": "1"})], liquidity_cap="ex-post"))
        self.assertEqual(o["status"], "unfilled")
        self.assertIn("above 5", o["reason"])

    def test_the_default_ex_ante_cap_uses_the_decision_days_traded_value(self):
        # reference 11 x 259.49 = 2,854.39 against 5% of the decision day's value
        ok = market(lambda d: flat(d, k1_value=57087.80 if d == "2024-01-19" else 10_000_000.0))
        self.assertEqual(self.order(self.run_broker(ok, [("2024-01-19", {"590901": "1"})]))["status"], "filled")
        thin = market(lambda d: flat(d, k1_value=57087.79 if d == "2024-01-19" else 10_000_000.0))
        o = self.order(self.run_broker(thin, [("2024-01-19", {"590901": "1"})]))
        self.assertEqual((o["status"], o["execution_date"]), ("not-placed", None))
        self.assertIn("decision day's traded value", o["reason"])

    def test_the_execution_days_traded_value_cannot_decide_an_ex_ante_fill(self):
        thin_next_day = market(lambda d: flat(d, k1_value=1.0 if d == "2024-01-22" else 10_000_000.0))
        o = self.order(self.run_broker(thin_next_day, [("2024-01-19", {"590901": "1"})]))
        self.assertEqual((o["status"], o["within_cap_of_execution_day"]), ("filled", False))   # audit only
        deep = self.order(self.run_broker(market(flat), [("2024-01-19", {"590901": "1"})]))
        self.assertEqual((deep["filled"], deep["within_cap_of_execution_day"]), (o["filled"], True))

    def test_the_liquidity_mode_is_validated(self):
        data = market(flat)
        path, seal = ledger(self.tmp.name, data, [("2024-01-19", {"590901": "1"})])
        with self.assertRaises(B.BrokerError):
            B.Broker(data, path, seal, liquidity_cap="same-day")

    def test_a_full_capital_order_needs_sixty_thousand_of_traded_value(self):
        def rows(d):
            return {"590901": row(d, "590901", A, 100.0, value=60000.0 if d == "2024-01-19" else 1e7),
                    "590902": row(d, "590902", ISIN_B, 100.0)}
        o = self.order(self.run_broker(market(rows), [("2024-01-19", {"590901": "1"})]))
        self.assertEqual((o["filled"], o["price_paise"]), (30, 10000))      # 30 x Rs 100 = Rs 3,000 = 5% of Rs 60,000

    def test_no_trade_or_a_changed_isin_on_the_execution_day_means_unfilled(self):
        gone = market(lambda d: {k: v for k, v in flat(d).items() if not (d == "2024-01-22" and k == "590901")})
        self.assertIn("no verified trade", self.order(self.run_broker(gone, [("2024-01-19", {"590901": "1"})]))["reason"])
        changed = market(lambda d: dict(flat(d), **({"590901": row(d, "590901", "INF000SYN099", 259.49)}
                                                    if d == "2024-01-22" else {})))
        self.assertIn("not %s" % A, self.order(self.run_broker(changed, [("2024-01-19", {"590901": "1"})]))["reason"])

    def test_an_open_outside_the_days_range_is_refused(self):
        bad = market(lambda d: dict(flat(d), **({"590901": row(d, "590901", A, 259.49, 300.0, high=270.0)}
                                                if d == "2024-01-22" else {})))
        self.assertIn("outside the day's range", self.order(self.run_broker(bad, [("2024-01-19", {"590901": "1"})]))["reason"])

    def test_insufficient_cash_is_rejected_or_reduced_by_explicit_policy(self):
        gap_up = market(lambda d: flat(d, k1_open=280.00 if d == "2024-01-22" else None))   # 11 x 280 = 3,080
        o = self.order(self.run_broker(gap_up, [("2024-01-19", {"590901": "1"})]))
        self.assertEqual(o["status"], "unfilled")
        self.assertIn("settled cash is 3000.00", o["reason"])
        o = self.order(self.run_broker(gap_up, [("2024-01-19", {"590901": "1"})], on_insufficient_cash="reduce"))
        self.assertEqual((o["status"], o["filled"]), ("filled", 10))
        self.assertIn("reduced from 11 to 10", o["assumptions"][0])


class SellsAndSettlement(Base):
    def test_holdings_not_in_the_targets_are_sold_and_proceeds_settle_next_session(self):
        b = self.run_broker(market(flat), [("2024-01-19", {"590901": "1"}), ("2024-01-26", {})])
        sell = self.order(b, n=1)
        self.assertEqual((sell["side"], sell["requested"], sell["filled"], sell["execution_date"]),
                         ("sell", 11, 11, "2024-01-29"))
        self.assertEqual((b.portfolio.holdings, sum(x for _, x in b.portfolio.unsettled)), ({}, 11 * 25949))
        self.assertEqual(b.portfolio.cash, 300000 - 11 * 25949)

    def test_proceeds_cannot_fund_a_buy_on_the_same_session(self):
        b = self.run_broker(market(flat), [("2024-01-19", {"590901": "1"}), ("2024-01-26", {"590902": "1"})])
        buy = self.order(b, "590902")
        self.assertEqual(buy["status"], "unfilled")                     # 29 units of Rs 100 need Rs 2,900; cash is 145.61
        self.assertIn("settled cash is 145.61", buy["reason"])

    def test_a_sell_is_matched_to_the_isin_it_was_bought_under(self):
        def rows(d):
            r = flat(d)
            if d == "2024-01-26":
                del r["590901"]           # held ETF does not trade on the decision day
            return r
        plan = [("2024-01-19", {"590901": "1"}), ("2024-01-26", {})]
        sell = self.order(self.run_broker(market(rows), plan, liquidity_cap="ex-post"), n=1)
        self.assertEqual((sell["isin"], sell["status"]), (A, "filled"))
        # under the default ex-ante cap there is no decision-day liquidity to place the sell against
        sell = self.order(self.run_broker(market(rows), plan), n=1)
        self.assertEqual(sell["status"], "not-placed")
        self.assertIn("liquidity unknown", sell["reason"])


class Costs(Base):
    def test_unresolved_costs_are_never_zero_and_are_flagged(self):
        b = self.run_broker(market(flat), [("2024-01-19", {"590901": "1"})])
        o = self.order(b)
        self.assertEqual((o["fee_paise"], o["cost_status"]), (None, "unresolved"))
        r = b.result()
        self.assertEqual((r["cost_statuses"], r["fills_with_unresolved_fees"]), (["unresolved"], 1))
        marks = [e for e in b.portfolio.journal if e["type"] == "mark"]
        self.assertIn("1 fill(s) with unresolved fees: cash excludes them", marks[-1]["flags"])

    def test_synthetic_fees_reduce_cash_exactly(self):
        b = self.run_broker(market(flat), [("2024-01-19", {"590901": "1"}), ("2024-01-26", {})],
                            cost_model=SyntheticFlatCosts())
        p = b.portfolio
        self.assertEqual(p.cash, 300000 - (11 * 25949 + 500))
        self.assertEqual(sum(x for _, x in p.unsettled), 11 * 25949 - 500)
        self.assertEqual((p.fees_known, b.result()["cost_statuses"]), (1000, ["synthetic"]))

    def test_charges_are_validated(self):
        for bad in (dict(paise=None, status="synthetic"), dict(paise=5, status="unresolved"),
                    dict(paise=-1, status="synthetic"), dict(paise=1.5, status="synthetic"),
                    dict(paise=1, status="guessed")):
            with self.assertRaises(B.BrokerError):
                B.Charges(components=[], **bad)


class Marks(Base):
    def test_a_stale_mark_stays_flagged_and_gaps_are_recorded(self):
        def rows(d):
            r = flat(d)
            if d == "2024-01-26":
                r["590901"] = dict(r["590901"], close=None)       # traded nothing usable at the close
            return r
        # the gap puts 590901 over the unknown-share limit, so the second signal can only sell it
        b = self.run_broker(market(rows, gaps=("2024-01-24",)), [("2024-01-19", {"590901": "1"}),
                                                                ("2024-01-26", {})])
        marks = {e["date"]: e for e in b.portfolio.journal if e["type"] == "mark"}
        self.assertTrue(any("stale" in f for f in marks["2024-01-26"]["flags"]))
        self.assertEqual(marks["2024-01-26"]["gaps_since_previous_mark"], ["2024-01-24"])


class Whole(Base):
    def plan(self):
        return [("2024-01-19", {"590901": "0.5", "590902": "0.5"}), ("2024-01-26", {"590902": "1"}),
                ("2024-02-02", {"590901": "1"}), ("2024-02-09", {})]

    def test_a_multi_session_synthetic_run_from_rs_3000_reconciles_exactly(self):
        def rows(d):  # SYNTHETIC prices: 590901 drifts up by Rs 1 a day from 250, 590902 stays at 100
            i = DAYS.index(d)
            return {"590901": row(d, "590901", A, 250.0 + i), "590902": row(d, "590902", ISIN_B, 100.0)}
        b = self.run_broker(market(rows), self.plan())
        p = b.portfolio
        self.assertTrue(p.reconcile())
        self.assertEqual(p.journal[0]["capital_paise"], 300000)
        fills = [(o["side"], o["key"], o["filled"], o["price_paise"], o["execution_date"])
                 for o in b.orders if o["status"] == "filled"]
        # 01-19: value 3,000; 590901 close 264 -> 5 units; 590902 -> 15 units; filled 01-22 at 265 and 100
        self.assertEqual(fills[:2], [("buy", "590901", 5, 26500, "2024-01-22"), ("buy", "590902", 15, 10000, "2024-01-22")])
        cash = 300000 - 5 * 26500 - 15 * 10000
        self.assertEqual(p.journal[-1]["type"] in ("mark", "filled", "unfilled"), True)
        flow = cash
        for side, key, n, price, day in fills[2:]:
            flow += (n * price) if side == "sell" else -(n * price)
        self.assertEqual(p.cash + sum(x for _, x in p.unsettled), flow)
        self.assertEqual(b.result()["label"], B.LABEL)

    def test_the_journal_is_reproducible(self):
        a = self.run_broker(market(flat), self.plan()).result()
        b = self.run_broker(market(flat), self.plan()).result()
        self.assertEqual(a["journal_digest"], b["journal_digest"])

    def test_data_after_the_last_execution_cannot_change_the_journal(self):
        plan = [("2024-01-19", {"590901": "1"}), ("2024-01-26", {})]
        a = self.run_broker(market(flat), plan).result()
        later = market(lambda d: flat(d) if d <= "2024-01-29" else flat(d, k1_open=1.0, close1=1.0))
        b = self.run_broker(later, plan).result()
        self.assertEqual(a["journal_digest"], b["journal_digest"])


if __name__ == "__main__":
    unittest.main()
