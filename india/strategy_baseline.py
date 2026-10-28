#!/usr/bin/env python3
"""Baseline strategy for the India study (frozen; see india/PROTOCOL.md and india/protocol.json).

Equal weight across every ETF the point-in-time universe marks eligible on
the decision date, keys in sorted order, each weight 1/n rounded down to six
decimals; no eligible ETF means all cash. It uses no prices, no lookback and
no parameter, so nothing in it can be fitted to outcomes. It is a measuring
stick for what a Rs 3,000 account loses to costs, whole-unit rounding and
cash drag against the index, not a search for an edge.

Changing this file changes its SHA-256, which protocol.json freezes: the
runner refuses a run whose strategy does not match the frozen hash.
"""

import decimal


class EqualWeightEligible(object):
    NAME = "baseline-equal-weight-eligible"
    VERSION = "1"
    LOOKBACK = 1

    def decide(self, inp):
        keys = sorted(inp.eligible)
        if not keys:
            return {}
        w = (decimal.Decimal(1) / len(keys)).quantize(decimal.Decimal("0.000001"), rounding=decimal.ROUND_DOWN)
        return {k: str(w) for k in keys}
