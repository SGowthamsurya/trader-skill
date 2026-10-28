"""Cost model mechanics. The rate schedules here are SYNTHETIC MECHANICS-ONLY FIXTURES: every
rate is made up and none is a real Indian charge or tax."""

import copy
import json
import os
import tempfile
import unittest

import _india_fixtures as fx
import costs as C  # noqa: E402
import simbroker as B  # noqa: E402

LABEL = "SYNTHETIC MECHANICS-ONLY RATES (not real charges)"


def per(n, d, frm="2018-01-01", to=None, rounding="half_up", verification="verified"):
    return {"effective_from": frm, "effective_to": to, "rate": {"numerator": n, "denominator": d},
            "rounding": rounding, "verification": verification}


def fixed(paise, frm="2018-01-01", to=None, verification="verified"):
    return {"effective_from": frm, "effective_to": to, "rate": {"paise": paise}, "rounding": "half_up",
            "verification": verification}


def comp(cid, basis, periods, sides=("buy", "sell"), venues=("BSE", "NSE"), **kw):
    c = {"id": cid, "category": "synthetic", "basis": basis, "periods": periods,
         "applies": {"venues": list(venues), "sides": list(sides), "instruments": ["etf"]}}
    c.update(kw)
    return c


def schedule(*components, synthetic=True):
    return C.Schedule({"synthetic": synthetic, "label": LABEL if synthetic else "real", "components": list(components),
                       "investor_taxes": [{"id": "capital-gains", "status": "unresolved", "depends_on": ["holding period"]}]})


SYN = schedule(
    comp("turnover-a", "turnover", [per(1, 1000, to="2024-09-30"), per(2, 1000, frm="2024-10-01")]),   # 0.1% then 0.2%
    comp("stamp", "turnover", [per(15, 100000, rounding="ceil")], sides=("buy",)),                        # 0.015%, buy only
    comp("bse-only", "turnover", [per(3, 100000)], venues=("BSE",)),
    comp("flat", "per_fill", [fixed(2000)]),                                                              # Rs 20 a fill
    comp("debit", "per_debit", [fixed(1350)], sides=("sell",)),                                            # Rs 13.50 a sale
    comp("tax-on", "on_components", [per(18, 100)], of=["flat", "debit"]),                                 # 18% of those
)


def model(s=SYN):
    return C.ScheduleCosts(s)


def amounts(ch):
    return {c["component"]: c.get("paise") for c in ch.components}


class Calculation(unittest.TestCase):
    def test_component_by_component_on_a_bse_buy(self):
        ch = model().charges("buy", 10, 25949, "2024-01-22", "BSE", "590901")     # value Rs 2,594.90
        # debit applies to sells only, so a buy does not list it at all
        self.assertEqual(amounts(ch), {"turnover-a": 259, "stamp": 39, "bse-only": 8, "flat": 2000, "tax-on": 360})
        self.assertEqual((ch.paise, ch.status, ch.known_paise), (2666, "synthetic", 2666))

    def test_effective_date_boundaries(self):
        before = amounts(model().charges("buy", 10, 25949, "2024-09-30", "BSE", "K"))["turnover-a"]
        after = amounts(model().charges("buy", 10, 25949, "2024-10-01", "BSE", "K"))["turnover-a"]
        self.assertEqual((before, after), (259, 519))

    def test_venue_and_side_applicability(self):
        nse_sell = amounts(model().charges("sell", 10, 25949, "2024-01-22", "NSE", "K"))
        self.assertNotIn("stamp", nse_sell)
        self.assertNotIn("bse-only", nse_sell)
        self.assertEqual((nse_sell["debit"], nse_sell["tax-on"]), (1350, 603))

    def test_rounding_rules(self):
        self.assertEqual([C._round(__import__("decimal").Decimal(x), r) for x, r in (
            ("2.5", "half_up"), ("2.4", "half_up"), ("2.01", "ceil"), ("2.99", "floor"),
            ("149", "nearest_rupee_half_up"), ("150", "nearest_rupee_half_up"))], [3, 2, 3, 2, 100, 200])

    def test_reproducible(self):
        a = model().charges("sell", 7, 12345, "2025-03-03", "BSE", "K")
        b = model().charges("sell", 7, 12345, "2025-03-03", "BSE", "K")
        self.assertEqual((a.paise, a.components), (b.paise, b.components))


class Unresolved(unittest.TestCase):
    def test_a_date_no_period_covers_is_unresolved_and_the_known_part_is_kept(self):
        s = schedule(comp("old", "turnover", [per(1, 1000, to="2019-12-31")]), comp("flat", "per_fill", [fixed(2000)]))
        ch = C.ScheduleCosts(s).charges("buy", 10, 10000, "2020-06-01", "BSE", "K")
        self.assertEqual((ch.paise, ch.status, ch.known_paise), (None, "unresolved", 2000))
        old = [c for c in ch.components if c["component"] == "old"][0]
        self.assertEqual(old["status"], "unresolved")
        self.assertNotIn("paise", old)

    def test_an_unverified_or_ambiguous_period_is_never_used(self):
        for v in ("unverified", "ambiguous"):
            s = schedule(comp("x", "turnover", [per(1, 100, verification=v)]), synthetic=False)
            ch = C.ScheduleCosts(s).charges("buy", 1, 10000, "2024-01-01", "BSE", "K")
            self.assertEqual((ch.paise, ch.known_paise, ch.status), (None, 0, "unresolved"), v)

    def test_a_charge_levied_on_an_unresolved_component_is_unresolved(self):
        s = schedule(comp("base", "per_fill", []), comp("tax-on", "on_components", [per(18, 100)], of=["base"]))
        tax = [c for c in C.ScheduleCosts(s).charges("buy", 1, 100, "2024-01-01", "BSE", "K").components
               if c["component"] == "tax-on"][0]
        self.assertEqual(tax["status"], "unresolved")

    def test_no_applicable_component_is_unresolved_not_free(self):
        ch = model(schedule(comp("bse-only", "turnover", [per(1, 100)], venues=("BSE",)))).charges(
            "buy", 1, 100, "2024-01-01", "NSE", "K")
        self.assertEqual((ch.paise, ch.status), (None, "unresolved"))


class Validation(unittest.TestCase):
    def bad(self, doc, why):
        with self.assertRaisesRegex(C.CostError, why):
            C.Schedule(doc)

    def test_overlapping_periods_are_refused(self):
        self.bad({"synthetic": True, "label": LABEL, "components": [
            comp("x", "turnover", [per(1, 100, to="2024-06-30"), per(2, 100, frm="2024-06-30")])]}, "overlapping")

    def test_malformed_schedules_are_refused(self):
        base = {"synthetic": True, "label": LABEL}
        for comps, why in (([comp("x", "turnover", [dict(per(1, 100), rate={"numerator": 0.1, "denominator": 1})])], "integers"),
                           ([comp("x", "per_fill", [dict(fixed(1), rate={"paise": -1})])], "fixed rate"),
                           ([comp("x", "weekly", [])], "basis"),
                           ([comp("x", "turnover", []), comp("x", "turnover", [])], "unique"),
                           ([comp("x", "on_components", [per(1, 1)], of=["nope"])], "unknown component"),
                           ([comp("x", "turnover", [dict(per(1, 1), rounding="bankers")])], "rounding"),
                           ([comp("x", "turnover", [], sides=("short",))], "sides")):
            self.bad(dict(base, components=comps), why)

    def test_a_synthetic_schedule_must_say_so(self):
        self.bad({"synthetic": True, "label": "test rates", "components": []}, "SYNTHETIC")

    def test_a_real_verified_period_must_cite_its_source(self):
        self.bad({"synthetic": False, "components": [comp("x", "turnover", [per(1, 100)])]}, "cites source")


class RealSchedule(unittest.TestCase):
    def test_it_validates_and_records_no_unverified_rate(self):
        s = C.Schedule.load()
        self.assertFalse(s.synthetic)
        for c in s.components:
            for p in c.get("periods", []):
                self.assertEqual(p["verification"], "verified", c["id"])
            if not c.get("periods"):
                self.assertTrue(c.get("unresolved_reason"), c["id"])

    def test_every_applicable_component_is_unresolved_over_the_window(self):
        gaps = C.Schedule.load().coverage("2022-01-01", "2026-10-27", "BSE", "etf")
        self.assertEqual(len(gaps), 12)
        ch = C.ScheduleCosts(C.Schedule.load()).charges("sell", 3, 25949, "2024-06-03", "BSE", "590103")
        self.assertEqual((ch.paise, ch.known_paise, ch.status), (None, 0, "unresolved"))

    def test_investor_taxes_are_reported_unresolved_with_dependencies(self):
        taxes = {t["id"]: t for t in C.investor_taxes(C.Schedule.load())}
        self.assertEqual(set(taxes), {"capital-gains", "tax-on-distributions"})
        for t in taxes.values():
            self.assertEqual(t["status"], "unresolved")
            self.assertTrue(t["depends_on"])

    def test_the_cdsl_fact_is_partial_evidence_not_a_rate(self):
        dp = [c for c in C.Schedule.load().components if c["id"] == "depository-debit-charge"][0]
        self.assertEqual((dp["periods"], dp["partial_evidence"][0]["verification"]), ([], "ambiguous"))


class WithTheBroker(unittest.TestCase):
    def test_synthetic_schedule_fees_flow_into_the_journal_exactly(self):
        from test_india_simbroker import flat, ledger, market
        with tempfile.TemporaryDirectory() as d:
            data = market(flat)
            path, seal = ledger(d, data, [("2024-01-19", {"590901": "1"}), ("2024-01-26", {})])
            b = B.Broker(data, path, seal, cost_model=model()).run()
        buy, sell = b.orders
        self.assertEqual((buy["cost_status"], sell["cost_status"]), ("synthetic", "synthetic"))
        self.assertTrue(b.portfolio.reconcile())
        self.assertEqual(b.portfolio.fees_known, buy["fee_paise"] + sell["fee_paise"])
        self.assertEqual(b.portfolio.cash, 300000 - (11 * 25949 + buy["fee_paise"]))

    def test_the_real_schedule_leaves_every_fill_unresolved_never_free(self):
        from test_india_simbroker import flat, ledger, market
        with tempfile.TemporaryDirectory() as d:
            data = market(flat)
            path, seal = ledger(d, data, [("2024-01-19", {"590901": "1"})])
            b = B.Broker(data, path, seal, cost_model=C.ScheduleCosts(C.Schedule.load())).run()
        o = b.orders[0]
        self.assertEqual((o["fee_paise"], o["fee_known_paise"], o["cost_status"]), (None, 0, "unresolved"))
        self.assertEqual(b.result()["fills_with_unresolved_fees"], 1)

    def test_a_partly_known_fee_deducts_the_known_part_and_flags_the_rest(self):
        from test_india_simbroker import flat, ledger, market
        s = schedule(comp("flat", "per_fill", [fixed(2000)]), comp("missing", "turnover", []))
        with tempfile.TemporaryDirectory() as d:
            data = market(flat)
            path, seal = ledger(d, data, [("2024-01-19", {"590901": "1"})])
            b = B.Broker(data, path, seal, cost_model=C.ScheduleCosts(s)).run()
        p = b.portfolio
        self.assertEqual((p.cash, p.fees_known, p.fees_unknown), (300000 - 11 * 25949 - 2000, 2000, 1))
        self.assertTrue(p.reconcile())


if __name__ == "__main__":
    unittest.main()
