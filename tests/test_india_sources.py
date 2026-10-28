import copy
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import ROOT

INDIA = os.path.join(ROOT, "india")
if INDIA not in sys.path:
    sys.path.insert(0, INDIA)
import sources as src  # noqa: E402

UPSTREAM = ["trader-copycat", "trader-news", "trader-budget", "trader-pelosi", "trader-analyst",
            "trader-screener", "trader-risk", "trader-broker", "trader-tax", "trader-alerts",
            "trader-rebalance"]


def registry():
    with open(src.DEFAULT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class Structure(unittest.TestCase):
    def test_india_is_not_a_skill_folder(self):
        # a SKILL.md here would make a twelfth skill and change the plugin
        self.assertFalse(os.path.exists(os.path.join(INDIA, "SKILL.md")))

    def test_gitignore_keeps_caches_and_market_data_out(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            lines = {l.strip() for l in fh}
        for want in ("__pycache__/", "*.pyc", "india/data/"):
            self.assertIn(want, lines)

    def test_upstream_skills_do_not_depend_on_india(self):
        for s in UPSTREAM:
            for f in os.listdir(os.path.join(ROOT, s)):
                if f.endswith((".py", ".md", ".json")):
                    with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                        self.assertNotIn("india/", fh.read(), "%s/%s" % (s, f))

    def test_no_raw_market_data_committed_under_india(self):
        for base, dirs, files in os.walk(INDIA):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "data")]
            for f in files:
                self.assertFalse(f.lower().endswith((".zip", ".csv")), os.path.join(base, f))

    def test_nothing_in_india_talks_to_a_broker(self):
        for f in os.listdir(INDIA):
            if f.endswith(".py"):
                with open(os.path.join(INDIA, f), encoding="utf-8") as fh:
                    text = fh.read().lower()
                for word in ("place_stock_order", "alpaca", "kite.trade", "api_key", "secret"):
                    self.assertNotIn(word, text, "%s mentions %s" % (f, word))


class Registry(unittest.TestCase):
    def test_the_shipped_registry_is_valid(self):
        self.assertEqual(src.validate(registry()), [])
        self.assertEqual(len(src.read()["sources"]), 26)

    def test_it_records_access_facts_not_rates(self):
        text = json.dumps(registry()).lower()
        self.assertNotRegex(text, r"\d+(\.\d+)?\s*%")
        self.assertNotIn("rate\":", text)

    def test_every_purpose_the_study_needs_has_a_source(self):
        have = {p for s in registry()["sources"] for p in s["purpose"]}
        for p in ("prices", "index", "tri", "nav", "corporate-actions", "calendar", "costs", "tax", "risk-free"):
            self.assertIn(p, have)

    def test_tri_is_manual_and_not_fetchable(self):
        doc = registry()
        tri = src.get(doc, "nifty50-tri-history")
        self.assertEqual(tri["status"], "manual")
        self.assertFalse(src.fetchable(doc, tri))

    def test_blocked_and_unverified_hosts_are_not_allowed(self):
        hosts = src.allowed_hosts(registry())
        for h in ("www.nseindia.com", "api.bseindia.com", "incometaxindia.gov.in", "nsdl.co.in",
                  "www.indiacode.nic.in", "www.hdfcfund.com", "egazette.gov.in"):
            self.assertNotIn(h, hosts)
        self.assertIn("nsearchives.nseindia.com", hosts)

    def test_validate_names_each_problem(self):
        doc = registry()
        bad = copy.deepcopy(doc)
        s = bad["sources"][0]
        s["status"] = "fine"
        s["url"] = "http://example.com/x"
        s["purpose"] = ["prices", "vibes"]
        s["authority"] = "tertiary"
        del s["evidence"]
        bad["sources"].append(copy.deepcopy(doc["sources"][1]))
        bad["policy"]["fetchable_status"] = ["ok", "blocked"]
        text = "\n".join(src.validate(bad))
        for want in ("status 'fine'", "url must be https", "unknown purpose 'vibes'", "authority 'tertiary'",
                     "evidence is missing", "duplicate id", "fetchable_status"):
            self.assertIn(want, text)

    def test_unverified_sources_carry_no_check_date(self):
        doc = registry()
        s = doc["sources"][0]
        s["status"], s["checked_at"] = "unverified", "2026-10-28"
        self.assertTrue(any("unverified" in p for p in src.validate(doc)))

    def test_read_refuses_an_invalid_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w") as fh:
                json.dump({"policy": {}, "sources": []}, fh)
            with self.assertRaises(src.SourceError):
                src.read(p)


class Render(unittest.TestCase):
    def setUp(self):
        self.doc = registry()

    def test_exchange_file_names(self):
        nse = src.get(self.doc, "nse-bhavcopy-legacy")
        self.assertEqual(src.render(nse, src.params_for(nse, "2018-01-02")),
                         "https://nsearchives.nseindia.com/content/historical/EQUITIES/2018/JAN/cm02JAN2018bhav.csv.zip")
        ud = src.get(self.doc, "nse-bhavcopy-udiff")
        self.assertTrue(src.render(ud, src.params_for(ud, "2026-10-27")).endswith("_20261027_F_0000.csv.zip"))
        bse = src.get(self.doc, "bse-bhavcopy-isin")
        self.assertTrue(src.render(bse, src.params_for(bse, "2018-01-02")).endswith("EQ_ISINCODE_020118.ZIP"))
        ni = src.get(self.doc, "niftyindices-close-all")
        self.assertTrue(src.render(ni, src.params_for(ni, "2026-10-27")).endswith("ind_close_all_27102026.csv"))

    def test_missing_and_extra_parameters_are_refused(self):
        nse = src.get(self.doc, "nse-bhavcopy-legacy")
        with self.assertRaisesRegex(src.SourceError, "needs"):
            src.render(nse, {"YYYY": "2018"})
        with self.assertRaisesRegex(src.SourceError, "does not take"):
            src.render(nse, dict(src.params_for(nse, "2018-01-02"), EXTRA="x"))

    def test_a_parameter_cannot_change_the_path_or_host(self):
        amfi = src.get(self.doc, "amfi-nav-history")
        for evil in ("../x", "01-Oct-2026&mf=1", "a/b", "x@evil.com", "", "a" * 40, "https://evil"):
            with self.assertRaises(src.SourceError, msg=evil):
                src.render(amfi, {"FROM": evil, "TO": "27-Oct-2026"})

    def test_bad_dates_are_refused(self):
        with self.assertRaises(src.SourceError):
            src.date_params("02/01/2018")

    def test_cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(src.main(["url", "nse-bhavcopy-legacy", "--date", "2012-01-02"]), 0)
        self.assertIn("cm02JAN2012bhav.csv.zip", out.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(src.main(["url", "no-such-source"]), 1)


if __name__ == "__main__":
    unittest.main()
