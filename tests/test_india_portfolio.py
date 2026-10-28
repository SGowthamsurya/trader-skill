"""Portfolio accounting mechanics. SYNTHETIC DATA ONLY: every price and amount is made up."""

import unittest

import _india_fixtures as fx  # noqa: F401  (puts india/ on the path)
import portfolio as P  # noqa: E402
from test_india_universe import etf_row, market

CAP = 300000   # Rs 3,000 in paise


class Money(unittest.TestCase):
    def test_prices_become_exact_paise(self):
        self.assertEqual([P.to_paise(x) for x in (259.49, 0.01, 1292.95, "100", 3000)], [25949, 1, 129295, 10000, 300000])

    def test_amounts_that_are_not_whole_paise_are_refused_not_rounded(self):
        for bad in (0.005, 1.2345, None, float("nan"), True):
            with self.assertRaises(P.AccountingError, msg=repr(bad)):
                P.to_paise(bad)

    def test_display(self):
        self.assertEqual((P.rupees(300000), P.rupees(-5), P.rupees(25949)), ("3000.00", "-0.05", "259.49"))


class Accounting(unittest.TestCase):
    def test_a_buy_and_a_sell_reconcile_exactly(self):
        p = P.Portfolio(CAP)
        p.buy("2024-01-02", "K", 11, 25949, 0, 1)
        self.assertEqual((p.cash, p.holdings), (CAP - 11 * 25949, {"K": 11}))
        p.sell("2024-01-03", "K", 4, 26000, 0, 2)
        self.assertEqual((p.cash, sum(x for _, x in p.unsettled), p.holdings), (CAP - 11 * 25949, 104000, {"K": 7}))
        self.assertTrue(p.reconcile())

    def test_whole_units_only_and_no_short_sales(self):
        p = P.Portfolio(CAP)
        for bad in (0, -1, 1.5, "2"):
            with self.assertRaises(P.AccountingError):
                p.buy("2024-01-02", "K", bad, 100, 0, 1)
        p.buy("2024-01-02", "K", 2, 100, 0, 1)
        with self.assertRaisesRegex(P.AccountingError, "holding 2"):
            p.sell("2024-01-03", "K", 3, 100, 0, 2)

    def test_insufficient_settled_cash_is_refused(self):
        p = P.Portfolio(CAP)
        with self.assertRaisesRegex(P.AccountingError, "settled cash"):
            p.buy("2024-01-02", "K", 12, 25949, 0, 1)          # 3,113.88 > 3,000.00

    def test_sale_proceeds_settle_only_on_a_later_session(self):
        p = P.Portfolio(CAP)
        p.buy("2024-01-02", "K", 10, 30000, 0, 1)
        p.sell("2024-01-03", "K", 10, 30000, 0, 2)
        self.assertIsNone(p.settle("2024-01-03"))                  # not the same day
        with self.assertRaises(P.AccountingError):
            p.buy("2024-01-03", "K", 1, 30000, 0, 3)
        self.assertEqual(p.settle("2024-01-04")["cash_paise"], 300000)
        self.assertEqual(p.cash, CAP)
        self.assertTrue(p.reconcile())

    def test_known_fees_reduce_cash_unknown_fees_are_counted_not_zeroed(self):
        p = P.Portfolio(CAP)
        p.buy("2024-01-02", "K", 10, 20000, 1500, 1)               # synthetic fee
        p.buy("2024-01-02", "K", 1, 20000, None, 2)                # unresolved fee
        self.assertEqual((p.cash, p.fees_known, p.fees_unknown), (CAP - 201500 - 20000, 1500, 1))
        self.assertTrue(p.reconcile())

    def test_reconciliation_catches_a_state_that_drifts_from_the_journal(self):
        p = P.Portfolio(CAP)
        p.buy("2024-01-02", "K", 1, 100, 0, 1)
        p.cash += 1
        with self.assertRaisesRegex(P.AccountingError, "disagree"):
            p.reconcile()

    def test_money_moves_only_through_the_accounting_methods(self):
        with self.assertRaises(P.AccountingError):
            P.Portfolio(CAP).note({"type": "buy"})

    def test_the_journal_digest_is_reproducible(self):
        def run():
            p = P.Portfolio(CAP, {"run": "synthetic"})
            p.buy("2024-01-02", "K", 3, 10000, 0, 1)
            p.note({"type": "order", "key": "K"})
            return p.digest()
        self.assertEqual(run(), run())


class Valuation(unittest.TestCase):
    def data(self):
        return market(lambda d: {"590901": etf_row(d, close=100.0 if d != "2024-01-10" else None)},
                      gaps=("2024-01-15",))

    def test_value_marks_at_the_close_or_flags_a_stale_mark(self):
        p = P.Portfolio(CAP)
        p.buy("2024-01-02", "590901", 10, 10000, 0, 1)
        total, rows, flags = p.value(self.data().view("2024-01-09"))
        self.assertEqual((total, flags), (CAP, []))
        total, rows, flags = p.value(self.data().view("2024-01-10"))
        self.assertEqual(total, CAP)
        self.assertTrue(any("stale" in f for f in flags))

    def test_a_holding_without_any_mark_makes_the_total_unknown_not_zero(self):
        p = P.Portfolio(CAP)
        p.buy("2024-01-02", "NEVER", 1, 100, 0, 1)
        total, rows, flags = p.value(self.data().view("2024-01-09"))
        self.assertIsNone(total)
        self.assertIn("NEVER has no mark: total unknown", flags)

    def test_unresolved_fees_are_flagged_in_the_valuation(self):
        p = P.Portfolio(CAP)
        p.buy("2024-01-02", "590901", 1, 10000, None, 1)
        self.assertIn("1 fill(s) with unresolved fees: cash excludes them", p.value(self.data().view("2024-01-09"))[2])


if __name__ == "__main__":
    unittest.main()
