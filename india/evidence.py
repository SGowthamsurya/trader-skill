#!/usr/bin/env python3
"""Official documents as evidence: stored with where they came from, never trusted blindly.

A document (an AMC notice, an exchange circular, a SEBI circular, a holiday
list) is imported into the local cache with its source URL, the day it was
retrieved and its SHA-256. Confirmations and corporate actions cite the
SHA-256, so anyone can check that the cited file is the one that was read.
Documents stay in the ignored cache: a circular's text is the publisher's,
and its redistribution terms have not been reviewed.

    python3 evidence.py import addendum.pdf --url https://... --retrieved-on 2026-10-28 \\
        --title "Notice cum Addendum No. 107" --publisher "Reliance Nippon Life AMC"
    python3 evidence.py list
    python3 evidence.py check            # every cited document is in the cache and unchanged
"""

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fetch  # noqa: E402

SOURCE = "official-document"
MAX_BYTES = 50 * 1024 * 1024
SHA = re.compile(r"^[0-9a-f]{64}$")
CITING = ("corporate_actions.json", "identity_confirmations.json", "cost_tax_sources.json")


class EvidenceError(Exception):
    pass


def import_document(path, cache, url, retrieved_on, title, publisher, mirror_of=None):
    if not str(url).startswith("https://"):
        raise EvidenceError("--url must be the https address the file was downloaded from")
    try:
        datetime.date.fromisoformat(retrieved_on)
    except (TypeError, ValueError):
        raise EvidenceError("--retrieved-on must be YYYY-MM-DD")
    if not title or not publisher:
        raise EvidenceError("a document needs a title and a publisher")
    size = os.path.getsize(path)
    if size == 0 or size > MAX_BYTES:
        raise EvidenceError("%s is %d bytes; refusing" % (path, size))
    with open(path, "rb") as fh:
        data = fh.read()
    if fetch.looks_html(data) and not path.lower().endswith((".html", ".htm")):
        raise EvidenceError("%s is a web page, not the document (a 'not found' page?)" % path)
    rec = {"kind": "manual-import", "ok": True, "source": SOURCE, "source_url": url, "retrieved_on": retrieved_on,
           "imported_at": fetch.utc_now(), "filename": os.path.basename(path), "title": title, "publisher": publisher}
    if mirror_of:
        rec["mirror_of"] = mirror_of
    return cache.put(data, rec)


def documents(cache):
    return [r for r in cache.records() if r.get("source") == SOURCE and r.get("ok")]


def cited(paths):
    """{sha256: [where]} for every document a committed evidence file cites."""
    out = {}
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)

        def walk(x, where):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k == "sha256" and isinstance(v, str):
                        if not SHA.match(v):
                            raise EvidenceError("%s cites a malformed sha256 %r" % (where, v))
                        out.setdefault(v, []).append(where)
                    else:
                        walk(v, where)
            elif isinstance(x, list):
                for v in x:
                    walk(v, where)
        walk(doc, os.path.basename(p))
    return out


def check(cache, paths):
    """[(sha256, problem)] for cited documents that are missing or changed locally."""
    have = {r["sha256"] for r in cache.records() if r.get("sha256")}
    bad = []
    for sha, where in sorted(cited(paths).items()):
        if sha not in have:
            bad.append((sha, "cited by %s but not in the local cache" % ", ".join(sorted(set(where)))))
            continue
        try:
            cache.read(sha)
        except fetch.FetchError as e:
            bad.append((sha, str(e)))
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import and check official documents used as evidence.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("import")
    pi.add_argument("file")
    pi.add_argument("--url", required=True)
    pi.add_argument("--retrieved-on", required=True)
    pi.add_argument("--title", required=True)
    pi.add_argument("--publisher", required=True)
    pi.add_argument("--mirror-of", help="when the file was taken from a copy, the original publisher or path")
    sub.add_parser("list")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "import":
            rec = import_document(a.file, cache, a.url, a.retrieved_on, a.title, a.publisher, a.mirror_of)
            print("Imported %s, sha256 %s" % (rec["filename"], rec["sha256"]))
        elif a.cmd == "list":
            for r in documents(cache):
                print("%s  %s  %s (%s)  %s" % (r["sha256"][:12], r["retrieved_on"], r["title"], r["publisher"],
                                               r["source_url"]))
        else:
            bad = check(cache, [os.path.join(HERE, p) for p in CITING])
            for sha, why in bad:
                print("MISSING %s  %s" % (sha[:12], why))
            print("%d problems." % len(bad))
            return 1 if bad else 0
    except (EvidenceError, fetch.FetchError, OSError, ValueError) as e:
        print("evidence.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
