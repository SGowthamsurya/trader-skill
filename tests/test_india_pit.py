import os
import tempfile
import unittest

import _india_fixtures as fx
import calendar_in as cal_mod  # noqa: E402
import fetch  # noqa: E402
import pit  # noqa: E402

GAP_SHA = "a" * 64
BOTH = ("bse-bhavcopy-isin", "bse-bhavcopy-udiff")
DAYS = list(fx.weekdays("2024-01-01", "2024-01-12"))     # Mon 1st .. Fri 12th
GAP, NOFILE = "2024-01-08", "2024-01-10"


def build(price=lambda d: 100.0 + int(d[-2:]), extra_after=None):
    """Synthetic market: one ETF (key K) on every tradable session; 01-08 a documented gap, 01-10 no file."""
    files = [d for d in DAYS if d not in (GAP, NOFILE)]
    cal = cal_mod.Calendar("BSE", {d: "sha" for d in files}, [(NOFILE, s, "not-data") for s in BOTH],
                           invalid=[(GAP, GAP_SHA)], gaps={GAP: GAP_SHA})
    rows = {d: {"K": {"date": d, "exchange": "BSE", "code": "K", "isin": "INF000SYN011", "close": price(d),
                      "value": 1e6},
                "OLD": {"date": d, "exchange": "BSE", "code": "OLD", "isin": "INE000SYN099", "close": None,
                        "value": None, "identity_only": True}} for d in files}
    if extra_after:
        for d, r in extra_after.items():
            rows.setdefault(d, {}).update(r)
    return pit.MarketData(cal, rows, "BSE")


class LookAhead(unittest.TestCase):
    def test_reading_after_the_view_date_raises(self):
        v = build().view("2024-01-05")
        with self.assertRaises(pit.LookAheadError):
            v.bar("K", "2024-01-09")
        with self.assertRaises(pit.LookAheadError):
            v.status("2024-01-09")

    def test_sessions_and_history_stop_at_the_view_date(self):
        v = build().view("2024-01-05")
        self.assertEqual(v.sessions(), ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
        self.assertEqual(v.closes("K", 2), [("2024-01-04", 104.0), ("2024-01-05", 105.0)])
        self.assertEqual(v.sessions(2), ["2024-01-04", "2024-01-05"])

    def test_nothing_before_the_view_date_depends_on_data_after_it(self):
        a = build().view("2024-01-05")
        b = build(price=lambda d: (100.0 + int(d[-2:])) if d <= "2024-01-05" else 9999.0,
                  extra_after={"2024-01-09": {"NEW": {"date": "2024-01-09", "code": "NEW", "close": 1.0,
                                                      "value": 1.0}}}).view("2024-01-05")
        for f in (lambda v: v.sessions(), lambda v: v.closes("K", 10), lambda v: v.mark("K"),
                  lambda v: v.bar("K"), lambda v: v.gaps(), lambda v: v.bar("NEW", "2024-01-05")):
            self.assertEqual(f(a), f(b))

    def test_a_view_is_read_only_and_returns_copies(self):
        data = build()
        v = data.view("2024-01-05")
        with self.assertRaises(AttributeError):
            v.as_of = "2024-01-12"
        row = v.bar("K")
        row["close"] = -1.0
        self.assertEqual(v.bar("K")["close"], 105.0)
        s = v.sessions()
        s.append("2024-01-12")
        self.assertNotIn("2024-01-12", v.sessions())


class Sessions(unittest.TestCase):
    def test_no_decision_on_a_gap_a_no_file_day_or_an_unobserved_day(self):
        data = build()
        for d in (GAP, NOFILE, "2024-01-06", "2024-01-15"):
            with self.assertRaises(pit.PitError, msg=d):
                data.view(d)

    def test_a_gap_is_never_a_session_and_has_no_bar(self):
        v = build().view("2024-01-12")
        self.assertNotIn(GAP, v.sessions())
        self.assertNotIn(NOFILE, v.sessions())
        self.assertIsNone(v.bar("K", GAP))
        self.assertEqual(v.status(GAP), "gap")
        self.assertEqual(v.gaps(), [GAP])
        self.assertEqual(build().view("2024-01-05").gaps(), [])

    def test_history_across_a_gap_has_no_filled_in_value(self):
        closes = build().view("2024-01-09").closes("K", 3)
        self.assertEqual([d for d, _ in closes], ["2024-01-04", "2024-01-05", "2024-01-09"])

    def test_identity_only_rows_are_never_bars(self):
        self.assertIsNone(build().view("2024-01-05").bar("OLD"))


class Marks(unittest.TestCase):
    def test_mark_on_the_view_date_uses_its_close(self):
        self.assertEqual(build().view("2024-01-09").mark("K"), (109.0, []))

    def test_an_untraded_etf_is_marked_stale_from_the_past_only(self):
        data = build(price=lambda d: None if d == "2024-01-09" else 100.0 + int(d[-2:]))
        price, flags = data.view("2024-01-09").mark("K")
        self.assertEqual(price, 105.0)               # Friday's close, carried; never Thursday the 11th
        self.assertTrue(flags[0].startswith("stale"))

    def test_never_traded_means_no_mark(self):
        price, flags = build().view("2024-01-05").mark("NOPE")
        self.assertIsNone(price)
        self.assertTrue(flags[0].startswith("no-mark"))


class FromCache(unittest.TestCase):
    def test_built_from_verified_files(self):
        with tempfile.TemporaryDirectory() as d:
            cache = fetch.Cache(os.path.join(d, "c"))
            for day in ("2024-01-02", "2024-01-03"):
                fx.put_file(cache, "bse-bhavcopy-isin", day, fx.bse_legacy(day, [
                    ("590901", "SYNBEES", "INF000SYN011", 100.0, 99.0, 10, 1000.0)]))
            data = pit.from_cache(cache, "BSE")
            v = data.view("2024-01-03")
            self.assertEqual(v.sessions(), ["2024-01-02", "2024-01-03"])
            self.assertEqual(v.bar("590901")["close"], 100.0)
            with self.assertRaises(pit.LookAheadError):
                data.view("2024-01-02").bar("590901", "2024-01-03")


if __name__ == "__main__":
    unittest.main()
