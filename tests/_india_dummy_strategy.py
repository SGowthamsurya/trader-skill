"""MECHANICS TEST ONLY. This is not a trading strategy and must never be used as one.

It exists to exercise the strategy interface and the signal ledger: it gives an equal
weight to the first two eligible keys in sorted order, using no prices at all."""


class DummyEqualWeight(object):
    NAME = "dummy-equal-weight-MECHANICS-TEST-ONLY"
    VERSION = "0"
    LOOKBACK = 3

    def decide(self, inp):
        keys = sorted(inp.eligible)[:2]
        return {k: "0.5" for k in keys}


class PeeksPastLookback(DummyEqualWeight):
    """MECHANICS TEST ONLY: tries to read data it was not given."""

    def decide(self, inp):
        return {k: str(len(inp.closes(k))) for k in inp.eligible[:0]} or {"NOT-ELIGIBLE": "0.1"}


class Overweight(DummyEqualWeight):
    """MECHANICS TEST ONLY: asks for more than the whole capital."""

    def decide(self, inp):
        return {k: "0.9" for k in inp.eligible}
