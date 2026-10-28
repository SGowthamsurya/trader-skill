import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import ROOT

INDIA = os.path.join(ROOT, "india")
if INDIA not in sys.path:
    sys.path.insert(0, INDIA)
import fetch  # noqa: E402
import tri  # noqa: E402

PAGE = "https://www.niftyindices.com/reports/historical-data"
# synthetic numbers, not index data
GOOD = ("Index Name,Date,Total Returns Index\n"
        "NIFTY 50,05 Jan 2018,1003.00\n"
        "NIFTY 50,04 Jan 2018,1002.00\n"
        "NIFTY 50,03 Jan 2018,1001.00\n"
        "NIFTY 50,02 Jan 2018,1000.00\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "cache"))

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, text, name="tri.csv", raw=None):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as fh:
            fh.write(raw if raw is not None else text.encode("utf-8"))
        return p


class Status(Base):
    def test_blocked_until_a_file_is_imported(self):
        st, why = tri.status(self.cache)
        self.assertEqual(st, "BLOCKED")
        self.assertIn("no NIFTY 50 TRI file", why)
        with self.assertRaisesRegex(tri.TriError, "BLOCKED"):
            tri.load_series(self.cache)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tri.main(["--cache", self.cache.root, "status"]), 1)

    def test_available_after_a_valid_import(self):
        p = self.file(GOOD)
        rec = tri.import_file(p, self.cache, PAGE, "2026-10-29")
        with open(p, "rb") as fh:
            self.assertEqual(rec["sha256"], fetch.sha256(fh.read()))
        self.assertEqual(rec["source_url"], PAGE)
        self.assertEqual(rec["retrieved_on"], "2026-10-29")
        st, got = tri.status(self.cache)
        self.assertEqual(st, "AVAILABLE")
        self.assertEqual(got["validation"]["rows"], 4)
        self.assertEqual(tri.load_series(self.cache)[0], ("2018-01-02", 1000.0))

    def test_a_tampered_import_is_blocked_again(self):
        rec = tri.import_file(self.file(GOOD), self.cache, PAGE, "2026-10-29")
        with open(self.cache._blob(rec["sha256"]), "wb") as fh:
            fh.write(b"Date,Total Returns Index\n02 Jan 2018,1\n")
        self.assertEqual(tri.status(self.cache)[0], "BLOCKED")

    def test_import_needs_provenance(self):
        p = self.file(GOOD)
        with self.assertRaisesRegex(tri.TriError, "source-url"):
            tri.import_file(p, self.cache, "http://insecure", "2026-10-29")
        with self.assertRaisesRegex(tri.TriError, "retrieved-on"):
            tri.import_file(p, self.cache, PAGE, "29/10/2026")
        self.assertEqual(self.cache.records(), [])

    def test_an_invalid_file_is_not_imported(self):
        p = self.file(GOOD.replace("1001.00", "-5"))
        with self.assertRaises(tri.TriError):
            tri.import_file(p, self.cache, PAGE, "2026-10-29")
        self.assertEqual(self.cache.records(), [])


class Validate(Base):
    def test_columns_are_detected_and_rows_sorted_oldest_first(self):
        rep = tri.validate_text(GOOD)
        self.assertEqual((rep["date_col"], rep["value_col"]), ("Date", "Total Returns Index"))
        self.assertEqual(rep["date_format"], "%d %b %Y")
        self.assertTrue(rep["newest_first"])
        self.assertEqual((rep["first"], rep["last"]), ("2018-01-02", "2018-01-05"))
        self.assertEqual(rep["warnings"], [])

    def test_ambiguous_or_missing_columns_ask_to_be_named(self):
        two = "Date,Total Returns Index,TRI\n02 Jan 2018,1,1\n03 Jan 2018,2,2\n"
        with self.assertRaisesRegex(tri.TriError, "more than one value col.*--value-col"):
            tri.validate_text(two)
        rep = tri.validate_text(two, value_col="TRI")
        self.assertEqual(rep["value_col"], "TRI")
        with self.assertRaisesRegex(tri.TriError, "no value col"):
            tri.validate_text("Date,Close\n02 Jan 2018,1\n")
        with self.assertRaisesRegex(tri.TriError, "no column 'Nope'"):
            tri.validate_text(GOOD, value_col="Nope")

    def test_one_consistent_day_first_date_format(self):
        for d1, d2, fmt in (("02-Jan-2018", "03-Jan-2018", "%d-%b-%Y"), ("02-01-2018", "03-01-2018", "%d-%m-%Y"),
                            ("2018-01-02", "2018-01-03", "%Y-%m-%d"), ("02/01/2018", "03/01/2018", "%d/%m/%Y")):
            rep = tri.validate_text("Date,TRI\n%s,1\n%s,2\n" % (d1, d2))
            self.assertEqual(rep["date_format"], fmt)
            self.assertEqual(rep["first"], "2018-01-02")
        with self.assertRaisesRegex(tri.TriError, "consistent"):
            tri.validate_text("Date,TRI\n02 Jan 2018,1\n2018-01-03,2\n")
        with self.assertRaisesRegex(tri.TriError, "consistent"):
            tri.validate_text("Date,TRI\nyesterday,1\n")

    def test_duplicate_dates_and_bad_values_are_errors(self):
        with self.assertRaisesRegex(tri.TriError, "twice"):
            tri.validate_text("Date,TRI\n02 Jan 2018,1\n02 Jan 2018,1\n")
        for bad in ("0", "-1", "nan", "inf", "abc", ""):
            with self.assertRaises(tri.TriError, msg=bad):
                tri.validate_text("Date,TRI\n02 Jan 2018,%s\n03 Jan 2018,2\n" % bad)
        self.assertEqual(tri.validate_text('Date,TRI\n02 Jan 2018,"12,345.67"\n')["series"][0][1], 12345.67)

    def test_structure_errors(self):
        with self.assertRaisesRegex(tri.TriError, "no data rows"):
            tri.validate_text("Date,TRI\n")
        with self.assertRaisesRegex(tri.TriError, "too few columns"):
            tri.validate_text("Date,TRI\n02 Jan 2018\n")

    def test_gaps_moves_and_late_starts_are_warnings(self):
        text = "Date,TRI\n02 Jan 2019,100\n03 Jan 2019,130\n20 Jan 2019,131\n"  # starts a year late
        rep = tri.validate_text(text)
        kinds = sorted(w.split(":")[0] for w in rep["warnings"])
        self.assertEqual(kinds, ["coverage", "gap", "move"])
        self.assertEqual(tri.validate_text(text, max_gap_days=30, max_move=0.5, want_from="2019-01-01")["warnings"], [])
        # the first trading day after the window opens is not a coverage gap
        self.assertEqual(tri.validate_text("Date,TRI\n02 Jan 2018,1\n03 Jan 2018,1\n")["warnings"], [])

    def test_the_file_must_be_csv_text(self):
        with self.assertRaisesRegex(tri.TriError, "HTML page"):
            tri.read_file(self.file("", raw=b"<!DOCTYPE html><html></html>"))
        with self.assertRaisesRegex(tri.TriError, "UTF-8"):
            tri.read_file(self.file("", raw=b"Date,TRI\n02 Jan 2018,\xff\xfe1\n"))
        with self.assertRaisesRegex(tri.TriError, "cannot read"):
            tri.read_file(os.path.join(self.tmp.name, "missing.csv"))

    def test_cli_import_then_status(self):
        p = self.file(GOOD)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(tri.main(["--cache", self.cache.root, "import", p, "--source-url", PAGE,
                                       "--retrieved-on", "2026-10-29"]), 0)
            self.assertEqual(tri.main(["--cache", self.cache.root, "status"]), 0)
        self.assertIn("AVAILABLE", out.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(tri.main(["--cache", self.cache.root, "validate", self.file("Date,Close\n1,1\n", "x.csv")]), 1)


if __name__ == "__main__":
    unittest.main()
