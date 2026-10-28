"""Benchmark, not the strategy under test: buy NIFTYBEES (BSE 590103) with the whole account on the first
decision date and hold it. The runner gives this strategy only the first decision date, so it never
rebalances. Cost-matched: its orders go through the same broker, costs and policies as the baseline."""


class BuyAndHoldNiftyBees(object):
    NAME = "benchmark-buy-and-hold-niftybees"
    VERSION = "1"
    LOOKBACK = 1
    KEY = "590103"

    def decide(self, inp):
        return {self.KEY: "1"} if self.KEY in inp.eligible else {}
