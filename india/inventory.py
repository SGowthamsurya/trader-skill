#!/usr/bin/env python3
"""A committed inventory of the local cache: checksums only, never the data.

The downloaded files stay out of git (redistribution terms unreviewed). This
inventory records, for every file the study relies on, its source, date,
SHA-256 and size, so a cache that is lost and collected again can be checked
file by file:

  present    the file is in the cache and its bytes hash to the recorded sha256
  missing    no file with that sha256 is in the cache (re-collect or re-import;
             a different checksum for the same source and date means the
             publisher's file changed, and anything derived from it must be
             regenerated and compared)
  corrupt    the cached bytes do not hash to their name

Price-file URLs follow from the source and date (sources.json); documents
keep their URL. Refusals and network failures are not listed: they are
evidence about access on a date, not files.

    python3 inventory.py build --write     # rewrite india/cache_inventory.json from the cache
    python3 inventory.py check             # compare the cache with the committed inventory
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fetch  # noqa: E402

INVENTORY = os.path.join(HERE, "cache_inventory.json")
SCHEMA = "india-cache-inventory/1"


class InventoryError(Exception):
    pass


def build(cache):
    items = {}
    for r in cache.records():
        sha = r.get("sha256")
        if not sha:
            continue
        if r.get("kind") == "invalid":
            items[(r["source"], r["date"], sha, "invalid")] = {"source": r["source"], "date": r["date"], "sha256": sha,
                                                                 "role": "corrupt-published-file"}
        elif r.get("ok") and r.get("kind") == "fetch":
            e = {"source": r["source"], "date": r.get("date"), "sha256": sha, "bytes": r["bytes"], "role": "data"}
            if not r.get("date"):
                e["url"] = r["url"]
            items[(r["source"], r.get("date") or "", sha, "data")] = e
        elif r.get("ok") and r.get("kind") == "manual-import":
            items[(r["source"], r.get("retrieved_on") or "", sha, "import")] = {
                "source": r["source"], "date": r.get("retrieved_on"), "sha256": sha, "bytes": r["bytes"],
                "role": "imported-document", "url": r.get("source_url"), "title": r.get("title") or r.get("filename")}
    records = [items[k] for k in sorted(items)]
    return {"schema": SCHEMA, "about": "Checksums of every cached file the study relies on; no data. See india/inventory.py.",
            "records": records, "count": len(records)}


def dumps(doc):
    """One record per line, keys sorted: small, stable diffs."""
    head = {k: v for k, v in doc.items() if k != "records"}
    lines = [json.dumps(r, sort_keys=True, separators=(",", ":")) for r in doc["records"]]
    return (json.dumps(head, sort_keys=True)[:-1] + ', "records": [\n' + ",\n".join(lines) + "\n]}\n")


def check(cache, doc):
    if doc.get("schema") != SCHEMA:
        raise InventoryError("not an inventory (schema %r)" % doc.get("schema"))
    have = {r["sha256"] for r in cache.records() if r.get("sha256")}
    out = {"present": [], "missing": [], "corrupt": []}
    for e in doc["records"]:
        if e["sha256"] not in have:
            out["missing"].append(e)
            continue
        try:
            cache.read(e["sha256"])
            out["present"].append(e)
        except fetch.FetchError:
            out["corrupt"].append(e)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build or check the committed cache inventory.")
    ap.add_argument("--cache", default=fetch.DEFAULT_CACHE)
    ap.add_argument("--inventory", default=INVENTORY)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pb = sub.add_parser("build")
    pb.add_argument("--write", action="store_true")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    cache = fetch.Cache(a.cache)
    try:
        if a.cmd == "build":
            doc = build(cache)
            text = dumps(doc)
            if a.write:
                with open(a.inventory, "w", encoding="utf-8") as fh:
                    fh.write(text)
            print("%d files in the inventory%s" % (doc["count"], " (written)" if a.write else ""))
            return 0
        with open(a.inventory, encoding="utf-8") as fh:
            res = check(cache, json.load(fh))
        for k in ("missing", "corrupt"):
            for e in res[k][:10]:
                print("%s %s %s %s" % (k.upper(), e["source"], e.get("date"), e["sha256"][:12]))
        print("present %d, missing %d, corrupt %d" % (len(res["present"]), len(res["missing"]), len(res["corrupt"])))
        return 0 if not (res["missing"] or res["corrupt"]) else 1
    except (InventoryError, OSError, ValueError, KeyError) as e:
        print("inventory.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
