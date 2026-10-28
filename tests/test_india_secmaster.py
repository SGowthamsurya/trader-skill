import json
import os
import tempfile
import unittest

import _india_fixtures as fx
import bhav  # noqa: E402
import secmaster as sm  # noqa: E402

A, B, C = "INF000SYN011", "INF000SYN022", "INF000SYN033"
URL = "https://example.org/notice"


def files(spec):
    """spec: {day: [(code, name, isin)]} -> [(sha, rows)] of parsed synthetic BSE files."""
    out = []
    for day, rows in sorted(spec.items()):
        data = fx.bse_legacy(day, [(c, n, i, 100.0, 100.0, 10, 1000.0) for c, n, i in rows])
        out.append(("sha-" + day, bhav.parse(data, day)[1]))
    return out


def confirm(**kw):
    c = {"exchange": "BSE", "key": "590901", "from_isin": A, "to_isin": B, "effective_on": None,
         "verification": "verified", "source_url": URL, "retrieved_on": "2026-10-29"}
    c.update(kw)
    return c


class Changes(unittest.TestCase):
    def setUp(self):
        self.spec = {"2024-01-01": [("590901", "SYN BEES", A)], "2024-01-02": [("590901", "SYNBEES", A)],
                     "2024-01-05": [("590901", "SYNBEES", B)], "2024-01-08": [("590901", "SYNBEES", B)]}

    def test_symbol_and_isin_changes_carry_windows_and_evidence(self):
        m = sm.Master(files(self.spec))
        self.assertEqual(m.conflicts, [])
        sym, isin = m.events
        self.assertEqual((sym["type"], sym["status"], sym["from_symbol"], sym["to_symbol"]),
                         ("symbol-change", "observed", "SYN BEES", "SYNBEES"))
        self.assertEqual((isin["type"], isin["status"], isin["after"], isin["on_or_before"]),
                         ("isin-change", "proposed", "2024-01-02", "2024-01-05"))
        self.assertEqual(isin["evidence"], ["sha-2024-01-02", "sha-2024-01-05"])
        self.assertIsNone(isin["effective_on"])
        hist = m.history("BSE", "590901")
        self.assertEqual([(h["symbol"], h["isin"], h["first"], h["last"]) for h in hist],
                         [("SYN BEES", A, "2024-01-01", "2024-01-01"), ("SYNBEES", A, "2024-01-02", "2024-01-02"),
                          ("SYNBEES", B, "2024-01-05", "2024-01-08")])

    def test_an_isin_is_not_assumed_permanent_and_links_wait_for_confirmation(self):
        chain = sm.Master(files(self.spec)).chain(B)
        self.assertEqual(chain["isins"], [A, B])
        self.assertFalse(chain["confirmed"])
        m = sm.Master(files(self.spec), [confirm(effective_on="2024-01-04")])
        e = [x for x in m.events if x["type"] == "isin-change"][0]
        self.assertEqual((e["status"], e["effective_on"], e["confirmation"]), ("confirmed", "2024-01-04", URL))
        self.assertTrue(m.chain(B)["confirmed"])

    def test_a_corroborated_link_is_shown_but_never_confirmed(self):
        m = sm.Master(files(self.spec), [confirm(verification="corroborated", effective_on="2024-01-04")])
        e = [x for x in m.events if x["type"] == "isin-change"][0]
        self.assertEqual((e["status"], e["effective_on"], e["confirmation"]), ("corroborated", None, URL))
        self.assertFalse(m.chain(B)["confirmed"])

    def test_confirmations_that_do_not_fit_are_conflicts(self):
        m = sm.Master(files(self.spec), [confirm(effective_on="2024-01-02")])
        self.assertEqual(m.conflicts[0]["type"], "confirmation-outside-window")
        m = sm.Master(files(self.spec), [confirm(to_isin=C)])
        self.assertEqual(m.conflicts[0]["type"], "confirmation-unmatched")

    def test_a_shared_isin_links_listings_automatically(self):
        m = sm.Master(files({"2024-01-01": [("590901", "SYN", A)], "2024-01-02": [("590999", "SYNNEW", A)]}))
        self.assertEqual(m.chain(A)["listings"], [("BSE", "590901"), ("BSE", "590999")])
        self.assertTrue(m.chain(A)["confirmed"])


class Conflicts(unittest.TestCase):
    def test_one_code_with_two_isins_on_one_day(self):
        m = sm.Master(files({"2024-01-01": [("590901", "SYN", A), ("590901", "SYN", B)]}))
        self.assertTrue(any(c["type"] == "duplicate-mapping" and "two ISINs" in c["detail"] for c in m.conflicts))

    def test_one_isin_under_two_codes_on_one_day(self):
        m = sm.Master(files({"2024-01-01": [("590901", "SYN", A), ("590902", "SYN2", A)]}))
        self.assertTrue(any(c["type"] == "duplicate-mapping" and "two keys" in c["detail"] for c in m.conflicts))
        self.assertTrue(m.conflicts_for([A]))
        self.assertEqual(m.conflicts_for([C]), [])

    def test_an_isin_that_comes_back_overlaps(self):
        m = sm.Master(files({"2024-01-01": [("590901", "SYN", A)], "2024-01-02": [("590901", "SYN", B)],
                             "2024-01-03": [("590901", "SYN", A)]}))
        self.assertTrue(any(c["type"] == "overlapping-isins" for c in m.conflicts))

    def test_nse_series_of_one_symbol_are_not_duplicates(self):
        data = fx.nse_legacy("2024-01-01", [("SYN", "EQ", A, 1, 1, 1, 1), ("SYN", "BE", A, 1, 1, 1, 1)])
        self.assertEqual(sm.Master([("s", bhav.parse(data)[1])]).conflicts, [])
        data = fx.nse_legacy("2024-01-01", [("SYN", "EQ", A, 1, 1, 1, 1), ("SYN", "EQ", B, 1, 1, 1, 1)])
        self.assertTrue(sm.Master([("s", bhav.parse(data)[1])]).conflicts)


class ConfirmationFile(unittest.TestCase):
    def test_the_shipped_file_is_valid_and_claims_no_more_than_it_shows(self):
        cs = sm.read_confirmations()
        self.assertTrue(cs)
        for c in cs:
            self.assertTrue(c["documents"], c["key"])
        # no retrieved document states both Nippon ISINs, so neither link may be 'verified'
        nippon = {c["key"]: c["verification"] for c in cs if c["from_isin"].startswith("INF732E")}
        self.assertEqual(nippon, {"590103": "corroborated", "590106": "corroborated"})

    def test_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            for bad, why in ((confirm(source_url="http://x"), "https"), (confirm(retrieved_on=None), "retrieved_on"),
                             (confirm(effective_on="4 Jan 2024"), "YYYY-MM-DD"),
                             (confirm(verification="likely"), "verified or corroborated"),
                             ({k: v for k, v in confirm().items() if k != "verification"}, "verification")):
                with open(p, "w") as fh:
                    json.dump({"confirmations": [bad]}, fh)
                with self.assertRaisesRegex(sm.SecError, why):
                    sm.read_confirmations(p)


if __name__ == "__main__":
    unittest.main()
