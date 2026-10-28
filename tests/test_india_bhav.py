import os
import tempfile
import unittest

import _india_fixtures as fx
import bhav  # noqa: E402
import fetch  # noqa: E402
import sources as src  # noqa: E402

ROWS = [("590901", "SYNTH NIFTY", "INF000SYN011", 101.5, 100.0, 1000, 101500.0)]


class Parse(unittest.TestCase):
    def test_bse_legacy(self):
        layout, rows = bhav.parse(fx.bse_legacy("2018-01-02", ROWS), "2018-01-02")
        self.assertEqual(layout, "bse-legacy")
        r = rows[0]
        self.assertEqual((r["exchange"], r["code"], r["symbol"], r["isin"]), ("BSE", "590901", "SYNTH NIFTY", "INF000SYN011"))
        self.assertEqual((r["close"], r["prev_close"], r["volume"], r["value"]), (101.5, 100.0, 1000.0, 101500.0))

    def test_nse_legacy(self):
        data = fx.nse_legacy("2020-01-02", [("SYNTHBEES", "EQ", "INF000SYN011", 130.1, 129.4, 500, 65050.0)])
        layout, rows = bhav.parse(data, "2020-01-02")
        self.assertEqual(layout, "nse-legacy")
        self.assertEqual((rows[0]["exchange"], rows[0]["code"], rows[0]["symbol"], rows[0]["series"]),
                         ("NSE", None, "SYNTHBEES", "EQ"))

    def test_udiff_names_its_exchange(self):
        for src in ("NSE", "BSE"):
            data = fx.udiff("2026-10-27", src, [("11", "SYNTH", "EQ", "INF000SYN011", "SYNTH ETF", 259.0, 258.0, 10, 2590.0)])
            layout, rows = bhav.parse(data, "2026-10-27")
            self.assertEqual((layout, rows[0]["exchange"], rows[0]["lot"]), ("udiff", src, 1.0))

    def test_a_file_dated_differently_is_refused(self):
        with self.assertRaisesRegex(bhav.BhavError, "fetched for 2018-01-03"):
            bhav.parse(fx.bse_legacy("2018-01-02", ROWS), "2018-01-03")

    def test_mixed_dates_unknown_layouts_and_bad_archives(self):
        text = fx.udiff("2026-10-27", "BSE", [("1", "A", "B", "INF000SYN011", "A", 1, 1, 1, 1)]).decode()
        text += text.splitlines()[1].replace("2026-10-27", "2026-10-28", 1) + "\n"
        with self.assertRaisesRegex(bhav.BhavError, "mixes dates"):
            bhav.parse(text.encode())
        with self.assertRaisesRegex(bhav.BhavError, "layout"):
            bhav.parse(b"Date,Close\n2026-10-27,1\n")
        with self.assertRaisesRegex(bhav.BhavError, "one CSV"):
            bhav.parse(fx.zip_of("a.csv", "x", extra="b.csv"))
        with self.assertRaisesRegex(bhav.BhavError, "no rows"):
            bhav.parse((fx.BSE_HEAD + "\n").encode())
        with self.assertRaisesRegex(bhav.BhavError, "not a date"):
            bhav.parse(fx.udiff("27/10/2026", "BSE", [("1", "A", "B", "INF000SYN011", "A", 1, 1, 1, 1)]))


class Collect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_observations_read_only_price_files_in_date_order(self):
        fx.put_file(self.cache, "bse-bhavcopy-isin", "2018-01-03", fx.bse_legacy("2018-01-03", ROWS))
        fx.put_file(self.cache, "bse-bhavcopy-isin", "2018-01-02", fx.bse_legacy("2018-01-02", ROWS))
        self.cache.put(b"x;y\n", {"kind": "fetch", "ok": True, "source": "amfi-nav-latest", "url": "u"})
        fx.put_refusal(self.cache, "bse-bhavcopy-udiff", "2018-01-02")
        got = [rows[0]["date"] for _, _, rows in bhav.observations(self.cache, "BSE")]
        self.assertEqual(got, ["2018-01-02", "2018-01-03"])
        self.assertEqual(bhav.observations(self.cache, "NSE"), [])

    def test_fetch_session_tries_each_layout_and_remembers_what_worked(self):
        asked = []

        class F(object):
            cache = self.cache

            def get(_, sid, day):
                asked.append(sid)
                if sid != "bse-bhavcopy-isin":
                    raise fetch.FetchError("no", "not-data")
                return fx.put_file(self.cache, sid, day, fx.bse_legacy(day, ROWS))
        prefer = [None]
        rec = bhav.fetch_session(F(), "BSE", fx.d("2018-01-02"), prefer)
        self.assertEqual((rec["source"], prefer[0]), ("bse-bhavcopy-isin", "bse-bhavcopy-isin"))
        self.assertEqual(asked, ["bse-bhavcopy-udiff", "bse-bhavcopy-isin"])
        bhav.fetch_session(F(), "BSE", fx.d("2018-01-03"), prefer)
        self.assertEqual(asked[-1], "bse-bhavcopy-isin")
        self.assertEqual(len(asked), 3)

    def test_fetch_session_returns_none_when_every_layout_is_refused_and_stops_on_network(self):
        class F(object):
            cache = self.cache

            def __init__(self, reason):
                self.reason = reason

            def get(self, sid, day):
                raise fetch.FetchError("no", self.reason)
        self.assertIsNone(bhav.fetch_session(F("http"), "BSE", fx.d("2018-01-02"), [None]))
        with self.assertRaises(fetch.FetchError):
            bhav.fetch_session(F("network"), "BSE", fx.d("2018-01-02"), [None])

    def test_sampling_skips_weekends_by_choice(self):
        self.assertEqual([x.isoformat() for x in bhav.weekdays(fx.d("2024-01-05"), fx.d("2024-01-08"))],
                         ["2024-01-05", "2024-01-08"])


class Collector(unittest.TestCase):
    """The resumable daily collector, against a fake network that answers by URL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))
        self.doc = src.read()
        self.calls = []
        self.answers = {}

    def tearDown(self):
        self.tmp.cleanup()

    def net(self, url, headers, timeout, max_bytes):
        self.calls.append(url)
        a = self.answers.get(url, "html")
        if a == "down":
            raise fetch.FetchError("could not reach %s" % url)
        body = b"<!DOCTYPE html><html>not found</html>" if a == "html" else a
        return {"status": 200, "url": url, "content_type": "", "body": body}

    def url(self, sid, day):
        s = src.get(self.doc, sid)
        return src.render(s, src.params_for(s, day))

    def fetcher(self):
        return fetch.Fetcher(self.doc, self.cache, opener=self.net, clock=lambda: 0.0, sleep=lambda s: None)

    def legacy(self, day):
        self.answers[self.url("bse-bhavcopy-isin", day)] = fx.bse_legacy(day, ROWS)

    def test_files_refusals_and_layout_order(self):
        self.legacy("2024-01-01")
        self.legacy("2024-01-03")      # 2024-01-02 answers a page in both layouts: a non-trading response
        u = "2024-08-05"
        self.answers[self.url("bse-bhavcopy-udiff", u)] = fx.udiff(u, "BSE", [("1", "S", "B", "INF000SYN011", "S", 1, 1, 1, 1)])
        res = bhav.collect(self.fetcher(), "BSE", fx.d("2024-01-01"), fx.d("2024-01-03"), 50)
        self.assertEqual((res["files"], res["refused"], res["network"], res["unresolved"]), (2, 2, 0, 0))
        self.assertTrue(self.calls[0].endswith("EQ_ISINCODE_010124.ZIP"))  # legacy first before the switch
        self.calls[:] = []
        res = bhav.collect(self.fetcher(), "BSE", fx.d(u), fx.d(u), 50)
        self.assertEqual(res["files"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("BhavCopy_BSE_CM", self.calls[0])                  # UDiFF first after it

    def test_it_resumes_without_asking_again(self):
        self.legacy("2024-01-01")
        bhav.collect(self.fetcher(), "BSE", fx.d("2024-01-01"), fx.d("2024-01-02"), 50)
        n = len(self.calls)
        res = bhav.collect(self.fetcher(), "BSE", fx.d("2024-01-01"), fx.d("2024-01-02"), 50)
        self.assertEqual((len(self.calls), res["requests"], res["unresolved"]), (n, 0, 0))

    def test_the_request_budget_is_a_hard_stop(self):
        res = bhav.collect(self.fetcher(), "BSE", fx.d("2024-01-01"), fx.d("2024-01-31"), 5)
        self.assertEqual((len(self.calls), res["requests"], res["stopped"]), (5, 5, "request budget"))
        self.assertGreater(res["unresolved"], 0)

    def test_network_failures_stop_the_batch_and_stay_unresolved(self):
        for d in ("2024-01-01", "2024-01-02", "2024-01-03"):
            for sid in ("bse-bhavcopy-isin", "bse-bhavcopy-udiff"):
                self.answers[self.url(sid, d)] = "down"
        res = bhav.collect(self.fetcher(), "BSE", fx.d("2024-01-01"), fx.d("2024-01-05"), 50, max_network_failures=3)
        self.assertEqual((res["network"], res["stopped"], len(self.calls)), (3, "3 network failures in a row", 3))
        self.assertEqual(res["unresolved"], 5)

    def test_an_invalid_file_is_an_error_not_a_session_and_is_not_refetched(self):
        self.answers[self.url("bse-bhavcopy-isin", "2024-01-01")] = fx.bse_legacy("2024-01-02", ROWS)  # wrong date
        res = bhav.collect(self.fetcher(), "BSE", fx.d("2024-01-01"), fx.d("2024-01-01"), 50)
        self.assertEqual((res["invalid"], res["files"], res["unresolved"]), (1, 0, 1))
        self.assertEqual(bhav.observations(self.cache, "BSE"), [])
        import calendar_in
        self.assertEqual(calendar_in.from_cache(self.cache, "BSE").status("2024-01-01"), "invalid")
        n = len(self.calls)
        again = bhav.collect(self.fetcher(), "BSE", fx.d("2024-01-01"), fx.d("2024-01-01"), 50)
        self.assertEqual((len(self.calls), again["invalid"]), (n, 0))
        self.assertEqual(len([r for r in self.cache.records() if r.get("kind") == "invalid"]), 1)


class DerivedIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, day, rows):
        return fx.put_file(self.cache, "bse-bhavcopy-isin", day, fx.bse_legacy(day, rows))

    def test_built_once_then_read_without_parsing(self):
        rec = self.put("2024-01-02", ROWS + [("500001", "SYNTHCO", "INE000SYN099", 50.0, 50.0, 1, 50.0)])
        s = bhav.summary(self.cache, rec)
        self.assertEqual((s["date"], s["layout"], s["rows"], len(s["funds"]), len(s["keys"])),
                         ("2024-01-02", "bse-legacy", 2, 1, 2))
        self.assertTrue(os.path.exists(bhav.index_path(self.cache, rec["sha256"])))
        real = bhav.parse
        bhav.parse = lambda *a, **k: self.fail("the index should have been used")
        try:
            self.assertEqual(bhav.summary(self.cache, rec)["funds"], s["funds"])
        finally:
            bhav.parse = real

    def test_funds_only_matches_a_full_parse_for_fund_rows(self):
        self.put("2024-01-02", ROWS + [("500001", "SYNTHCO", "INE000SYN099", 50.0, 50.0, 1, 50.0)])
        full = bhav.observations(self.cache, "BSE")[0][2]
        fast = bhav.observations(self.cache, "BSE", funds_only=True)[0][2]
        self.assertEqual([x for x in full if x["isin"].startswith("INF")], fast)

    def test_a_code_reused_from_a_share_to_an_etf_stays_visible(self):
        import secmaster
        self.put("2024-01-02", [("590901", "OLDCO", "INE000SYN099", 50.0, 50.0, 1, 50.0)])
        self.put("2024-01-03", ROWS)
        obs = bhav.observations(self.cache, "BSE", funds_only=True)
        self.assertTrue(obs[0][2][0]["identity_only"])
        self.assertIsNone(obs[0][2][0]["close"])
        m = secmaster.from_cache(self.cache, confirmations=[])
        e = [x for x in m.events if x["type"] == "isin-change"]
        self.assertEqual((e[0]["from_isin"], e[0]["to_isin"]), ("INE000SYN099", "INF000SYN011"))

    def test_identity_rows_are_never_prices(self):
        import etf
        self.put("2024-01-02", [("590901", "OLDCO", "INE000SYN099", 50.0, 50.0, 1, 50.0)])
        self.put("2024-01-03", ROWS)
        crit = {"window": {"start": "2024-01-02", "end": None}, "venue": "BSE", "capital_inr": 3000}
        cand = {"category": "nifty50", "isin": "INF000SYN011", "exchange": "BSE", "key": "590901", "symbol": "S",
                "amfi_name": "x"}
        _, rows = etf.liquidity_table(self.cache, crit, [cand], [1])
        self.assertEqual((rows[0]["first"], rows[0]["sessions"]), ("2024-01-03", 1))

    def test_a_file_without_funds_still_counts_as_a_session(self):
        import calendar_in
        self.put("2024-01-02", [("500001", "SYNTHCO", "INE000SYN099", 50.0, 50.0, 1, 50.0)])
        self.assertEqual(calendar_in.from_cache(self.cache, "BSE").status("2024-01-02"), "trading")


class GapConsistency(unittest.TestCase):
    """bhav.py and calendar_in.py must classify a corrupt published file the same way."""

    def setUp(self):
        import json
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))
        fx.put_file(self.cache, "bse-bhavcopy-isin", "2024-01-02", fx.bse_legacy("2024-01-02", ROWS))
        bad = fx.put_file(self.cache, "bse-bhavcopy-isin", "2024-01-03", fx.bse_legacy("2024-01-04", ROWS))  # wrong date
        self.cache.note({"kind": "invalid", "ok": False, "source": "bse-bhavcopy-isin", "url": bad["url"],
                         "date": "2024-01-03", "sha256": bad["sha256"], "detail": "synthetic corrupt file"})
        fx.put_refusal(self.cache, "bse-bhavcopy-udiff", "2024-01-03")
        self.sha = bad["sha256"]
        self.gaps = os.path.join(self.tmp.name, "gaps.json")

        def write(sha):
            with open(self.gaps, "w") as fh:
                json.dump({"gaps": [{"venue": "BSE", "date": "2024-01-03", "sha256": sha, "reason": "synthetic",
                                     "status": "unresolved", "handling": "excluded"}]}, fh)
        self.write = write

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_checksum_matched_gap_is_documented_not_unresolved(self):
        self.write(self.sha)
        cls = bhav.classify(self.cache.records(), "BSE", fx.d("2024-01-02"), fx.d("2024-01-03"), self.gaps)
        self.assertEqual(cls, {"2024-01-02": "valid-file", "2024-01-03": "documented-gap"})
        self.assertEqual(bhav.unresolved(self.cache.records(), "BSE", fx.d("2024-01-02"), fx.d("2024-01-03"),
                                         self.gaps), [])

    def test_a_gap_with_another_checksum_stays_invalid_and_unresolved(self):
        self.write("b" * 64)
        cls = bhav.classify(self.cache.records(), "BSE", fx.d("2024-01-03"), fx.d("2024-01-03"), self.gaps)
        self.assertEqual(cls["2024-01-03"], "invalid-undocumented")
        self.assertEqual(bhav.unresolved(self.cache.records(), "BSE", fx.d("2024-01-03"), fx.d("2024-01-03"),
                                         self.gaps), ["2024-01-03"])

    def test_both_tools_agree_day_by_day(self):
        import calendar_in
        for sha, want_cal in ((self.sha, "gap"), ("b" * 64, "invalid")):
            self.write(sha)
            cls = bhav.classify(self.cache.records(), "BSE", fx.d("2024-01-02"), fx.d("2024-01-03"), self.gaps)
            cal = calendar_in.from_cache(self.cache, "BSE", gaps_path=self.gaps)
            self.assertEqual(cal.status("2024-01-03"), want_cal)
            self.assertEqual(cls["2024-01-03"] == "documented-gap", cal.status("2024-01-03") == "gap")
            self.assertEqual(cal.status("2024-01-02"), "trading")

    def test_the_corrupt_file_is_kept_and_never_read_as_data(self):
        self.write(self.sha)
        self.assertTrue(self.cache.read(self.sha))                       # still in the cache, checksum intact
        self.assertEqual([r["date"] for r, _, _ in bhav.observations(self.cache, "BSE")], ["2024-01-02"])

    def test_the_shipped_gaps_match_the_real_records_rule(self):
        import calendar_in
        listed = sorted(d.isoformat() for d in calendar_in.read_gaps(exchange="BSE"))
        self.assertEqual(listed, ["2022-02-07", "2022-07-15"])


if __name__ == "__main__":
    unittest.main()
