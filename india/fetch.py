#!/usr/bin/env python3
"""Polite downloads into a local, checksummed cache. Standard library only.

Every download starts from a source in sources.json and is refused unless
that source is fetchable (ok or partial). The request carries one honest
User-Agent and nothing else: no cookies, no Referer, no Origin, no browser
impersonation. A redirect may only land on another fetchable host. Each host
gets at most one request per policy.min_interval_seconds.

What comes back is untrusted. It is checked against what the source should
send (a zip starts with PK, a CSV is not a web page) because these sites
answer a missing file with an HTML page and HTTP 200. It is then stored by
its SHA-256 and recorded, never executed and never committed:

  <cache>/blobs/ab/abcdef...     the bytes, named by their SHA-256
  <cache>/manifest.jsonl         one JSON record per download, import or
                                 refusal, appended, never rewritten

A refusal (an HTTP error, or a page where data should be) is recorded too,
with its reason, and the same URL is not asked again unless --retry is
given. A network failure is recorded but not remembered as a refusal.
Neither ever means the market was closed that day: see calendar_in.py.

    python3 fetch.py get nse-bhavcopy-legacy --date 2018-01-02
    python3 fetch.py get amfi-nav-history --param FROM=01-Oct-2026 --param TO=27-Oct-2026
    python3 fetch.py get nse-bhavcopy-legacy --date 2018-01-02 --offline   # cache only
    python3 fetch.py get nse-bhavcopy-legacy --date 2018-01-02 --retry     # ask again after a refusal
    python3 fetch.py list                                                  # what is cached
    python3 fetch.py verify                                                # re-hash every blob

The default cache is india/data/cache, which .gitignore keeps out of git.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import sources as src  # noqa: E402

DEFAULT_CACHE = os.path.join(HERE, "data", "cache")
FORBIDDEN_HEADERS = ("cookie", "referer", "origin", "x-requested-with")


# why a fetch failed; all but "network" are the site's answer, remembered per URL
REFUSALS = ("http", "not-data", "too-large", "redirect")


class FetchError(Exception):
    def __init__(self, msg, reason=None):
        Exception.__init__(self, msg)
        self.reason = reason


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the cache

class Cache(object):
    """Bytes stored by SHA-256, with an append-only manifest of where they came from."""

    def __init__(self, root=DEFAULT_CACHE):
        self.root = root
        self.manifest = os.path.join(root, "manifest.jsonl")

    def _blob(self, digest):
        return os.path.join(self.root, "blobs", digest[:2], digest)

    def put(self, data, record):
        digest = sha256(data)
        path = self._blob(digest)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        rec = dict(record)
        rec["sha256"] = digest
        rec["bytes"] = len(data)
        return self.note(rec)

    def note(self, record):
        """Append a record that has no bytes (a refusal, a verification)."""
        os.makedirs(self.root, exist_ok=True)
        with open(self.manifest, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def read(self, digest):
        """The bytes for a digest, re-hashed so a changed file is never used."""
        try:
            with open(self._blob(digest), "rb") as fh:
                data = fh.read()
        except OSError:
            raise FetchError("blob %s is not in the cache" % digest)
        if sha256(data) != digest:
            raise FetchError("blob %s does not match its checksum; delete it and fetch again" % digest)
        return data

    def records(self):
        if not os.path.exists(self.manifest):
            return []
        out = []
        with open(self.manifest, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    raise FetchError("manifest line %d is not JSON" % n)
        return out

    def latest(self, **match):
        """The newest record whose fields equal every given value, or None."""
        hit = None
        for r in self.records():
            if all(r.get(k) == v for k, v in match.items()):
                hit = r
        return hit

    def verify(self):
        """(digest, problem) for every manifest entry whose blob is missing or changed."""
        bad = []
        for d in sorted({r["sha256"] for r in self.records() if r.get("sha256")}):
            try:
                self.read(d)
            except FetchError as e:
                bad.append((d, str(e)))
        return bad


# ---------------------------------------------------------------- checks

def looks_html(data):
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head


def sniff(data, expect):
    """None if the bytes look like what the source should send, else why not."""
    if not data:
        return "the response is empty"
    if expect == "html":
        return None
    if looks_html(data):
        return "an HTML page came back where %s data was expected (the site's 'not found' page)" % expect
    if expect == "zip":
        return None if data[:4] == b"PK\x03\x04" else "not a zip archive"
    if b"\x00" in data[:4096]:
        return "binary bytes where text was expected"
    if expect == "csv" and b"," not in data.split(b"\n", 1)[0]:
        return "the first line has no comma, so this is not a CSV"
    return None


def check_url(url, hosts, reason=None):
    u = urlsplit(url)
    if u.scheme != "https":
        raise FetchError("refusing %s: only https" % url, reason)
    if u.hostname not in hosts:
        raise FetchError("refusing %s: %s is not a fetchable host in sources.json" % (url, u.hostname), reason)


# ---------------------------------------------------------------- the network

class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.hosts, "redirect")
        return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)


def urllib_opener(hosts):
    """The real network call: (url, headers, timeout, max_bytes) -> response dict."""
    opener = urllib.request.build_opener(_Redirects(hosts))

    def open_(url, headers, timeout, max_bytes):
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as r:
                body = r.read(max_bytes + 1)
                return {"status": r.status, "url": r.geturl(), "content_type": r.headers.get("Content-Type", ""),
                        "body": body}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "url": url, "content_type": "", "body": b""}
        except (urllib.error.URLError, OSError) as e:
            raise FetchError("could not reach %s: %s" % (url, getattr(e, "reason", e)))
    return open_


class Fetcher(object):
    def __init__(self, registry, cache, opener=None, clock=time.monotonic, sleep=time.sleep):
        self.doc = registry
        self.cache = cache
        self.policy = registry["policy"]
        self.hosts = set(src.allowed_hosts(registry))
        self.opener = opener or urllib_opener(self.hosts)
        self.clock = clock
        self.sleep = sleep
        self.last = {}

    def headers(self):
        h = {"User-Agent": self.policy["user_agent"]}
        assert not any(k.lower() in FORBIDDEN_HEADERS for k in h)
        return h

    def _wait(self, host):
        gap = float(self.policy["min_interval_seconds"])
        if host in self.last:
            left = gap - (self.clock() - self.last[host])
            if left > 0:
                self.sleep(left)
        self.last[host] = self.clock()

    def url(self, source_id, date=None, params=None):
        s = src.get(self.doc, source_id)
        return s, src.render(s, src.params_for(s, date, params))

    def refused(self, url):
        """The last refusal recorded for this URL, if no success came after it."""
        last = None
        for r in self.cache.records():
            if r.get("url") == url and r.get("kind") in ("fetch", "manual-import"):
                last = r
        return last if last and not last.get("ok") and last.get("reason") in REFUSALS else None

    def get(self, source_id, date=None, params=None, offline=False, retry=False):
        """The cached record for this exact URL, or a fresh download of it."""
        s, url = self.url(source_id, date, params)
        hit = self.cache.latest(url=url, ok=True)
        if hit:
            self.cache.read(hit["sha256"])
            return hit
        if offline:
            raise FetchError("%s is not cached and --offline was given" % url)
        if not src.fetchable(self.doc, s):
            raise FetchError("%s is %s in sources.json: %s" % (source_id, s["status"], s["evidence"]))
        check_url(url, self.hosts)
        old = None if retry else self.refused(url)
        if old:
            raise FetchError("%s was refused at %s (%s); not asking again without --retry"
                             % (url, old["retrieved_at"], old["detail"]), old["reason"])
        base = {"kind": "fetch", "source": source_id, "url": url, "expect": s["expect"],
                "date": None if date is None else str(date)}
        try:
            self._wait(urlsplit(url).hostname)
            body, r = self._download(url, s["expect"])
        except FetchError as e:
            self.cache.note(dict(base, ok=False, reason=e.reason or "network", detail=str(e),
                                 retrieved_at=utc_now()))
            raise
        return self.cache.put(body, dict(base, ok=True, final_url=r["url"], content_type=r["content_type"],
                                         retrieved_at=utc_now()))

    def _download(self, url, expect):
        max_bytes = int(self.policy["max_bytes"])
        r = self.opener(url, self.headers(), float(self.policy["timeout_seconds"]), max_bytes)
        check_url(r["url"], self.hosts, "redirect")
        if r["status"] != 200:
            raise FetchError("%s answered HTTP %s" % (url, r["status"]), "http")
        if len(r["body"]) > max_bytes:
            raise FetchError("%s is larger than policy.max_bytes (%d)" % (url, max_bytes), "too-large")
        why = sniff(r["body"], expect)
        if why:
            raise FetchError("%s: %s" % (url, why), "not-data")
        return r["body"], r


def main(argv=None):
    ap = argparse.ArgumentParser(description="Download a registered India source into the local cache.")
    ap.add_argument("--registry", default=src.DEFAULT_PATH)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pg = sub.add_parser("get")
    pg.add_argument("source")
    pg.add_argument("--date", help="YYYY-MM-DD")
    pg.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    pg.add_argument("--offline", action="store_true", help="answer from the cache or fail")
    pg.add_argument("--retry", action="store_true", help="ask again for a URL that was refused before")
    sub.add_parser("list")
    sub.add_parser("verify")
    a = ap.parse_args(argv)
    cache = Cache(a.cache)
    try:
        if a.cmd == "list":
            for r in cache.records():
                what = "%s  %9d" % (r["sha256"][:12], r["bytes"]) if r.get("sha256") else "%-22s" % (
                    "REFUSED " + r.get("reason", "") if r.get("kind") == "fetch" else r.get("kind", ""))
                print("%s  %s  %s" % (r.get("retrieved_at") or r.get("imported_at", "?"), what,
                                      r.get("url") or r.get("source_url", "")))
            return 0
        if a.cmd == "verify":
            bad = cache.verify()
            for d, why in bad:
                print("BAD  %s  %s" % (d[:12], why))
            print("%d records, %d bad." % (len(cache.records()), len(bad)))
            return 1 if bad else 0
        f = Fetcher(src.read(a.registry), cache)
        extra = dict(p.split("=", 1) for p in a.param if "=" in p)
        rec = f.get(a.source, a.date, extra, a.offline, a.retry)
        print("%s  %d bytes  sha256 %s" % (rec["url"], rec["bytes"], rec["sha256"]))
    except (FetchError, src.SourceError) as e:
        print("fetch.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
