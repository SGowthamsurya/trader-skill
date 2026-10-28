import json
import os
import tempfile
import unittest

import _india_fixtures as fx  # noqa: F401  (puts india/ on the path)
import calendar_in as cal_mod  # noqa: E402
import execution  # noqa: E402
import marks  # noqa: E402

GAP_SHA = "a" * 64
BOTH = ("bse-bhavcopy-isin", "bse-bhavcopy-udiff")


def calendar():
    """Mon 2024-01-01 .. Fri 2024-01-12; 2024-01-08 is a documented gap, 2024-01-10 a no-file day."""
    days = [d for d in fx.weekdays("2024-01-01", "2024-01-12") if d not in ("2024-01-08", "2024-01-10")]
    return cal_mod.Calendar("BSE", {d: "sha" for d in days}, [("2024-01-10", s, "not-data") for s in BOTH],
                            invalid=[("2024-01-08", GAP_SHA)], gaps={"2024-01-08": GAP_SHA})


CLOSES = {"2024-01-02": 100.0, "2024-01-03": 101.0, "2024-01-04": 102.0, "2024-01-05": 103.0,
          "2024-01-09": 110.0, "2024-01-11": 111.0, "2024-01-12": 112.0}


class Gaps(unittest.TestCase):
    def test_only_a_documented_checksum_makes_a_gap(self):
        c = calendar()
        self.assertEqual(c.status("2024-01-08"), "gap")
        other = cal_mod.Calendar("BSE", {}, [], invalid=[("2024-01-08", "b" * 64)], gaps={"2024-01-08": GAP_SHA})
        self.assertEqual(other.status("2024-01-08"), "invalid")
        self.assertFalse(other.coverage("2024-01-08", "2024-01-08")["complete_weekdays"])

    def test_a_gap_is_accounted_for_but_never_tradable(self):
        c = calendar()
        cov = c.coverage("2024-01-01", "2024-01-12")
        self.assertTrue(cov["complete_weekdays"])
        self.assertEqual(cov["documented_gaps"], ["2024-01-08"])
        self.assertFalse(c.tradable("2024-01-08"))
        self.assertTrue(c.tradable("2024-01-09"))

    def test_sessions_either_side_of_a_gap_are_not_adjacent(self):
        self.assertEqual(calendar().adjacent("2024-01-05", "2024-01-09"), (False, []))

    def test_the_gaps_file_is_validated(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g.json")
            good = {"venue": "BSE", "date": "2024-01-08", "sha256": GAP_SHA, "reason": "x", "status": "unresolved",
                    "handling": "excluded"}
            for bad, why in ((dict(good, sha256="abc"), "full sha256"), (dict(good, status="resolved"), "unresolved"),
                             (dict(good, handling="interpolate"), "excluded"), ({"venue": "BSE"}, "has no date")):
                with open(p, "w") as fh:
                    json.dump({"gaps": [bad]}, fh)
                with self.assertRaisesRegex(cal_mod.CalendarError, why):
                    cal_mod.read_gaps(p)
            with open(p, "w") as fh:
                json.dump({"gaps": [good, good]}, fh)
            with self.assertRaisesRegex(cal_mod.CalendarError, "twice"):
                cal_mod.read_gaps(p)

    def test_the_shipped_gaps_are_the_two_corrupt_bse_files(self):
        self.assertEqual(sorted(d.isoformat() for d in cal_mod.read_gaps(exchange="BSE")), ["2022-02-07", "2022-07-15"])


class Marks(unittest.TestCase):
    def test_a_valid_session_marks_at_its_close(self):
        self.assertEqual(marks.mark(CLOSES, "2024-01-09", calendar()), (110.0, []))

    def test_a_gap_carries_the_last_valid_close_and_never_a_later_one(self):
        price, flags = marks.mark(CLOSES, "2024-01-08", calendar())
        self.assertEqual(price, 103.0)                    # Friday's close, not Tuesday's 110
        self.assertIn("documented data gap", flags[0])
        self.assertTrue(flags[0].startswith("stale"))

    def test_no_file_days_and_untraded_sessions_are_flagged_too(self):
        self.assertIn("no file", marks.mark(CLOSES, "2024-01-10", calendar())[1][0])
        self.assertIn("no trade in this instrument", marks.mark(CLOSES, "2024-01-01", calendar())[1][0])

    def test_no_earlier_price_means_no_mark(self):
        price, flags = marks.mark(CLOSES, "2024-01-01", calendar())
        self.assertIsNone(price)
        self.assertTrue(flags[0].startswith("no-mark"))

    def test_a_return_across_a_gap_is_computed_from_valid_closes_and_flagged(self):
        r, flags = marks.holding_return(CLOSES, "2024-01-05", "2024-01-09", calendar())
        self.assertAlmostEqual(r, 110.0 / 103.0 - 1)
        self.assertIn("spans-gap: 2024-01-08", flags)

    def test_a_return_ending_on_a_gap_is_stale_and_spans_it(self):
        r, flags = marks.holding_return(CLOSES, "2024-01-02", "2024-01-08", calendar())
        self.assertAlmostEqual(r, 103.0 / 100.0 - 1)
        self.assertIn("stale-endpoint", flags)
        self.assertIn("spans-gap: 2024-01-08", flags)

    def test_a_clean_return_has_no_flags(self):
        self.assertEqual(marks.holding_return(CLOSES, "2024-01-02", "2024-01-05", calendar())[1], [])

    def test_a_return_starting_on_the_gap_does_not_count_it_as_spanned(self):
        self.assertNotIn("spans-gap: 2024-01-08",
                         marks.holding_return(CLOSES, "2024-01-08", "2024-01-09", calendar())[1])


class Execution(unittest.TestCase):
    ROW = {"value": 100000.0}

    def test_within_five_percent_fills(self):
        self.assertTrue(execution.check(5000, "2024-01-09", self.ROW, calendar())[0])

    def test_above_five_percent_is_unfilled(self):
        ok, why = execution.check(5001, "2024-01-09", self.ROW, calendar())
        self.assertFalse(ok)
        self.assertIn("5.0% of the day's traded value", why)

    def test_no_trade_on_a_gap_or_no_file_day(self):
        for day, status in (("2024-01-08", "gap"), ("2024-01-10", "no-file"), ("2024-01-13", "unobserved")):
            ok, why = execution.check(100, day, self.ROW, calendar())
            self.assertFalse(ok)
            self.assertIn(status, why)

    def test_no_verified_trade_in_the_etf_means_no_fill(self):
        for row in (None, {}, {"value": 0.0}, {"value": None}, {"value": 1e9, "identity_only": True}):
            self.assertFalse(execution.check(100, "2024-01-09", row, calendar())[0], row)

    def test_bad_orders(self):
        self.assertFalse(execution.check(0, "2024-01-09", self.ROW, calendar())[0])

    def test_fillable_share(self):
        c = calendar()
        rows = {"2024-01-09": {"K": {"value": 100000.0}}, "2024-01-11": {"K": {"value": 40000.0}}}
        self.assertEqual(execution.fillable_share(3000, ["2024-01-09", "2024-01-11"], rows, "K", c), 0.5)
        self.assertIsNone(execution.fillable_share(3000, [], rows, "K", c))


if __name__ == "__main__":
    unittest.main()
