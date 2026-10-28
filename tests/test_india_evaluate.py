"""Metrics on SYNTHETIC journals. No figure here is a trading or performance result."""

import decimal
import unittest

import _india_fixtures  # noqa: F401
import evaluate as E  # noqa: E402

D = decimal.Decimal


def journal(marks, fills=()):
    j = [{"type": "start", "capital_paise": 300000}]
    for d, key, units in fills:
        j.append({"type": "buy" if units > 0 else "sell", "date": d, "key": key, "units": abs(units)})
    for d, v in marks:
        j.append({"type": "mark", "date": d, "value_paise": v, "flags": [], "gaps_since_previous_mark": []})
    j.sort(key=lambda e: e.get("date", ""))
    return j


def result(orders=()):
    return {"orders": list(orders), "cash_paise": 0, "unsettled_paise": 0, "holdings": {}, "fees_known_paise": 0}


class Metrics(unittest.TestCase):
    def test_drawdown_counts_from_the_start_capital(self):
        dd, a, b = E.drawdown([("s", 300000), ("1", 330000), ("2", 297000), ("3", 340000)])
        self.assertEqual((dd, a, b), (D("0.1"), "1", "2"))

    def test_short_windows_are_not_annualised_and_volatility_needs_24_returns(self):
        m = E.evaluate(result(), journal([("2024-01-01", 300000), ("2024-06-03", 330000)]),
                       ["2024-01-01"], "SYNTHETIC")
        self.assertEqual(m["return"], "0.100000")
        self.assertIsNone(m["cagr"])
        self.assertIsNone(m["volatility_annualised"])
        self.assertEqual(E.cagr(D("0.21"), 730).quantize(D("0.0001")), D("0.1000"))
        self.assertIsNotNone(E.volatility([D("0.01"), D("-0.01")] * 12))

    def test_unresolved_fees_are_labelled(self):
        o = {"status": "filled", "side": "buy", "filled": 1, "price_paise": 100, "cost_status": "unresolved",
             "cost_components": [{"component": "brokerage", "status": "unresolved"}]}
        m = E.evaluate(result([o]), journal([("2024-01-01", 300000)]), ["2024-01-01"], "SYNTHETIC")
        self.assertIn("unresolved fees", m["return_label"])
        self.assertEqual(m["fees_by_component"]["brokerage"]["unresolved_fills"], 1)

    def test_invested_share_replays_fills(self):
        j = journal([("2024-01-01", 300000), ("2024-01-31", 300000)],
                    [("2024-01-11", "K", 3), ("2024-01-21", "K", -3)])
        self.assertEqual(E.invested_share(j, "2024-01-01", "2024-01-31"), D(10) / D(30))

    def test_benchmarks_only_over_the_covered_window(self):
        series = [("2023-12-29", 100.0), ("2024-03-01", 90.0), ("2024-06-03", 110.0)]
        b = E.benchmark(series, "2024-01-01", "2024-06-03")
        self.assertEqual((b["return"], b["max_drawdown"]), ("0.100000", "0.100000"))
        self.assertIsNone(E.benchmark(series, "2023-01-01", "2024-06-03")["return"])
        m = E.evaluate(result(), journal([("2024-01-01", 300000), ("2024-06-03", 330000)]), ["2024-01-01"],
                       "SYNTHETIC", {"SYN TRI": series})
        self.assertEqual(m["benchmarks"]["SYN TRI"]["relative_return"], "0.000000")
        self.assertEqual(m["benchmarks"]["uninvested cash"]["return"], "0.000000")


if __name__ == "__main__":
    unittest.main()
