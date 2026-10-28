"""Readiness gate. Synthetic inputs only; nothing here is evidence about real data."""

import json
import os
import shutil
import tempfile
import unittest

import _india_fixtures as fx
import fetch  # noqa: E402
import readiness as R  # noqa: E402


def chk(status, mandatory=True, id_="x"):
    return R.Check(id_, "data", status, mandatory, "synthetic")


class Classification(unittest.TestCase):
    def test_ready_only_when_every_mandatory_check_passes_or_is_not_applicable(self):
        self.assertEqual(R.classify([chk("PASS"), chk("NOT_APPLICABLE", id_="y")]), "READY")
        self.assertEqual(R.classify([chk("PASS"), chk("UNRESOLVED", False, "y")]), "READY")
        for bad in ("BLOCKED", "UNRESOLVED"):
            self.assertEqual(R.classify([chk("PASS"), chk(bad, id_="y")]), "BLOCKED")
        self.assertEqual(R.classify([]), "BLOCKED")

    def test_statuses_categories_and_ids_are_validated(self):
        with self.assertRaises(R.ReadinessError):
            chk("OK")
        with self.assertRaises(R.ReadinessError):
            R.Check("x", "vibes", "PASS", True, "")
        with self.assertRaises(R.ReadinessError):
            R.Report([chk("PASS"), chk("PASS")], {})

    def test_a_blocked_report_says_so_and_its_digest_is_stable(self):
        rep = R.Report([chk("BLOCKED")], {"venue": "BSE"})
        self.assertIn("BLOCKED - NO VALID PERFORMANCE RESULT", rep.markdown())
        self.assertEqual(rep.digest(), R.Report([chk("BLOCKED")], {"venue": "BSE"}).digest())
        self.assertEqual(json.loads(rep.to_json())["digest"], rep.digest())
        self.assertEqual([c.id for c in rep.blockers()], ["x"])


class Checks(unittest.TestCase):
    def test_a_strategy_that_differs_from_its_frozen_hash_blocks(self):
        with open(R.PROTOCOL) as fh:
            p = json.load(fh)
        self.assertEqual(R.check_protocol(p).status, "PASS")
        with tempfile.TemporaryDirectory() as tmp:
            for f in (p["strategy"]["file"], p["criteria"]["file"]):
                shutil.copy(os.path.join(fx.INDIA, f), tmp)
            with open(os.path.join(tmp, p["strategy"]["file"]), "a") as fh:
                fh.write("# tuned\n")
            self.assertEqual(R.check_protocol(p, tmp).status, "BLOCKED")

    def test_dirty_or_unknown_code_blocks(self):
        self.assertEqual(R.check_git("a" * 40, False).status, "PASS")
        self.assertEqual(R.check_git("a" * 40, True).status, "BLOCKED")
        self.assertEqual(R.check_git(None, None).status, "BLOCKED")

    def test_synthetic_market_data_can_never_pass(self):
        self.assertEqual(R.check_provenance("exchange-cache").status, "PASS")
        for p in ("SYNTHETIC", "unspecified"):
            self.assertEqual(R.check_provenance(p).status, "BLOCKED")
        self.assertEqual(R.check_provenance(None).status, "NOT_APPLICABLE")

    def test_category_facts_must_be_knowable_by_the_evaluation_start(self):
        late = {"INF000SYN011": [("nifty50", "2026-10-28", "syn")]}
        early = {"INF000SYN011": [("nifty50", "2021-06-01", "syn")]}
        self.assertEqual(R.check_category_evidence(late, "2022-01-01").status, "BLOCKED")
        self.assertEqual(R.check_category_evidence(early, "2022-01-01").status, "PASS")
        self.assertEqual(R.check_category_evidence({}, "2022-01-01").status, "BLOCKED")

    def test_price_coverage_blocks_on_any_unresolved_weekday(self):
        ok = {"2024-01-01": "valid-file", "2024-01-02": "documented-gap", "2024-01-03": "refused-all-layouts"}
        self.assertEqual(R.check_price_coverage(ok, "a", "b").status, "PASS")
        for bad in ("unresolved", "invalid-undocumented"):
            self.assertEqual(R.check_price_coverage(dict(ok, **{"2024-01-04": bad}), "a", "b").status, "BLOCKED")

    def test_missing_official_calendar_years_or_unconfirmed_closures_block(self):
        self.assertEqual(R.check_official_calendar(["2024-01-26", "2025-01-26"], ["2024-01-26"],
                                                   "2024-01-01", "2025-12-31").status, "PASS")
        self.assertEqual(R.check_official_calendar(["2025-01-26"], [], "2024-01-01", "2025-12-31").status, "BLOCKED")
        self.assertEqual(R.check_official_calendar(["2024-01-26", "2025-01-26"], ["2024-03-01"],
                                                   "2024-01-01", "2025-12-31").status, "BLOCKED")

    def test_the_committed_cost_schedule_does_not_cover_the_window(self):
        import costs
        gaps = costs.Schedule.load().coverage("2022-01-01", "2026-10-27", "BSE", "etf")
        self.assertTrue(gaps)
        self.assertEqual(R.check_costs(gaps, "2022-01-01", "2026-10-27").status, "BLOCKED")
        self.assertEqual(R.check_costs([], "a", "b").status, "PASS")

    def test_evidence_criteria_need_a_market_qualified_candidate_that_passes(self):
        def res(sym, verdict, **c):
            base = {k: ("pass", "") for k in R.MARKET_CRITERIA + R.EVIDENCE_CRITERIA}
            base.update(c)
            return {"candidate": {"symbol": sym}, "verdict": verdict, "criteria": base}
        rs = [res("SYNA", "INSUFFICIENT DATA", distributions=("unresolved", "no review")),
              res("SYNB", "NOT ELIGIBLE", liquidity=("fail", "thin"))]
        got = {c.id: c.status for c in R.check_evidence_criteria(rs)}
        self.assertEqual(got, {"identity-evidence": "PASS", "corporate-evidence": "PASS",
                               "distributions-evidence": "BLOCKED", "eligible-candidates": "BLOCKED"})
        got = {c.id: c.status for c in R.check_evidence_criteria([res("SYNA", "ELIGIBLE")])}
        self.assertTrue(all(s == "PASS" for s in got.values()))

    def test_benchmarks_and_execution(self):
        self.assertEqual(R.check_tri("BLOCKED", "no file").status, "BLOCKED")
        info = {"first": "2018-01-01", "last": "2026-10-27", "rows": 9}
        self.assertEqual(R.check_tri("AVAILABLE", info, []).status, "PASS")
        self.assertEqual(R.check_tri("AVAILABLE", info, ["ends early"]).status, "BLOCKED")
        self.assertEqual({c.id: c.status for c in R.check_execution({"execution": {"liquidity_cap": "ex-post"}})},
                         {"liquidity-cap": "BLOCKED", "price-limits": "UNRESOLVED"})
        self.assertEqual(R.check_ledger(None).status, "NOT_APPLICABLE")
        self.assertEqual(R.check_ledger({"sealed": True, "anchor_matches": False, "head": "0" * 64}).status, "BLOCKED")


class Assess(unittest.TestCase):
    def test_an_empty_cache_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = R.assess(fetch.Cache(os.path.join(tmp, "none")), git=("a" * 40, False))
        self.assertEqual(rep.classification, "BLOCKED")
        self.assertEqual({c.id: c.status for c in rep.checks}["cache-inventory"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
