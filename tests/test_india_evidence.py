import json
import os
import tempfile
import unittest

import _india_fixtures as fx  # noqa: F401  (puts india/ on the path)
import evidence  # noqa: E402
import fetch  # noqa: E402

URL = "https://example.org/notice.pdf"


class Evidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "c"))

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, data, name="n.pdf"):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_import_records_provenance_and_checksum(self):
        p = self.file(b"%PDF-1.4 synthetic notice")
        rec = evidence.import_document(p, self.cache, URL, "2026-10-28", "Synthetic notice", "Synthetic AMC",
                                       mirror_of="Synthetic AMC filing")
        self.assertEqual(rec["sha256"], fetch.sha256(b"%PDF-1.4 synthetic notice"))
        self.assertEqual((rec["source_url"], rec["retrieved_on"], rec["mirror_of"]),
                         (URL, "2026-10-28", "Synthetic AMC filing"))
        self.assertEqual(len(evidence.documents(self.cache)), 1)

    def test_bad_imports_are_refused(self):
        good = self.file(b"%PDF-1.4 x")
        for kw, why in ((dict(url="http://x"), "https"), (dict(retrieved_on="28/10/2026"), "YYYY-MM-DD"),
                        (dict(title=""), "title")):
            args = dict(url=URL, retrieved_on="2026-10-28", title="t", publisher="p")
            args.update(kw)
            with self.assertRaisesRegex(evidence.EvidenceError, why):
                evidence.import_document(good, self.cache, **args)
        with self.assertRaisesRegex(evidence.EvidenceError, "web page"):
            evidence.import_document(self.file(b"<!DOCTYPE html><html></html>"), self.cache, URL, "2026-10-28", "t", "p")
        with self.assertRaisesRegex(evidence.EvidenceError, "does not start with %PDF-"):
            evidence.import_document(self.file(b"Access Denied", "x.pdf"), self.cache, URL, "2026-10-28", "t", "p")
        with self.assertRaisesRegex(evidence.EvidenceError, "refusing"):
            evidence.import_document(self.file(b""), self.cache, URL, "2026-10-28", "t", "p")
        self.assertEqual(self.cache.records(), [])

    def test_check_finds_cited_documents_missing_or_changed(self):
        rec = evidence.import_document(self.file(b"%PDF-1.4 a"), self.cache, URL, "2026-10-28", "t", "p")
        cites = os.path.join(self.tmp.name, "cites.json")
        with open(cites, "w") as fh:
            json.dump({"actions": [{"documents": [{"sha256": rec["sha256"]}, {"sha256": "1" * 64}]}]}, fh)
        bad = evidence.check(self.cache, [cites])
        self.assertEqual([b[0] for b in bad], ["1" * 64])
        with open(self.cache._blob(rec["sha256"]), "wb") as fh:
            fh.write(b"changed")
        self.assertEqual(len(evidence.check(self.cache, [cites])), 2)
        with open(cites, "w") as fh:
            json.dump({"x": {"sha256": "nothex"}}, fh)
        with self.assertRaisesRegex(evidence.EvidenceError, "malformed"):
            evidence.cited([cites])


if __name__ == "__main__":
    unittest.main()
