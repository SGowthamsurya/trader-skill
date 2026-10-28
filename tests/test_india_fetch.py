import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

from _load import ROOT

INDIA = os.path.join(ROOT, "india")
if INDIA not in sys.path:
    sys.path.insert(0, INDIA)
import fetch  # noqa: E402
import sources as src  # noqa: E402

NSE_2018 = "https://nsearchives.nseindia.com/content/historical/EQUITIES/2018/JAN/cm02JAN2018bhav.csv.zip"
HTML = b"  <!DOCTYPE html> <html><head><title>Page not found</title></head></html>"


def a_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cm02JAN2018bhav.csv", "SYMBOL,SERIES,CLOSE\nSYNTH,EQ,100.00\n")  # synthetic
    return buf.getvalue()


class FakeNet(object):
    """Stands in for the network: records every call, answers from a table."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append((url, dict(headers)))
        a = self.answers[url]
        return {"status": a.get("status", 200), "url": a.get("final", url),
                "content_type": a.get("type", ""), "body": a.get("body", b"")}


class Clock(object):
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = fetch.Cache(os.path.join(self.tmp.name, "cache"))
        self.doc = src.read()
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, answers):
        net = FakeNet(answers)
        return fetch.Fetcher(self.doc, self.cache, opener=net, clock=self.clock, sleep=self.clock.sleep), net


class Fetching(Base):
    def test_a_good_download_is_cached_with_its_checksum(self):
        body = a_zip()
        f, net = self.fetcher({NSE_2018: {"body": body, "type": "application/zip"}})
        rec = f.get("nse-bhavcopy-legacy", "2018-01-02")
        self.assertEqual(rec["sha256"], fetch.sha256(body))
        self.assertEqual(rec["bytes"], len(body))
        self.assertEqual(rec["source"], "nse-bhavcopy-legacy")
        self.assertTrue(rec["retrieved_at"].endswith("Z"))
        self.assertEqual(self.cache.read(rec["sha256"]), body)
        self.assertEqual(len(net.calls), 1)

    def test_the_second_ask_is_answered_from_the_cache(self):
        f, net = self.fetcher({NSE_2018: {"body": a_zip()}})
        a = f.get("nse-bhavcopy-legacy", "2018-01-02")
        b = f.get("nse-bhavcopy-legacy", "2018-01-02")
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual(len(net.calls), 1)

    def test_only_an_honest_user_agent_is_sent(self):
        f, net = self.fetcher({NSE_2018: {"body": a_zip()}})
        f.get("nse-bhavcopy-legacy", "2018-01-02")
        headers = net.calls[0][1]
        self.assertEqual(list(headers), ["User-Agent"])
        self.assertTrue(headers["User-Agent"].startswith("trader-skill-india/"))
        self.assertNotIn("Mozilla", headers["User-Agent"])

    def test_an_html_not_found_page_with_200_is_refused(self):
        f, _ = self.fetcher({NSE_2018: {"body": HTML}})
        with self.assertRaisesRegex(fetch.FetchError, "HTML page"):
            f.get("nse-bhavcopy-legacy", "2018-01-02")
        self.assertEqual(self.cache.records(), [])

    def test_http_errors_are_reported_not_cached(self):
        f, _ = self.fetcher({NSE_2018: {"status": 403}})
        with self.assertRaisesRegex(fetch.FetchError, "HTTP 403"):
            f.get("nse-bhavcopy-legacy", "2018-01-02")
        self.assertEqual(self.cache.records(), [])

    def test_oversize_bodies_are_refused(self):
        self.doc["policy"]["max_bytes"] = 10
        f, _ = self.fetcher({NSE_2018: {"body": a_zip()}})
        with self.assertRaisesRegex(fetch.FetchError, "max_bytes"):
            f.get("nse-bhavcopy-legacy", "2018-01-02")

    def test_a_redirect_to_an_unregistered_host_is_refused(self):
        f, _ = self.fetcher({NSE_2018: {"body": a_zip(), "final": "https://evil.example.com/x.zip"}})
        with self.assertRaisesRegex(fetch.FetchError, "not a fetchable host"):
            f.get("nse-bhavcopy-legacy", "2018-01-02")

    def test_the_real_redirect_handler_checks_the_new_host(self):
        h = fetch._Redirects({"nsearchives.nseindia.com"})
        with self.assertRaises(fetch.FetchError):
            h.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/")
        with self.assertRaises(fetch.FetchError):
            h.redirect_request(None, None, 302, "Found", {}, "http://nsearchives.nseindia.com/x")

    def test_blocked_manual_and_unverified_sources_are_never_requested(self):
        f, net = self.fetcher({})
        for sid in ("nse-website", "bse-api", "incometax-india", "nifty50-tri-history", "india-code"):
            with self.assertRaises(fetch.FetchError, msg=sid):
                f.get(sid)
        self.assertEqual(net.calls, [])

    def test_offline_answers_only_from_the_cache(self):
        f, net = self.fetcher({})
        with self.assertRaisesRegex(fetch.FetchError, "not cached"):
            f.get("nse-bhavcopy-legacy", "2018-01-02", offline=True)
        self.assertEqual(net.calls, [])

    def test_one_request_per_host_per_interval(self):
        url2 = NSE_2018.replace("02JAN", "03JAN")
        f, _ = self.fetcher({NSE_2018: {"body": a_zip()}, url2: {"body": a_zip() + b""}})
        f.get("nse-bhavcopy-legacy", "2018-01-02")
        self.clock.t += 0.25
        f.get("nse-bhavcopy-legacy", "2018-01-03")
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], 0.75)


class CacheIntegrity(Base):
    def test_a_changed_blob_is_detected(self):
        rec = self.cache.put(b"SYNTH,1\n", {"kind": "test", "url": "x"})
        path = self.cache._blob(rec["sha256"])
        with open(path, "wb") as fh:
            fh.write(b"tampered")
        with self.assertRaisesRegex(fetch.FetchError, "checksum"):
            self.cache.read(rec["sha256"])
        self.assertEqual(len(self.cache.verify()), 1)

    def test_the_manifest_is_append_only(self):
        self.cache.put(b"a,1\n", {"kind": "test", "url": "a"})
        self.cache.put(b"a,1\n", {"kind": "test", "url": "a"})
        self.assertEqual(len(self.cache.records()), 2)
        self.assertEqual(len(os.listdir(os.path.join(self.cache.root, "blobs"))), 1)

    def test_verify_cli_on_an_empty_cache(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(fetch.main(["--cache", self.cache.root, "verify"]), 0)
        self.assertIn("0 records, 0 bad", out.getvalue())


class Sniff(unittest.TestCase):
    def test_formats(self):
        self.assertIsNone(fetch.sniff(a_zip(), "zip"))
        self.assertIn("not a zip", fetch.sniff(b"SYMBOL,CLOSE\n", "zip"))
        self.assertIsNone(fetch.sniff(b"Date,Value\n02-01-2018,1\n", "csv"))
        self.assertIn("no comma", fetch.sniff(b"Date;Value\n", "csv"))
        self.assertIn("binary", fetch.sniff(b"a,b\x00\x01", "text"))
        self.assertIn("empty", fetch.sniff(b"", "csv"))
        self.assertIsNone(fetch.sniff(HTML, "html"))
        for expect in ("zip", "csv", "text"):
            self.assertIn("HTML page", fetch.sniff(HTML, expect))


if __name__ == "__main__":
    unittest.main()
