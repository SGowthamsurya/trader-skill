import os
import tempfile
import unittest

import _india_fixtures as fx
import calendar_in as cal  # noqa: E402
import fetch  # noqa: E402

ROWS = [("590901", "SYNTH", "INF000SYN011", 100.0, 100.0, 10, 1000.0)]
URL = "https://example.org/official-holidays"


def make(files, refused=(), network=(), official=()):
    attempts = [(d, s, "not-data") for d, s in refused] + [(d, "bse-bhavcopy-isin", "network") for d in network]
    return cal.Calendar("BSE", {d: "sha" for d in files}, attempts, [(d, t, "", "sha") for d, t in official])


BOTH = ("bse-bhavcopy-isin", "bse-bhavcopy-udiff")


class Status(unittest.TestCase):
    def test_observed_file_means_trading_and_nothing_else_is_inferred(self):
        c = make(["2024-01-05"])
        self.assertEqual(c.status("2024-01-05"), "trading")
        self.assertEqual(c.status("2024-01-08"), "unobserved")
        self.assertEqual(c.status("2024-01-06"), "unobserved")  # a weekend is not assumed closed

    def test_no_file_needs_every_layout_refused_and_is_never_a_holiday(self):
        c = make([], refused=[("2024-01-26", "bse-bhavcopy-isin")])
        self.assertEqual(c.status("2024-01-26"), "unobserved")
        c = make([], refused=[("2024-01-26", s) for s in BOTH])
        self.assertEqual(c.status("2024-01-26"), "no-file")

    def test_network_failures_are_unreachable_not_closed(self):
        self.assertEqual(make([], network=["2024-01-26"]).status("2024-01-26"), "unreachable")

    def test_official_records_and_conflicts(self):
        c = make(["2024-01-22"], official=[("2024-01-22", "holiday"), ("2024-01-26", "holiday"),
                                           ("2024-11-01", "session")])
        self.assertEqual(c.status("2024-01-22"), "conflict")
        self.assertEqual(c.status("2024-01-26"), "official-holiday")
        self.assertEqual(c.status("2024-11-01"), "official-session")

    def test_dates_after_the_last_observed_session_are_never_predicted(self):
        c = make(["2024-01-01", "2024-01-02"], refused=[("2023-12-25", s) for s in BOTH])
        self.assertEqual(c.last_observed.isoformat(), "2024-01-02")
        self.assertEqual(c.status("2024-12-25"), "unobserved")  # same day as a past closure, a year on

    def test_year_boundary(self):
        c = make(["2018-12-31", "2019-01-01"])
        self.assertEqual([x.isoformat() for x in c.sessions("2018-12-30", "2019-01-02")], ["2018-12-31", "2019-01-01"])
        self.assertEqual(c.adjacent("2018-12-31", "2019-01-01"), (True, []))


class CoverageAndAdjacency(unittest.TestCase):
    def setUp(self):
        days = list(fx.weekdays("2024-01-01", "2024-01-12"))
        days.remove("2024-01-10")
        self.c = make(days, refused=[("2024-01-10", s) for s in BOTH])

    def test_coverage_counts_and_completeness(self):
        cov = self.c.coverage("2024-01-01", "2024-01-14")
        self.assertEqual(cov["weekday"], {"trading": 9, "no-file": 1})
        self.assertEqual(cov["weekend"], {"unobserved": 4})
        self.assertTrue(cov["complete_weekdays"])
        self.assertEqual(cov["unconfirmed_closures"], 1)
        self.assertFalse(self.c.coverage("2024-01-01", "2024-01-16")["complete_weekdays"])

    def test_adjacency_across_a_weekend_states_its_assumption(self):
        ok, assumed = self.c.adjacent("2024-01-05", "2024-01-08")
        self.assertTrue(ok)
        self.assertEqual(len(assumed), 2)
        self.assertIn("weekend", assumed[0])

    def test_adjacency_across_a_missing_session(self):
        ok, assumed = self.c.adjacent("2024-01-09", "2024-01-11")
        self.assertTrue(ok)
        self.assertIn("not officially confirmed", assumed[0])
        self.assertEqual(self.c.adjacent("2024-01-09", "2024-01-16"), (False, []))  # 01-15 unobserved

    def test_an_official_weekend_session_breaks_adjacency(self):
        c = make(["2024-01-05", "2024-01-08"], official=[("2024-01-06", "session")])
        self.assertEqual(c.adjacent("2024-01-05", "2024-01-08"), (False, []))


class Official(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        p = os.path.join(self.tmp.name, "h.csv")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_import_with_provenance_and_read_back(self):
        p = self.write("date,type,description\n2024-01-26,holiday,Synthetic Day\n2024-11-01,session,Synthetic session\n")
        rec = cal.import_official(p, self.cache, "BSE", URL, "2026-10-29")
        self.assertEqual((rec["rows"], rec["first"], rec["last"]), (2, "2024-01-26", "2024-11-01"))
        fx.put_file(self.cache, "bse-bhavcopy-isin", "2024-01-25", fx.bse_legacy("2024-01-25", ROWS))
        c = cal.from_cache(self.cache, "BSE")
        self.assertEqual(c.status("2024-01-26"), "official-holiday")
        self.assertEqual(c.status("2024-01-25"), "trading")
        self.assertEqual(cal.from_cache(self.cache, "NSE").status("2024-01-26"), "unobserved")

    def test_bad_lists_are_refused(self):
        for text, why in (("date,type,description\n2024-01-26,closed,x\n", "type"),
                          ("date,type,description\n2024-01-26,holiday,x\n2024-01-26,holiday,y\n", "twice"),
                          ("date,type\n2024-01-26,holiday\n", "description"),
                          ("date,type,description\n26/01/2024,holiday,x\n", "YYYY-MM-DD")):
            with self.assertRaisesRegex(cal.CalendarError, why):
                cal.import_official(self.write(text), self.cache, "BSE", URL, "2026-10-29")
        good = self.write("date,type,description\n2024-01-26,holiday,x\n")
        with self.assertRaisesRegex(cal.CalendarError, "https"):
            cal.import_official(good, self.cache, "BSE", "http://x", "2026-10-29")
        with self.assertRaisesRegex(cal.CalendarError, "NSE or BSE"):
            cal.import_official(good, self.cache, "MCX", URL, "2026-10-29")
        with self.assertRaisesRegex(cal.CalendarError, "web page"):
            cal.import_official(self.write("<!DOCTYPE html><html></html>"), self.cache, "BSE", URL, "2026-10-29")
        self.assertEqual(self.cache.records(), [])

    def test_from_cache_uses_refusals_of_this_exchange_only(self):
        for s in ("bse-bhavcopy-isin", "bse-bhavcopy-udiff"):
            fx.put_refusal(self.cache, s, "2024-01-26")
        fx.put_refusal(self.cache, "nse-bhavcopy-legacy", "2024-01-25")
        self.assertEqual(cal.from_cache(self.cache, "BSE").status("2024-01-26"), "no-file")
        self.assertEqual(cal.from_cache(self.cache, "BSE").status("2024-01-25"), "unobserved")


NOTICE = """<html><body><div>NOTICES Notice No. 20991212-7 Notice Date 12 Dec 2098 Subject Trading Holidays for the
Calendar Year 2099 for Equity, Equity Derivatives and SLB Segments Content Trading Members are hereby informed that the
Exchange shall observe the following Trading Holidays during the Calendar Year 2099 for the Equity Segments.
<table><tr><td>1</td><td>Synthetic Day</td><td>February 26,2099</td><td>Thursday</td></tr>
<tr><td>2</td><td>Lamp Festival</td><td>October 21, 2099</td><td>Wednesday</td></tr></table>
Following Holidays are Falling on Saturday / Sunday
<table><tr><td>1</td><td>Weekend Day</td><td>January 25,2099</td><td>Sunday</td></tr></table>
*Muhurat Trading will be conducted on Wednesday, October 21, 2099. For and on behalf of BSE Ltd.</div></body></html>"""


class BseNotice(unittest.TestCase):
    def test_dates_and_a_muhurat_session_are_read_from_the_text(self):
        number, year, rows = cal.parse_bse_holiday_notice(NOTICE)
        self.assertEqual((number, year), ("20991212-7", 2099))
        self.assertEqual([(d.isoformat(), t) for d, t, _ in rows],
                         [("2099-01-25", "holiday"), ("2099-02-26", "holiday"), ("2099-10-21", "session")])

    def test_other_pages_and_bad_dates_are_refused(self):
        with self.assertRaisesRegex(cal.CalendarError, "not a BSE trading-holiday notice"):
            cal.parse_bse_holiday_notice("<html>Notice No. 20991212-7 Subject Circuit filters</html>")
        with self.assertRaisesRegex(cal.CalendarError, "outside the notice's year"):
            cal.parse_bse_holiday_notice(NOTICE.replace("February 26,2099", "February 26,2098"))
        with self.assertRaisesRegex(cal.CalendarError, "appears twice"):
            cal.parse_bse_holiday_notice(NOTICE.replace("October 21, 2099", "February 26,2099"))

    def test_import_through_the_fetcher(self):
        import sources as src
        with tempfile.TemporaryDirectory() as d:
            cache = fetch.Cache(os.path.join(d, "c"))
            doc = src.read()
            url = src.render(src.get(doc, "bse-notice"), {"NOTICE": "20991212-7"})
            f = fetch.Fetcher(doc, cache, opener=lambda u, h, t, m: {
                "status": 200, "url": u, "content_type": "text/html", "body": NOTICE.encode()},
                clock=lambda: 0.0, sleep=lambda s: None)
            rec = cal.import_bse_notice(f, "20991212-7")
            self.assertEqual((rec["source_url"], rec["rows"], rec["year"]), (url, 3, 2099))
            c = cal.from_cache(cache, "BSE")
            self.assertEqual(c.status("2099-02-26"), "official-holiday")
            self.assertEqual(c.status("2099-10-21"), "official-session")
            with self.assertRaisesRegex(cal.CalendarError, "page is notice"):
                cal.import_bse_notice(fetch.Fetcher(doc, cache, opener=lambda u, h, t, m: {
                    "status": 200, "url": u, "content_type": "", "body": NOTICE.encode()},
                    clock=lambda: 0.0, sleep=lambda s: None), "20991212-8")


if __name__ == "__main__":
    unittest.main()
