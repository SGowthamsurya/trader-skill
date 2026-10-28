"""The frozen baseline protocol and strategy. Synthetic inputs only for the strategy rule."""

import hashlib
import json
import os
import unittest

import _india_fixtures as fx
import signals as S  # noqa: E402

STRAT = os.path.join(fx.INDIA, "strategy_baseline.py")


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Frozen(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(fx.INDIA, "protocol.json")) as fh:
            self.p = json.load(fh)

    def test_the_strategy_and_criteria_match_their_frozen_hashes(self):
        self.assertEqual(sha(STRAT), self.p["strategy"]["sha256"])
        self.assertEqual(sha(os.path.join(fx.INDIA, "etf_criteria.json")), self.p["criteria"]["sha256"])

    def test_the_protocol_keeps_the_approved_assumptions(self):
        self.assertEqual((self.p["capital_inr"], self.p["venue"]), (3000, "BSE"))
        self.assertEqual(self.p["execution"], {"liquidity_cap": "ex-ante", "on_unconfirmed_closure": "unfilled",
                                               "on_insufficient_cash": "reject", "orders": "day orders, never retried"})
        self.assertEqual(self.p["windows"]["evaluation"], {"start": "2022-01-01", "end": "2026-10-27"})


class Rule(unittest.TestCase):
    class Inp(object):
        def __init__(self, keys):
            self.eligible = tuple(keys)

    def test_equal_weight_rounded_down_and_cash_when_nothing_is_eligible(self):
        cls = S.load_strategy(STRAT, "EqualWeightEligible")
        d = cls().decide
        self.assertEqual(d(self.Inp([])), {})
        self.assertEqual(d(self.Inp(["B", "A"])), {"A": "0.500000", "B": "0.500000"})
        self.assertEqual(d(self.Inp(["A", "B", "C"])), {"A": "0.333333", "B": "0.333333", "C": "0.333333"})
        self.assertEqual((cls.NAME, cls.VERSION, cls.LOOKBACK), ("baseline-equal-weight-eligible", "1", 1))


if __name__ == "__main__":
    unittest.main()
