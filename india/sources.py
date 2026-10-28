#!/usr/bin/env python3
"""The NSE/BSE source registry (india/sources.json). Standard library only, no network.

Every place the India tools may read from is listed in sources.json with what
it is for, whether it is a primary source, and whether a cloud session could
reach it on the date it was checked. This module checks that file and turns
a URL template into a URL. Nothing else is allowed to invent a URL.

    python3 sources.py check                 # validate sources.json
    python3 sources.py list                  # one line per source
    python3 sources.py list --purpose prices
    python3 sources.py url nse-bhavcopy-legacy --date 2018-01-02

Status words:

  ok          reached, and the file was the data it should be
  partial     reached, with a gap that is written down (some dates, a page
              without the data in it)
  blocked     the site refused us (403, empty reply); never worked around
  manual      the user downloads it in a browser and imports it
  unverified  not checked yet

Only ok and partial sources can be fetched (policy.fetchable_status).
"""

import argparse
import datetime
import json
import os
import re
import sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "sources.json")

STATUSES = ("ok", "partial", "blocked", "manual", "unverified")
AUTHORITIES = ("primary", "secondary")
PURPOSES = ("prices", "index", "tri", "constituents", "nav", "corporate-actions", "calendar",
            "costs", "tax", "risk-free", "depository")
EXPECTS = ("zip", "csv", "text", "html")
REQUIRED = ("id", "title", "publisher", "authority", "purpose", "url", "expect", "status",
            "checked_at", "evidence", "uncertainties")
POLICY_REQUIRED = ("user_agent", "min_interval_seconds", "timeout_seconds", "max_bytes", "fetchable_status")
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class SourceError(Exception):
    pass


def read(path=DEFAULT_PATH):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise SourceError("cannot read %s: %s" % (path, e))
    problems = validate(doc)
    if problems:
        raise SourceError("%s is not valid:\n  %s" % (path, "\n  ".join(problems)))
    return doc


def _date_ok(s):
    try:
        datetime.date.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


def validate(doc):
    """Every problem with a registry document, as plain sentences. Empty means valid."""
    out = []
    if not isinstance(doc, dict):
        return ["the registry is not a JSON object"]
    pol = doc.get("policy")
    if not isinstance(pol, dict):
        out.append("policy is missing")
    else:
        for k in POLICY_REQUIRED:
            if k not in pol:
                out.append("policy.%s is missing" % k)
        for s in pol.get("fetchable_status") or []:
            if s not in ("ok", "partial"):
                out.append("policy.fetchable_status may only name ok or partial, not %r" % s)
    srcs = doc.get("sources")
    if not isinstance(srcs, list) or not srcs:
        return out + ["sources is missing or empty"]
    seen = set()
    for i, s in enumerate(srcs):
        name = s.get("id") if isinstance(s, dict) else None
        where = "source %s" % (name or "#%d" % i)
        if not isinstance(s, dict):
            out.append("%s is not an object" % where)
            continue
        for k in REQUIRED:
            if k not in s:
                out.append("%s: %s is missing" % (where, k))
        if name is not None:
            if not isinstance(name, str) or not SLUG.match(name):
                out.append("%s: id must be lower-case words joined by hyphens" % where)
            elif name in seen:
                out.append("%s: duplicate id" % where)
            seen.add(name)
        if "status" in s and s["status"] not in STATUSES:
            out.append("%s: status %r is not one of %s" % (where, s["status"], ", ".join(STATUSES)))
        if "authority" in s and s["authority"] not in AUTHORITIES:
            out.append("%s: authority %r is not primary or secondary" % (where, s["authority"]))
        if "expect" in s and s["expect"] not in EXPECTS:
            out.append("%s: expect %r is not one of %s" % (where, s["expect"], ", ".join(EXPECTS)))
        if "purpose" in s:
            if not isinstance(s["purpose"], list) or not s["purpose"]:
                out.append("%s: purpose must be a non-empty list" % where)
            else:
                for p in s["purpose"]:
                    if p not in PURPOSES:
                        out.append("%s: unknown purpose %r" % (where, p))
        if "url" in s:
            u = urlsplit(s["url"]) if isinstance(s["url"], str) else None
            if not u or u.scheme != "https" or not u.hostname:
                out.append("%s: url must be https with a host" % where)
        if "checked_at" in s:
            if s.get("status") == "unverified":
                if s["checked_at"] is not None:
                    out.append("%s: an unverified source has checked_at null" % where)
            elif not _date_ok(s["checked_at"]):
                out.append("%s: checked_at must be YYYY-MM-DD" % where)
        if "uncertainties" in s and not isinstance(s["uncertainties"], list):
            out.append("%s: uncertainties must be a list" % where)
    return out


def get(doc, source_id):
    for s in doc["sources"]:
        if s["id"] == source_id:
            return s
    raise SourceError("no source %r in the registry" % source_id)


def fetchable(doc, source):
    return source["status"] in doc["policy"]["fetchable_status"]


def allowed_hosts(doc):
    """Hosts that a fetch may touch: those of fetchable sources, nothing else."""
    return sorted({urlsplit(s["url"]).hostname for s in doc["sources"] if fetchable(doc, s)})


def placeholders(template):
    return PLACEHOLDER.findall(template)


def render(source, params):
    """The source's URL with every {NAME} filled in. Every placeholder must be
    given, nothing extra may be given, and each value must be a short plain
    token, so a parameter can never add a path, a query or another host."""
    names = set(placeholders(source["url"]))
    given = set(params)
    if names - given:
        raise SourceError("%s needs %s" % (source["id"], ", ".join(sorted(names - given))))
    if given - names:
        raise SourceError("%s does not take %s" % (source["id"], ", ".join(sorted(given - names))))
    for k, v in params.items():
        if not isinstance(v, str) or not SAFE_VALUE.match(v):
            raise SourceError("%s=%r is not a plain token (letters, digits, - and _)" % (k, v))
    return PLACEHOLDER.sub(lambda m: params[m.group(1)], source["url"])


def date_params(d):
    """The date tokens the exchange file names use, for one calendar date."""
    if isinstance(d, str):
        try:
            d = datetime.date.fromisoformat(d)
        except ValueError:
            raise SourceError("dates are YYYY-MM-DD, not %r" % d)
    mon = MONTHS[d.month - 1]
    return {"YYYY": "%04d" % d.year, "MON": mon, "DD": "%02d" % d.day,
            "YYYYMMDD": d.strftime("%Y%m%d"), "DDMMYY": d.strftime("%d%m%y"),
            "DDMMYYYY": d.strftime("%d%m%Y"), "DD_Mon_YYYY": "%02d-%s-%04d" % (d.day, mon.title(), d.year)}


def params_for(source, date=None, extra=None):
    """Only the date tokens this source's template actually uses, plus extras."""
    out = {}
    if date is not None:
        names = set(placeholders(source["url"]))
        out.update({k: v for k, v in date_params(date).items() if k in names})
    out.update(extra or {})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check and list the India data sources.")
    ap.add_argument("--registry", default=DEFAULT_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    pl = sub.add_parser("list")
    pl.add_argument("--purpose", choices=PURPOSES)
    pu = sub.add_parser("url")
    pu.add_argument("source")
    pu.add_argument("--date", help="YYYY-MM-DD")
    pu.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    a = ap.parse_args(argv)
    try:
        doc = read(a.registry)
        if a.cmd == "check":
            print("%d sources, valid. Fetchable hosts: %s" % (len(doc["sources"]), ", ".join(allowed_hosts(doc))))
        elif a.cmd == "list":
            for s in doc["sources"]:
                if a.purpose and a.purpose not in s["purpose"]:
                    continue
                print("%-10s %-26s %-9s %s" % (s["status"], s["id"], s["authority"], ",".join(s["purpose"])))
        else:
            src = get(doc, a.source)
            extra = dict(p.split("=", 1) for p in a.param if "=" in p)
            print(render(src, params_for(src, a.date, extra)))
    except SourceError as e:
        print("sources.py: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
