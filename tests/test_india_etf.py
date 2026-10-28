import copy
import json
import os
import re
import tempfile
import unittest

import _india_fixtures as fx
import etf  # noqa: E402
import fetch  # noqa: E402

A, B = "INF000SYN011", "INF000SYN022"
CAND = {"category": "nifty50", "isin": A, "exchange": "BSE", "key": "590901", "symbol": "SYNBEES",
        "amfi_name": "Synthetic Nifty 50 ETF"}
CRIT = {"window": {"start": "2024-01-01", "end": None}, "venue": "BSE", "capital_inr": 3000,
        "start_tolerance_days": {"value": 10}, "min_presence": {"value": 0.95}, "break_ratio": {"value": 1.5},
        "liquidity": {"min_median_value_inr": 500000}}
DAYS = list(fx.weekdays("2024-01-01", "2024-01-12"))
DOC = {"title": "Synthetic notice", "url": "https://example.org/notice.pdf", "retrieved_on": "2026-10-28",
       "sha256": "0" * 64}


def action(**kw):
    a = {"id": "synthetic-split", "exchange": "BSE", "key": "590901", "type": "unit-split", "units_multiplier": 10,
         "record_date": None, "ex_date": None, "verification": "verified", "documents": [DOC]}
    a.update(kw)
    return a


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, price=lambda d: 100.0, isin=lambda d: A, days=DAYS, skip=(), extra=lambda d: [],
              traded=lambda d: True):
        for d in days:
            if d in skip:
                for s in ("bse-bhavcopy-isin", "bse-bhavcopy-udiff"):
                    fx.put_refusal(self.cache, s, d)
                continue
            rows = [("590901", "SYNBEES", isin(d), price(d), price(d), 10000, 1000000.0)] if traded(d) else []
            rows = rows + extra(d) + [("500001", "SYNTHCO", "INE000SYN099", 50.0, 50.0, 1, 50.0)]
            fx.put_file(self.cache, "bse-bhavcopy-isin", d, fx.bse_legacy(d, rows))

    def run_(self, crit=CRIT, cand=CAND, actions=(), confirmations=()):
        end, res = etf.evaluate_all(self.cache, crit, [cand], list(actions), list(confirmations))
        return res[0]["verdict"], {k: v[0] for k, v in res[0]["criteria"].items()}, res[0]["criteria"]

    def test_a_clean_complete_history_is_eligible(self):
        self.build(skip=("2024-01-10",))
        verdict, out, _ = self.run_()
        self.assertEqual(verdict, "ELIGIBLE")
        self.assertEqual(set(out.values()), {"pass"})

    def test_no_liquidity_threshold_means_insufficient_data(self):
        self.build()
        crit = copy.deepcopy(CRIT)
        crit["liquidity"]["min_median_value_inr"] = None
        verdict, out, why = self.run_(crit)
        self.assertEqual((verdict, out["liquidity"]), ("INSUFFICIENT DATA", "unresolved"))
        self.assertIn("threshold not chosen", why["liquidity"][1])
        crit["liquidity"]["min_median_value_inr"] = 10 ** 9
        self.assertEqual(self.run_(crit)[1]["liquidity"], "fail")

    def test_missing_weekdays_leave_it_insufficient(self):
        self.build(days=DAYS[:5] + DAYS[6:])
        verdict, out, why = self.run_()
        self.assertEqual((verdict, out["complete"]), ("INSUFFICIENT DATA", "unresolved"))
        self.assertIn("1 weekdays", why["complete"][1])

    def test_a_unit_above_the_capital_fails(self):
        self.build(price=lambda d: 3289.43 if d == "2024-01-03" else 100.0)
        verdict, out, why = self.run_()
        self.assertEqual((verdict, out["affordable"]), ("NOT ELIGIBLE", "fail"))
        self.assertIn("3289.43 on 2024-01-03", why["affordable"][1])

    def test_a_split_needs_a_confirmed_action(self):
        self.build(price=lambda d: 1000.0 if d < "2024-01-09" else 100.5)
        crit = dict(CRIT, capital_inr=5000)
        verdict, out, why = self.run_(crit)
        self.assertEqual((verdict, out["corporate"]), ("INSUFFICIENT DATA", "unresolved"))
        self.assertIn("2024-01-08 -> 2024-01-09", why["corporate"][1])
        self.assertEqual(self.run_(crit, actions=[action(ex_date="2024-01-09")])[1]["corporate"], "pass")
        self.assertEqual(self.run_(crit, actions=[action(ex_date="2024-01-09", verification="unverified")])[1]
                         ["corporate"], "unresolved")

    def test_a_record_date_matches_its_own_session_or_the_one_before(self):
        self.build(price=lambda d: 1000.0 if d < "2024-01-09" else 100.5)
        crit = dict(CRIT, capital_inr=5000)
        for rd, want in (("2024-01-10", "pass"), ("2024-01-09", "pass"), ("2024-01-11", "unresolved")):
            got = self.run_(crit, actions=[action(record_date=rd)])[1]["corporate"]
            self.assertEqual(got, want, rd)

    def test_the_document_ratio_must_agree_with_the_prices(self):
        self.build(price=lambda d: 1000.0 if d < "2024-01-09" else 100.5)
        verdict, out, why = self.run_(dict(CRIT, capital_inr=5000), actions=[action(ex_date="2024-01-09", units_multiplier=2)])
        self.assertEqual((verdict, out["corporate"]), ("NOT ELIGIBLE", "fail"))
        self.assertIn("states 1/2", why["corporate"][1])

    def test_a_break_across_unobserved_days_cannot_be_located(self):
        self.build(price=lambda d: 1000.0 if d < "2024-01-09" else 100.0, days=[d for d in DAYS if d != "2024-01-08"])
        why = self.run_(dict(CRIT, capital_inr=5000))[2]
        self.assertIn("not adjacent", why["corporate"][1])

    def test_an_isin_change_needs_confirmation(self):
        self.build(isin=lambda d: A if d < "2024-01-08" else B)
        cand = dict(CAND, isin=B)
        verdict, out, why = self.run_(cand=cand)
        self.assertEqual((verdict, out["identity"]), ("INSUFFICIENT DATA", "unresolved"))
        c = {"exchange": "BSE", "key": "590901", "from_isin": A, "to_isin": B, "effective_on": "2024-01-08",
             "verification": "verified", "source_url": "https://x", "retrieved_on": "2026-10-29"}
        self.assertEqual(self.run_(cand=cand, confirmations=[c])[0], "ELIGIBLE")
        verdict, out, why = self.run_(cand=cand, confirmations=[dict(c, verification="corroborated")])
        self.assertEqual((verdict, out["identity"]), ("INSUFFICIENT DATA", "unresolved"))
        self.assertIn("corroborated but not verified", why["identity"][1])

    def test_a_duplicate_mapping_fails_identity(self):
        self.build(extra=lambda d: [("590902", "OTHER", A, 100.0, 100.0, 1, 1.0)] if d == "2024-01-04" else [])
        verdict, out, _ = self.run_()
        self.assertEqual((verdict, out["identity"]), ("NOT ELIGIBLE", "fail"))

    def test_a_late_or_patchy_listing_fails(self):
        self.build(traded=lambda d: d >= DAYS[9])  # the venue traded every day; this ETF only on the last
        out = self.run_(dict(CRIT, start_tolerance_days={"value": 3}))[1]
        self.assertEqual((out["start"], out["presence"]), ("fail", "fail"))


class Liquidity(unittest.TestCase):
    def test_missed_sessions_count_as_zero_from_the_first_observed_day(self):
        sessions = ["2024-01-0%d" % i for i in range(1, 6)]
        mine = [{"date": "2024-01-02", "value": 100.0}, {"date": "2024-01-04", "value": 300.0},
                {"date": "2024-01-05", "value": 500.0}]
        v = etf.daily_values(sessions, mine)
        self.assertEqual((v["sessions"], v["traded"], v["median"]), (4, 3, 200.0))   # 0, 100, 300, 500
        self.assertEqual(etf.daily_values(sessions, [])["median"], None)

    def test_the_table_shows_each_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            cache = fetch.Cache(d)
            for day in DAYS:
                fx.put_file(cache, "bse-bhavcopy-isin", day, fx.bse_legacy(day, [
                    ("590901", "SYNBEES", A, 100.0, 100.0, 100, 400000.0),
                    ("590902", "THIN", B, 100.0, 100.0, 1, 1000.0)]))
            cands = [CAND, dict(CAND, key="590902", isin=B, symbol="THIN")]
            end, rows = etf.liquidity_table(cache, CRIT, cands, [300000, 1000000])
        got = {r["candidate"]["symbol"]: r["passes"] for r in rows}
        self.assertEqual(got, {"SYNBEES": {300000: True, 1000000: False}, "THIN": {300000: False, 1000000: False}})
        self.assertAlmostEqual([r for r in rows if r["candidate"]["symbol"] == "SYNBEES"][0]["capital_share"], 0.0075)

    def test_the_proposed_threshold_is_not_applied_until_approved(self):
        crit = etf.read_json(etf.CRITERIA, "criteria")
        self.assertEqual(crit["liquidity"]["proposed_value"], 300000)
        self.assertIsNone(crit["liquidity"]["min_median_value_inr"])


class Discovery(unittest.TestCase):
    def test_categories_come_from_official_scheme_names(self):
        cases = {"Nippon India ETF Nifty 50 BeES": "nifty50",
                 "Nippon India ETF Nifty Next 50 Junior BeES": "niftynext50",
                 "SBI Nifty Bank ETF": "bank",
                 "ICICI Prudential Nifty Private Bank ETF": None, "Kotak Nifty PSU Bank ETF": None,
                 "UTI Nifty 50 Index Fund": None, "Mirae Asset Nifty 50 Equal Weight ETF": None,
                 "Nippon India ETF Gold BeES": None, "Motilal Oswal Nasdaq 100 ETF": None,
                 "Sample Nifty 50 Value 20 ETF": None, "Sample Nifty Bank Index Fund": None, None: None}
        for name, want in cases.items():
            self.assertEqual(etf.category(name), want, name)

    def test_amfi_names(self):
        text = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date\n"
                "\nSynthetic Mutual Fund\n"
                "100001;INF000SYN011;-;Synthetic Nifty 50 ETF;;;100.0;27-Oct-2026\n")
        self.assertEqual(etf.amfi_names(text), {A: "Synthetic Nifty 50 ETF"})

    def test_discover_from_a_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = fetch.Cache(d)
            cache.put(b"100001;INF000SYN011;-;Synthetic Nifty 50 ETF;;;100.0;27-Oct-2026\n"
                      b"100002;INF000SYN022;-;Synthetic Gold ETF;;;10.0;27-Oct-2026\n",
                      {"kind": "fetch", "ok": True, "source": "amfi-nav-latest", "url": "https://a",
                       "retrieved_at": "2026-10-28T00:00:00Z"})
            fx.put_file(cache, "bse-bhavcopy-isin", "2024-01-02", fx.bse_legacy("2024-01-02", [
                ("590901", "SYNBEES", A, 1, 1, 1, 1), ("590902", "SYNGOLD", B, 1, 1, 1, 1)]))
            doc = etf.discover(cache, "BSE")
        self.assertEqual([(c["symbol"], c["category"]) for c in doc["candidates"]], [("SYNBEES", "nifty50")])
        self.assertEqual(doc["price_file"]["date"], "2024-01-02")


class ShippedFiles(unittest.TestCase):
    def test_criteria_leave_the_liquidity_threshold_to_the_user(self):
        crit = etf.read_json(etf.CRITERIA, "criteria")
        self.assertIsNone(crit["liquidity"]["min_median_value_inr"])
        self.assertEqual(crit["capital_inr"], 3000)
        self.assertEqual(crit["window"]["start"], "2018-01-01")

    def test_candidates_are_equity_index_etfs_with_provenance(self):
        doc = etf.read_json(etf.CANDIDATES, "candidates")
        self.assertRegex(doc["amfi_file"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(doc["price_file"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual({c["category"] for c in doc["candidates"]}, {"nifty50", "niftynext50", "bank"})
        for c in doc["candidates"]:
            self.assertEqual(etf.category(c["amfi_name"]), c["category"], c["amfi_name"])
            self.assertNotRegex(c["amfi_name"], re.compile(r"gold|silver|liquid|gilt|nasdaq|hang seng|bond", re.I))

    def test_shipped_actions_are_valid_and_cite_documents(self):
        acts = etf.read_actions()
        self.assertTrue(acts)
        for a in acts:
            if a["verification"] == "verified":
                self.assertTrue(a["documents"], a["id"])
                self.assertIsNotNone(a["units_multiplier"])
            else:
                self.assertTrue(a.get("leads") or a["documents"], "%s has nothing to follow up" % a["id"])

    def test_action_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            for bad, why in ((action(verification="probable"), "verified or unverified"),
                             (action(), "record date or an ex-date"),
                             (action(ex_date="2024-01-09", units_multiplier=None), "units multiplier"),
                             (action(ex_date="2024-01-09", documents=[]), "cites a document"),
                             (action(ex_date="2024-01-09", documents=[dict(DOC, sha256="abc")]), "sha256"),
                             ({"id": "x"}, "has no exchange")):
                with open(p, "w") as fh:
                    json.dump({"actions": [bad]}, fh)
                with self.assertRaisesRegex(etf.EtfError, why):
                    etf.read_actions(p)

    def test_the_review_names_its_venue_and_disclaims_advice(self):
        with open(os.path.join(fx.INDIA, "ETF_REVIEW.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("(BSE, 2018-01-01", text)
        self.assertIn("Not financial advice", text)
        self.assertNotIn("| ELIGIBLE |", text)


if __name__ == "__main__":
    unittest.main()
