"""Cache inventory: checksums only. Synthetic cache contents in the mechanics tests."""

import json
import os
import re
import tempfile
import unittest

import _india_fixtures as fx
import calendar_in  # noqa: E402
import fetch  # noqa: E402
import inventory as I  # noqa: E402


class Mechanics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))
        self.ok = fx.put_file(self.cache, "bse-bhavcopy-isin", "2024-01-02", b"PK\x03\x04 synthetic file")
        fx.put_refusal(self.cache, "bse-bhavcopy-udiff", "2024-01-02")

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_lists_files_not_refusals_and_is_deterministic(self):
        a, b = I.build(self.cache), I.build(self.cache)
        self.assertEqual(I.dumps(a), I.dumps(b))
        self.assertEqual([(r["source"], r["date"], r["role"]) for r in a["records"]],
                         [("bse-bhavcopy-isin", "2024-01-02", "data")])
        json.loads(I.dumps(a))

    def test_check_finds_present_missing_and_corrupt_files(self):
        doc = I.build(self.cache)
        self.assertEqual(len(I.check(self.cache, doc)["present"]), 1)
        with open(self.cache._blob(self.ok["sha256"]), "wb") as fh:
            fh.write(b"changed")
        self.assertEqual(len(I.check(self.cache, doc)["corrupt"]), 1)
        empty = fetch.Cache(os.path.join(self.tmp.name, "recovered"))
        self.assertEqual(len(I.check(empty, doc)["missing"]), 1)

    def test_a_foreign_document_is_refused(self):
        with self.assertRaises(I.InventoryError):
            I.check(self.cache, {"schema": "other", "records": []})


class Committed(unittest.TestCase):
    def setUp(self):
        with open(I.INVENTORY, encoding="utf-8") as fh:
            self.doc = json.load(fh)

    def test_it_is_well_formed_and_holds_no_data(self):
        self.assertEqual(self.doc["schema"], I.SCHEMA)
        self.assertEqual(self.doc["count"], len(self.doc["records"]))
        for r in self.doc["records"]:
            self.assertRegex(r["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(r["role"], ("data", "corrupt-published-file", "imported-document"))
            self.assertLessEqual(set(r), {"source", "date", "sha256", "bytes", "role", "url", "title"})

    def test_it_covers_the_documented_gaps_and_every_cited_document(self):
        shas = {r["sha256"] for r in self.doc["records"]}
        for d, sha in calendar_in.read_gaps(exchange="BSE").items():
            self.assertIn(sha, shas, d)
        import evidence
        for sha in evidence.cited([os.path.join(fx.INDIA, p) for p in evidence.CITING]):
            self.assertIn(sha, shas)

    def test_it_holds_one_bse_file_per_collected_session(self):
        days = [r["date"] for r in self.doc["records"] if r["role"] == "data" and r["source"].startswith("bse-")]
        self.assertEqual(len(days), len(set(days)))
        self.assertGreaterEqual(len(days), 2174)
        self.assertTrue(all(re.match(r"^\d{4}-\d{2}-\d{2}$", d) for d in days))


if __name__ == "__main__":
    unittest.main()
