"""Point-in-time universe mechanics. SYNTHETIC DATA ONLY: every instrument, price and
document here is made up to test the rules; nothing in this file is a market result."""

import json
import os
import unittest

import _india_fixtures as fx
import calendar_in as cal_mod  # noqa: E402
import pit  # noqa: E402
import universe as U  # noqa: E402

A, B = "INF000SYN011", "INF000SYN022"
BOTH = ("bse-bhavcopy-isin", "bse-bhavcopy-udiff")
DOC = {"title": "synthetic", "url": "https://example.org/x.pdf", "retrieved_on": "2026-10-28", "sha256": "0" * 64}
DAYS = list(fx.weekdays("2024-01-01", "2024-02-09"))          # 30 weekdays


def rules(**kw):
    r = dict(venue="BSE", threshold=300000, min_presence=0.95, break_ratio=1.5, capital=3000, trailing=10,
             min_history=5, max_unknown=0.05)
    r.update(kw)
    return U.Rules(**r)


def market(rows_for, gaps=(), nofile=()):
    """rows_for(day) -> {key: row}; every weekday has a valid file unless it is a gap or a no-file day."""
    files = [d for d in DAYS if d not in gaps and d not in nofile]
    sha = {g: ("%064x" % i) for i, g in enumerate(gaps, 1)}
    cal = cal_mod.Calendar("BSE", {d: "sha" for d in files}, [(d, s, "not-data") for d in nofile for s in BOTH],
                           invalid=[(g, sha[g]) for g in gaps], gaps=sha)
    return pit.MarketData(cal, {d: rows_for(d) for d in files}, "BSE")


def etf_row(d, key="590901", isin=A, close=100.0, value=1e6):
    return {"date": d, "exchange": "BSE", "code": key, "symbol": "SYNBEES", "isin": isin, "close": close,
            "value": value}


def evidence(key="590901", isin=A, known="2024-01-01", review=True, actions=(), confirmations=()):
    acts = list(actions)
    if review:
        acts.append({"id": "syn-review", "exchange": "BSE", "key": key, "type": "distributions-reviewed",
                     "verification": "verified", "documents": [DOC], "covers_from": "2023-01-01",
                     "covers_to": "2099-12-31", "distributions": []})
    return U.Evidence({isin: [("nifty50", known, "synthetic category fact")]}, acts, confirmations)


def decide(data, ev, t, **kw):
    return U.Universe(data, ev, rules(**kw)).decide(t)


def one(snap, key="590901"):
    return [x for x in snap.decisions if x.key == key][0]


class Basics(unittest.TestCase):
    def test_a_clean_synthetic_etf_is_eligible(self):
        d = one(decide(market(lambda d: {"590901": etf_row(d)}), evidence(), "2024-01-19"))
        self.assertEqual(d.status, "eligible", d.tests)

    def test_too_little_history_fails(self):
        d = one(decide(market(lambda d: {"590901": etf_row(d)}), evidence(), "2024-01-04"))
        self.assertEqual(d.tests["history"][0], "fail")

    def test_shipped_criteria_load_with_the_approved_values(self):
        r = U.Rules.from_criteria(os.path.join(fx.INDIA, "etf_criteria.json"))
        self.assertEqual((r.threshold, r.capital, r.trailing, r.min_history, r.max_unknown),
                         (300000, 3000, 250, 120, 0.05))

    def test_only_fund_isins_are_candidates(self):
        snap = decide(market(lambda d: {"590901": etf_row(d), "500001": etf_row(d, "500001", "INE000SYN099")}),
                      evidence(), "2024-01-19")
        self.assertEqual([x.key for x in snap.decisions], ["590901"])


class NoLookAhead(unittest.TestCase):
    def test_future_liquidity_cannot_change_an_earlier_decision(self):
        t = "2024-01-19"
        thin_later = market(lambda d: {"590901": etf_row(d, value=1e6 if d <= t else 1.0)})
        thick_later = market(lambda d: {"590901": etf_row(d, value=1e6 if d <= t else 1e12)})
        a, b = decide(thin_later, evidence(), t), decide(thick_later, evidence(), t)
        self.assertEqual(a.digest(), b.digest())
        self.assertEqual(one(a).status, "eligible")

    def test_liquidity_uses_only_the_trailing_window_up_to_the_decision(self):
        data = market(lambda d: {"590901": etf_row(d, value=1e6 if d < "2024-01-22" else 1000.0)})
        self.assertEqual(one(decide(data, evidence(), "2024-01-19")).tests["liquidity"][0], "pass")
        self.assertEqual(one(decide(data, evidence(), "2024-02-09")).tests["liquidity"][0], "fail")

    def test_a_future_announcement_cannot_explain_an_earlier_break(self):
        data = market(lambda d: {"590901": etf_row(d, close=1000.0 if d < "2024-01-16" else 100.5)})
        split = {"id": "syn-split", "exchange": "BSE", "key": "590901", "type": "unit-split", "units_multiplier": 10,
                 "record_date": "2024-01-17", "ex_date": None, "verification": "verified", "documents": [DOC]}
        late = dict(split, announced_on="2024-01-25")
        early = dict(split, announced_on="2024-01-05")
        self.assertEqual(one(decide(data, evidence(actions=[late]), "2024-01-19")).tests["corporate"][0], "unresolved")
        self.assertEqual(one(decide(data, evidence(actions=[late]), "2024-01-26")).tests["corporate"][0], "pass")
        self.assertEqual(one(decide(data, evidence(actions=[early]), "2024-01-19")).tests["corporate"][0], "pass")
        unannounced = dict(split)
        self.assertEqual(one(decide(data, evidence(actions=[unannounced]), "2024-02-09")).tests["corporate"][0],
                         "unresolved")

    def test_a_document_ratio_that_disagrees_with_prices_fails(self):
        data = market(lambda d: {"590901": etf_row(d, close=1000.0 if d < "2024-01-16" else 100.5)})
        bad = {"id": "syn-split", "exchange": "BSE", "key": "590901", "type": "unit-split", "units_multiplier": 2,
               "record_date": None, "ex_date": "2024-01-16", "verification": "verified", "documents": [DOC],
               "announced_on": "2024-01-05"}
        self.assertEqual(one(decide(data, evidence(actions=[bad]), "2024-01-19")).tests["corporate"][0], "fail")

    def test_a_fact_placing_it_outside_the_categories_fails_once_knowable(self):
        data = market(lambda d: {"590901": etf_row(d)})
        ev = U.Evidence({A: [("other", "2024-01-10", "synthetic: a gold ETF")]}, [], [])
        self.assertEqual(one(decide(data, ev, "2024-01-09")).tests["category"][0], "unresolved")
        self.assertEqual(one(decide(data, ev, "2024-01-19")).tests["category"][0], "fail")

    def test_category_evidence_known_only_later_is_unresolved(self):
        data = market(lambda d: {"590901": etf_row(d)})
        out = one(decide(data, evidence(known="2026-10-28"), "2024-01-19")).tests["category"]
        self.assertEqual(out[0], "unresolved")
        self.assertIn("known only from 2026-10-28", out[1])
        self.assertIn("hint, not evidence", out[1])


class Disappearing(unittest.TestCase):
    def test_an_instrument_that_later_disappears_is_a_candidate_while_it_traded(self):
        stops = "2024-01-19"
        gone = market(lambda d: {"590901": etf_row(d)} if d <= stops else {"590902": etf_row(d, "590902", B)})
        stays = market(lambda d: {"590901": etf_row(d)})
        a, b = decide(gone, evidence(), stops), decide(stays, evidence(), stops)
        self.assertEqual(one(a).status, "eligible")
        self.assertEqual(a.digest(), b.digest())

    def test_it_becomes_inactive_once_out_of_the_window_and_is_still_reported(self):
        gone = market(lambda d: {"590901": etf_row(d)} if d <= "2024-01-05" else {"590902": etf_row(d, "590902", B)})
        d = one(decide(gone, evidence(), "2024-02-09"))
        self.assertEqual((d.status, d.last_seen), ("inactive", "2024-01-05"))


class MissingData(unittest.TestCase):
    def test_a_verified_no_trade_session_is_an_observed_zero(self):
        data = market(lambda d: {"590901": etf_row(d)} if d not in ("2024-01-10", "2024-01-11") else {})
        d = one(decide(data, evidence(), "2024-01-19", min_presence=0.5))
        self.assertEqual(d.tests["presence"][0], "pass")
        self.assertIn("traded on 8 of 10", d.tests["presence"][1])

    def test_a_gap_is_unknown_and_never_counted_as_zero(self):
        # with one gap in a 10-session window the share of unknown days is 1/11 > 0.05
        data = market(lambda d: {"590901": etf_row(d)}, gaps=("2024-01-10",))
        d = one(decide(data, evidence(), "2024-01-19"))
        self.assertEqual((d.tests["data"][0], d.tests["liquidity"][0], d.tests["presence"][0]),
                         ("unresolved", "unresolved", "unresolved"))
        self.assertEqual(d.status, "insufficient-data")
        # tolerated gap: liquidity is the median of the known sessions, the gap is left out (not a zero)
        d = one(decide(data, evidence(), "2024-01-19", max_unknown=0.2))
        self.assertEqual(d.tests["liquidity"][0], "pass")
        self.assertIn("unknown days left out", d.tests["liquidity"][1])
        self.assertIn("2024-01-10", d.tests["data"][1])

    def test_a_no_file_day_is_not_a_session_and_not_unknown(self):
        data = market(lambda d: {"590901": etf_row(d)}, nofile=("2024-01-10",))
        d = one(decide(data, evidence(), "2024-01-19"))
        self.assertEqual(d.status, "eligible", d.tests)
        self.assertIn("0 unknown-state days", d.tests["data"][1])

    def test_no_decision_on_a_documented_gap(self):
        data = market(lambda d: {"590901": etf_row(d)}, gaps=("2024-01-10",))
        with self.assertRaises(pit.PitError):
            U.Universe(data, evidence(), rules()).decide("2024-01-10")

    def test_a_break_across_a_gap_cannot_be_explained(self):
        data = market(lambda d: {"590901": etf_row(d, close=1000.0 if d < "2024-01-10" else 100.0)},
                      gaps=("2024-01-10",))
        split = {"id": "syn", "exchange": "BSE", "key": "590901", "type": "unit-split", "units_multiplier": 10,
                 "record_date": None, "ex_date": "2024-01-11", "verification": "verified", "documents": [DOC],
                 "announced_on": "2024-01-02"}
        out = one(decide(data, evidence(actions=[split]), "2024-01-19", max_unknown=0.2)).tests["corporate"]
        self.assertEqual(out[0], "unresolved")
        self.assertIn("not adjacent", out[1])


class Identity(unittest.TestCase):
    def changed(self):
        return market(lambda d: {"590901": etf_row(d, isin=A if d < "2024-01-15" else B)})

    def conf(self, **kw):
        c = {"exchange": "BSE", "key": "590901", "from_isin": A, "to_isin": B, "verification": "verified",
             "effective_on": "2024-01-15", "source_url": "https://x", "retrieved_on": "2026-10-28"}
        c.update(kw)
        return c

    def test_an_isin_change_needs_a_verified_confirmation_dated_by_the_decision(self):
        ev = lambda *cs: evidence(isin=B, confirmations=cs)  # noqa: E731
        self.assertEqual(one(decide(self.changed(), ev(), "2024-01-19")).tests["identity"][0], "unresolved")
        self.assertEqual(one(decide(self.changed(), ev(self.conf()), "2024-01-19")).tests["identity"][0], "pass")
        self.assertEqual(one(decide(self.changed(), ev(self.conf(verification="corroborated")), "2024-01-19"))
                         .tests["identity"][0], "unresolved")
        self.assertEqual(one(decide(self.changed(), ev(self.conf(effective_on="2024-01-25")), "2024-01-19"))
                         .tests["identity"][0], "unresolved")

    def test_a_code_that_carried_a_share_isin_first_shows_the_change(self):
        def rows(d):
            if d < "2024-01-15":
                return {"590901": dict(etf_row(d, isin="INE000SYN099"), identity_only=True, close=None, value=None)}
            return {"590901": etf_row(d)}
        out = one(decide(market(rows), evidence(), "2024-02-09")).tests["identity"]
        self.assertEqual(out[0], "unresolved")
        self.assertIn("INE000SYN099 -> %s" % A, out[1])

    def test_one_isin_under_two_keys_fails(self):
        data = market(lambda d: {"590901": etf_row(d), "590902": etf_row(d, "590902")})
        self.assertEqual(one(decide(data, evidence(), "2024-01-19")).tests["identity"][0], "fail")


class Other(unittest.TestCase):
    def test_distributions_need_a_review_covering_the_window(self):
        data = market(lambda d: {"590901": etf_row(d)})
        out = one(decide(data, evidence(review=False), "2024-01-19")).tests["distributions"]
        self.assertEqual(out[0], "unresolved")
        self.assertIn("cannot show that no distribution occurred", out[1])

    def test_affordability_uses_the_last_close_up_to_the_decision(self):
        data = market(lambda d: {"590901": etf_row(d, close=3500.0 if d <= "2024-01-19" else 300.0)})
        self.assertEqual(one(decide(data, evidence(), "2024-01-19")).tests["affordable"][0], "fail")
        self.assertEqual(one(decide(data, evidence(), "2024-02-09")).tests["affordable"][0], "pass")

    def test_snapshots_are_reproducible(self):
        mk = lambda: decide(market(lambda d: {"590901": etf_row(d)}), evidence(), "2024-01-19")  # noqa: E731
        a, b = mk(), mk()
        self.assertEqual((a.to_json(), a.digest()), (b.to_json(), b.digest()))
        self.assertEqual(json.loads(a.to_json())["as_of"], "2024-01-19")

    def test_rules_must_match_the_market_venue(self):
        with self.assertRaises(U.UniverseError):
            U.Universe(market(lambda d: {}), evidence(), rules(venue="NSE"))


if __name__ == "__main__":
    unittest.main()
