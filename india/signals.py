#!/usr/bin/env python3
"""Strategy interface and an append-only, hash-chained signal ledger.

A strategy is a class with NAME, VERSION, LOOKBACK (sessions it needs) and
decide(inp) -> {key: weight}. It never receives a pit.View, the Universe,
its Evidence, the cache, fills or any portfolio. It receives a StrategyInput
built by the engine from a past-only view and the day's universe snapshot:
the decision date, the eligible keys, the digests of the data and universe
it was decided from, the lookback sessions, and closes of eligible keys
inside that lookback. Because it is plain data it can be handed to a
separate process (run_isolated) where nothing after the decision date exists.

Ledger (one canonical JSON record per line, see canonical()):

  open      seq 0: venue, strategy name / version / code hash, isolation
            mode, reproducibility metadata (Python version, SHA-256 of
            every engine and criteria file); prev_hash is 64 zeros
  signal    seq 1..n: decision_date, eligible keys, targets (key, isin,
            weight as a 6-decimal string), input digests, strategy name
            and version
  seal      last: number of records, head (hash of the last signal) and
            sealed_at, the only timestamp, so the signal chain itself is
            reproducible

Every record's hash is SHA-256 of its canonical JSON without the hash
field, and every prev_hash is the hash of the record before it. Opening a
ledger re-verifies the whole chain, so a changed, deleted, inserted or
reordered record, or a broken link, is reported. Decision dates must
strictly increase. Nothing can be appended after the seal. open_sealed()
is the only way later stages read signals: it refuses an unsealed ledger
and, given the head recorded elsewhere at sealing time, a different head.

Trust boundary: a hash chain proves the records agree with each other. A
process that can rewrite the file can also rewrite every hash and produce a
consistent forgery, or drop the seal and the last records together; only
the sealed head recorded outside the ledger (in the run report, before any
outcome is computed) detects that. run_isolated keeps future data out of the
strategy's memory and interface, but it is not a filesystem sandbox: a
strategy that opens the cache files itself is outside what this module can
prevent.
"""

import datetime
import decimal
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SCHEMA = "india-signal/1"
GENESIS = "0" * 64
WEIGHT = decimal.Decimal("0.000001")
REPRO_FILES = ("pit.py", "universe.py", "signals.py", "calendar_in.py", "marks.py", "etf_criteria.json",
               "data_gaps.json", "corporate_actions.json", "identity_confirmations.json")


class LedgerError(Exception):
    pass


class SignalError(Exception):
    pass


def canonical(obj):
    """Deterministic JSON. Floats are refused: every number in a record is an integer."""
    def check(x, where):
        if isinstance(x, float):
            raise SignalError("a float in %s; use an integer or a decimal string" % where)
        if isinstance(x, dict):
            for k, v in x.items():
                if not isinstance(k, str):
                    raise SignalError("a non-string key in %s" % where)
                check(v, where + "." + k)
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                check(v, "%s[%d]" % (where, i))
    check(obj, "record")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def record_hash(rec):
    return hashlib.sha256(canonical({k: v for k, v in rec.items() if k != "hash"}).encode("ascii")).hexdigest()


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def repro_metadata():
    files = {}
    for f in REPRO_FILES:
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            files[f] = file_sha(p)
    return {"python": "%d.%d.%d" % sys.version_info[:3], "files": files}


# ---------------------------------------------------------------- what a strategy sees

class StrategyInput(object):
    """Plain, past-only data for one decision. Read-only."""

    __slots__ = ("_d",)

    def __init__(self, data):
        object.__setattr__(self, "_d", data)

    def __setattr__(self, name, value):
        raise AttributeError("a StrategyInput is read-only")

    @classmethod
    def build(cls, view, snapshot, market_digest, lookback):
        if snapshot.as_of != view.as_of or snapshot.venue != view.venue:
            raise SignalError("the universe snapshot is for %s %s, the view for %s %s"
                              % (snapshot.venue, snapshot.as_of, view.venue, view.as_of))
        if not isinstance(lookback, int) or lookback < 1:
            raise SignalError("LOOKBACK must be a positive integer")
        sessions = view.sessions(lookback)
        eligible = sorted(snapshot.eligible())
        isins = {x.key: x.isin for x in snapshot.decisions if x.key in eligible}
        closes = {}
        for k in eligible:
            closes[k] = [[d, repr(c)] for d, c in view.closes(k, lookback) if d >= sessions[0]]
        return cls({"as_of": view.as_of, "venue": view.venue, "eligible": eligible, "isins": isins,
                    "universe_digest": snapshot.digest(), "market_digest": market_digest, "lookback": lookback,
                    "sessions": sessions, "closes": closes})

    @classmethod
    def from_json(cls, text):
        return cls(json.loads(text))

    def to_json(self):
        return json.dumps(self._d, sort_keys=True, separators=(",", ":"))

    as_of = property(lambda self: self._d["as_of"])
    venue = property(lambda self: self._d["venue"])
    eligible = property(lambda self: tuple(self._d["eligible"]))
    universe_digest = property(lambda self: self._d["universe_digest"])
    market_digest = property(lambda self: self._d["market_digest"])
    sessions = property(lambda self: tuple(self._d["sessions"]))

    def closes(self, key):
        """[(date, close)] for an eligible key inside the lookback; KeyError otherwise."""
        if key not in self._d["closes"]:
            raise KeyError("%s is not eligible on %s; no data is given for it" % (key, self.as_of))
        return [(d, float(c)) for d, c in self._d["closes"][key]]

    def isin(self, key):
        return self._d["isins"][key]


def validate_targets(raw, inp):
    """[{key, isin, weight}] sorted by key, or SignalError. Weights are decimal strings, >= 0, total <= 1."""
    if not isinstance(raw, dict):
        raise SignalError("decide() must return {key: weight}")
    out, total = [], decimal.Decimal(0)
    for key in sorted(raw):
        if key not in inp.eligible:
            raise SignalError("%s is not eligible on %s" % (key, inp.as_of))
        try:
            w = decimal.Decimal(str(raw[key])).quantize(WEIGHT, rounding=decimal.ROUND_DOWN)
        except (decimal.InvalidOperation, ValueError):
            raise SignalError("weight for %s is not a number: %r" % (key, raw[key]))
        if not w.is_finite() or w < 0:
            raise SignalError("weight for %s must be finite and not negative" % key)
        total += w
        if w > 0:
            out.append({"key": key, "isin": inp.isin(key), "weight": str(w)})
    if total > 1:
        raise SignalError("target weights add up to %s, more than 1" % total)
    return out


# ---------------------------------------------------------------- running a strategy

def strategy_identity(strategy_cls, source_path):
    for a in ("NAME", "VERSION", "LOOKBACK"):
        if not hasattr(strategy_cls, a):
            raise SignalError("a strategy class needs %s" % a)
    return {"name": str(strategy_cls.NAME), "version": str(strategy_cls.VERSION), "code_sha256": file_sha(source_path)}


def load_strategy(path, class_name):
    spec = importlib.util.spec_from_file_location("india_strategy_%s" % class_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, class_name)


def run_in_process(strategy_cls, inp):
    return strategy_cls().decide(inp)


_CHILD = r"""
import importlib.util, json, sys
path, cls_name, here = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, here)
import signals
inp = signals.StrategyInput.from_json(sys.stdin.read())
spec = importlib.util.spec_from_file_location("india_strategy_child", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
out = getattr(mod, cls_name)().decide(inp)
sys.stdout.write(json.dumps({k: str(v) for k, v in out.items()}, sort_keys=True))
"""


def run_isolated(path, class_name, inp, timeout=60):
    """Run decide() in a separate Python process that receives only the StrategyInput JSON."""
    with tempfile.TemporaryDirectory() as empty:
        p = subprocess.run([sys.executable, "-I", "-c", _CHILD, os.path.abspath(path), class_name, HERE],
                           input=inp.to_json(), capture_output=True, text=True, timeout=timeout, cwd=empty,
                           env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
    if p.returncode != 0:
        raise SignalError("the strategy process failed: %s" % p.stderr.strip()[-500:])
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise SignalError("the strategy process did not return JSON")


# ---------------------------------------------------------------- the ledger

def verify(records):
    """Raise LedgerError unless records form an intact chain; return (sealed, head)."""
    if not records:
        raise LedgerError("the ledger is empty")
    prev, last_date, sealed = GENESIS, None, False
    for i, r in enumerate(records):
        if sealed:
            raise LedgerError("record %d comes after the seal" % i)
        if r.get("schema") != SCHEMA:
            raise LedgerError("record %d has schema %r" % (i, r.get("schema")))
        if r.get("seq") != i:
            raise LedgerError("record %d carries seq %r (deleted, inserted or reordered records)" % (i, r.get("seq")))
        if r.get("prev_hash") != prev:
            raise LedgerError("record %d does not link to the record before it" % i)
        if record_hash(r) != r.get("hash"):
            raise LedgerError("record %d was modified (its hash does not match its content)" % i)
        kind = r.get("type")
        if (i == 0) != (kind == "open"):
            raise LedgerError("record %d: the first record, and only it, is the open record" % i)
        if kind == "signal":
            if last_date is not None and r["decision_date"] <= last_date:
                raise LedgerError("record %d decides %s, not after %s" % (i, r["decision_date"], last_date))
            last_date = r["decision_date"]
        elif kind == "seal":
            if r.get("records") != i or r.get("head") != prev:
                raise LedgerError("the seal does not match the records before it")
            sealed = True
        elif kind != "open":
            raise LedgerError("record %d has unknown type %r" % (i, kind))
        prev = r["hash"]
    return sealed, prev


def _read(path):
    out = []
    with open(path, encoding="ascii") as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line:
                raise LedgerError("line %d is empty" % n)
            try:
                rec = json.loads(line)
            except ValueError:
                raise LedgerError("line %d is not JSON" % n)
            if canonical(rec) != line:
                raise LedgerError("line %d is not in canonical form" % n)
            out.append(rec)
    return out


class Ledger(object):
    """Append-only writer. Every open re-verifies the whole file."""

    def __init__(self, path):
        self.path = path
        self.records = _read(path)
        self.sealed, self.head = verify(self.records)

    @classmethod
    def create(cls, path, venue, strategy, isolation):
        if os.path.exists(path):
            raise LedgerError("%s exists; a ledger is never overwritten" % path)
        if isolation not in ("subprocess", "in-process"):
            raise LedgerError("isolation is subprocess or in-process")
        rec = {"schema": SCHEMA, "type": "open", "seq": 0, "venue": venue, "strategy": strategy,
               "isolation": isolation, "repro": repro_metadata(), "prev_hash": GENESIS}
        rec["hash"] = record_hash(rec)
        with open(path, "x", encoding="ascii") as fh:
            fh.write(canonical(rec) + "\n")
        return cls(path)

    def _append(self, rec):
        if self.sealed:
            raise LedgerError("the ledger is sealed; nothing can be appended")
        if self.records != _read(self.path) or verify(self.records) != verify(_read(self.path)):
            raise LedgerError("the ledger file changed since it was opened")
        rec.update({"schema": SCHEMA, "seq": len(self.records), "prev_hash": self.head})
        rec["hash"] = record_hash(rec)
        verify(self.records + [rec])
        with open(self.path, "a", encoding="ascii") as fh:
            fh.write(canonical(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.records.append(rec)
        self.sealed, self.head = verify(self.records)
        return rec

    def append_signal(self, inp, targets, strategy):
        last = [r for r in self.records if r["type"] == "signal"]
        if last and inp.as_of <= last[-1]["decision_date"]:
            raise LedgerError("%s is not after the last decision date %s" % (inp.as_of, last[-1]["decision_date"]))
        if strategy != {k: self.records[0]["strategy"][k] for k in ("name", "version")}:
            raise LedgerError("the signal's strategy is not the ledger's strategy")
        return self._append({"type": "signal", "decision_date": inp.as_of, "venue": inp.venue,
                             "eligible": list(inp.eligible), "targets": targets, "strategy": strategy,
                             "inputs": {"universe_digest": inp.universe_digest, "market_digest": inp.market_digest,
                                        "lookback": inp._d["lookback"]}})

    def seal(self, now=None):
        stamp = now or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = self._append({"type": "seal", "records": len(self.records), "head": self.head, "sealed_at": stamp})
        return rec["hash"]


class SealedLedger(object):
    """What an evaluator may read: a verified, sealed ledger. Read-only copies."""

    def __init__(self, records):
        self._records = records

    def signals(self):
        return [json.loads(canonical(r)) for r in self._records if r["type"] == "signal"]

    @property
    def seal_hash(self):
        return self._records[-1]["hash"]

    @property
    def signal_head(self):
        return self._records[-1]["head"]


def open_sealed(path, expected_seal=None):
    """The only reader for evaluation. Refuses an unsealed or broken ledger, and a seal that differs from
    the one recorded outside the ledger when it was sealed."""
    records = _read(path)
    sealed, head = verify(records)
    if not sealed:
        raise LedgerError("the ledger is not sealed; no evaluation may read it")
    if expected_seal is not None and head != expected_seal:
        raise LedgerError("the seal %s is not the one recorded at sealing time (%s)" % (head, expected_seal))
    return SealedLedger(records)


def generate(market, universe, strategy_path, class_name, dates, ledger_path, isolation="subprocess"):
    """Decide on each date in order, appending one signal per date, then seal. Returns the seal hash.
    Outcomes are not computed here; nothing after a decision date reaches the strategy."""
    cls = load_strategy(strategy_path, class_name)
    ident = strategy_identity(cls, strategy_path)
    led = Ledger.create(ledger_path, market.venue, ident, isolation)
    for d in dates:
        snap = universe.decide(d)
        inp = StrategyInput.build(market.view(d), snap, market.digest_through(d), int(cls.LOOKBACK))
        raw = run_isolated(strategy_path, class_name, inp) if isolation == "subprocess" else run_in_process(cls, inp)
        led.append_signal(inp, validate_targets(raw, inp), {"name": ident["name"], "version": ident["version"]})
    return led.seal()
