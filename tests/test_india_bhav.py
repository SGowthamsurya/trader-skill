import os
import tempfile
import unittest

import _india_fixtures as fx
import bhav  # noqa: E402
import fetch  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
