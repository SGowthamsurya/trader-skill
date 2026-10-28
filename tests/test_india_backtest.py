"""Runner. Mechanics runs use SYNTHETIC data and costs; nothing here is a performance result."""

import os
import tempfile
import unittest

import _india_fixtures as fx
import backtest as BT  # noqa: E402
import readiness as R  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def out(self, name):
        return os.path.join(self.tmp.name, name)


class RealModeRefuses(Base):
    def test_a_blocked_gate_refuses_before_anything_is_written(self):
        rep = R.Report([R.Check("cost-coverage", "costs-taxes", "BLOCKED", True, "synthetic")], {})
        with self.assertRaises(BT.Refused) as e:
            BT.run_real(None, self.out("real"), report=rep)
        self.assertIn(BT.BLOCKED, str(e.exception))
        self.assertFalse(os.path.exists(self.out("real")))

    def test_synthetic_data_is_refused_in_real_mode_even_with_a_ready_report(self):
        rep = R.Report([R.Check("x", "data", "PASS", True, "synthetic")], {})
        with self.assertRaises(BT.Refused):
            BT._real(None, BT.synthetic_market(), {}, R.PROTOCOL, rep, self.out("real"), None, None, None)
        self.assertFalse(os.path.exists(self.out("real")))


class Mechanics(Base):
    def test_end_to_end_labelled_and_deterministic(self):
        a, ma = BT.run_mechanics(self.out("a"))
        b, mb = BT.run_mechanics(self.out("b"))
        self.assertEqual(ma["output_digest"], mb["output_digest"])
        self.assertEqual(ma["ledger_head"], mb["ledger_head"])
        for name, res in a["results"].items():
            self.assertTrue(res["label"].startswith(BT.MECHANICS), name)
        h = a["results"]["headline"]
        self.assertEqual(h["start_value_paise"], 300000)
        self.assertGreater(h["orders"].get("buy filled", 0), 0)
        self.assertEqual(sorted(a["results"]), sorted(["headline"] + ["sensitivity: " + s["name"] for s in
                                                                      R.load_protocol()["sensitivity"]]))
        self.assertEqual(ma["mode"], "mechanics")
        with open(os.path.join(self.out("a"), "seal_anchor.txt")) as fh:
            self.assertEqual(fh.read().strip(), ma["ledger_head"])

    def test_mechanics_never_runs_on_cache_data(self):
        data = BT.synthetic_market()
        data.provenance = "exchange-cache"
        with self.assertRaises(BT.Refused):
            BT.run_mechanics(self.out("m"), data=data)

    def test_decision_dates_are_the_first_tradable_session_of_each_month(self):
        import calendar_in
        import pit
        days = list(fx.weekdays("2090-01-01", "2090-03-10"))
        files = [d for d in days if d not in ("2090-02-01",)]
        cal = calendar_in.Calendar("BSE", {d: "s" for d in files}, [])
        data = pit.MarketData(cal, {d: {} for d in files}, "BSE", provenance="SYNTHETIC")
        self.assertEqual(BT.first_sessions(data, "2090-01-01", "2090-03-31"), ["2090-01-02", "2090-02-02",
                                                                              "2090-03-01"])


if __name__ == "__main__":
    unittest.main()
