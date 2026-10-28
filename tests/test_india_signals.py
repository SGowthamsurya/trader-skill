"""Strategy interface and signal ledger mechanics. SYNTHETIC DATA ONLY: the market, the
evidence and the dummy strategies are made up; nothing here is a trading result."""

import json
import os
import re
import tempfile
import unittest

import _india_fixtures as fx
import pit  # noqa: E402
import signals as S  # noqa: E402
import universe as U  # noqa: E402
from test_india_universe import A, B, DOC, market, etf_row, rules

DUMMY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_india_dummy_strategy.py")
DATES = ["2024-01-19", "2024-01-26", "2024-02-02"]
SEAL_TIME = "2026-10-28T00:00:00Z"


def two_etfs(after=None):
    def rows(d):
        r = {"590901": etf_row(d), "590902": etf_row(d, "590902", B)}
        return after(d, r) if after and d > "2024-01-19" else r
    return market(rows)


def evidence():
    acts = [{"id": "syn-review-%s" % k, "exchange": "BSE", "key": k, "type": "distributions-reviewed",
             "verification": "verified", "documents": [DOC], "covers_from": "2023-01-01", "covers_to": "2099-12-31",
             "distributions": []} for k in ("590901", "590902")]
    return U.Evidence({A: [("nifty50", "2024-01-01", "syn")], B: [("niftynext50", "2024-01-01", "syn")]}, acts, [])


def universe(data):
    return U.Universe(data, evidence(), rules())


def make_input(data, t="2024-01-19", lookback=3):
    return S.StrategyInput.build(data.view(t), universe(data).decide(t), data.digest_through(t), lookback)


class Ledgered(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "signals.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_ledger(self, path=None, isolation="in-process", cls="DummyEqualWeight", seal=True):
        data = two_etfs()
        led = S.Ledger.create(path or self.path, "BSE", S.strategy_identity(
            S.load_strategy(DUMMY, cls), DUMMY), isolation)
        strat = S.load_strategy(DUMMY, cls)
        for d in DATES:
            inp = make_input(data, d)
            raw = S.run_isolated(DUMMY, cls, inp) if isolation == "subprocess" else S.run_in_process(strat, inp)
            led.append_signal(inp, S.validate_targets(raw, inp), {"name": strat.NAME, "version": strat.VERSION})
        return led.seal(SEAL_TIME) if seal else led

    def lines(self):
        with open(self.path) as fh:
            return fh.read().splitlines()

    def write(self, lines):
        with open(self.path, "w") as fh:
            fh.write("\n".join(lines) + "\n")


class Serialization(unittest.TestCase):
    def test_canonical_json_is_deterministic(self):
        self.assertEqual(S.canonical({"b": 1, "a": [2, {"y": "x", "x": "y"}]}),
                         S.canonical({"a": [2, {"x": "y", "y": "x"}], "b": 1}))
        self.assertEqual(S.canonical({"k": "₹"}), '{"k":"\\u20b9"}')

    def test_floats_are_refused_anywhere_in_a_record(self):
        for bad in ({"w": 0.5}, {"t": [{"w": 1e-7}]}, {"x": float("nan")}):
            with self.assertRaises(S.SignalError):
                S.canonical(bad)

    def test_the_hash_covers_everything_but_itself(self):
        r = {"a": 1, "b": "x"}
        h = S.record_hash(r)
        self.assertEqual(S.record_hash(dict(r, hash="anything")), h)
        self.assertNotEqual(S.record_hash(dict(r, b="y")), h)


class Chain(Ledgered):
    def test_a_sealed_ledger_verifies_and_opens_for_evaluation(self):
        seal = self.run_ledger()
        led = S.open_sealed(self.path, expected_seal=seal)
        self.assertEqual([s["decision_date"] for s in led.signals()], DATES)
        self.assertEqual(led.seal_hash, seal)
        first = led.signals()[0]
        self.assertEqual([t["weight"] for t in first["targets"]], ["0.500000", "0.500000"])
        self.assertEqual(first["eligible"], ["590901", "590902"])
        self.assertRegex(first["inputs"]["universe_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(first["inputs"]["market_digest"], r"^[0-9a-f]{64}$")

    def test_the_open_record_carries_reproducibility_metadata(self):
        self.run_ledger()
        head = json.loads(self.lines()[0])
        self.assertEqual(head["type"], "open")
        self.assertIn("universe.py", head["repro"]["files"])
        self.assertRegex(head["strategy"]["code_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("MECHANICS-TEST-ONLY", head["strategy"]["name"])

    def test_two_runs_write_byte_identical_ledgers(self):
        other = os.path.join(self.tmp.name, "again.jsonl")
        self.assertEqual(self.run_ledger(), self.run_ledger(other))
        with open(other) as fh:
            self.assertEqual(fh.read().splitlines(), self.lines())

    def test_isolated_and_in_process_runs_give_the_same_signals(self):
        self.run_ledger(isolation="in-process")
        other = os.path.join(self.tmp.name, "isolated.jsonl")
        self.run_ledger(other, isolation="subprocess")
        strip = lambda s: {k: v for k, v in s.items() if k not in ("hash", "prev_hash")}  # noqa: E731
        a = [strip(s) for s in S.open_sealed(self.path).signals()]
        b = [strip(s) for s in S.open_sealed(other).signals()]
        self.assertEqual(a, b)
        with open(other) as fh:
            self.assertEqual(json.loads(fh.readline())["isolation"], "subprocess")


class Tampering(Ledgered):
    def test_a_modified_record_is_detected(self):
        self.run_ledger()
        lines = self.lines()
        r = json.loads(lines[2])
        r["targets"][0]["weight"] = "0.900000"
        lines[2] = S.canonical(r)
        self.write(lines)
        with self.assertRaisesRegex(S.LedgerError, "record 2 was modified"):
            S.open_sealed(self.path)

    def test_a_modified_record_with_a_recomputed_hash_breaks_the_next_link(self):
        self.run_ledger()
        lines = self.lines()
        r = json.loads(lines[2])
        r["targets"][0]["weight"] = "0.900000"
        r["hash"] = S.record_hash(r)
        lines[2] = S.canonical(r)
        self.write(lines)
        with self.assertRaisesRegex(S.LedgerError, "record 3 does not link"):
            S.open_sealed(self.path)

    def test_a_deleted_record_is_detected(self):
        self.run_ledger()
        lines = self.lines()
        del lines[2]
        self.write(lines)
        with self.assertRaisesRegex(S.LedgerError, "seq"):
            S.open_sealed(self.path)

    def test_reordered_records_are_detected(self):
        self.run_ledger()
        lines = self.lines()
        lines[1], lines[2] = lines[2], lines[1]
        self.write(lines)
        with self.assertRaisesRegex(S.LedgerError, "seq"):
            S.open_sealed(self.path)

    def test_dropping_the_seal_makes_it_unreadable_for_evaluation(self):
        self.run_ledger()
        self.write(self.lines()[:-1])
        with self.assertRaisesRegex(S.LedgerError, "not sealed"):
            S.open_sealed(self.path)

    def test_a_non_canonical_line_is_refused(self):
        self.run_ledger()
        lines = self.lines()
        lines[1] = json.dumps(json.loads(lines[1]), indent=1).replace("\n", " ")
        self.write(lines)
        with self.assertRaisesRegex(S.LedgerError, "canonical"):
            S.open_sealed(self.path)

    def test_a_rewritten_chain_is_consistent_but_the_external_seal_catches_it(self):
        # the documented limit: rewriting every record and hash yields a self-consistent chain
        seal = self.run_ledger()
        recs = [json.loads(x) for x in self.lines()]
        recs[1]["targets"][0]["weight"] = "0.100000"
        prev = S.GENESIS
        for r in recs:
            r["prev_hash"] = prev
            if r["type"] == "seal":
                r["head"] = prev
            r["hash"] = S.record_hash(r)
            prev = r["hash"]
        self.write([S.canonical(r) for r in recs])
        S.open_sealed(self.path)                                  # the chain alone cannot tell
        with self.assertRaisesRegex(S.LedgerError, "not the one recorded at sealing time"):
            S.open_sealed(self.path, expected_seal=seal)


class AppendRules(Ledgered):
    def test_an_unsealed_ledger_is_refused_for_evaluation(self):
        self.run_ledger(seal=False)
        with self.assertRaisesRegex(S.LedgerError, "not sealed"):
            S.open_sealed(self.path)

    def test_nothing_can_be_appended_after_the_seal(self):
        self.run_ledger()
        led = S.Ledger(self.path)
        with self.assertRaisesRegex(S.LedgerError, "sealed"):
            led.seal(SEAL_TIME)

    def test_duplicate_or_earlier_decision_dates_are_refused(self):
        led = self.run_ledger(seal=False)
        data = two_etfs()
        ident = {"name": "dummy-equal-weight-MECHANICS-TEST-ONLY", "version": "0"}
        for d in ("2024-02-02", "2024-01-26"):
            inp = make_input(data, d)
            with self.assertRaisesRegex(S.LedgerError, "not after the last decision date"):
                led.append_signal(inp, S.validate_targets({}, inp), ident)

    def test_an_existing_ledger_is_never_overwritten(self):
        self.run_ledger()
        with self.assertRaisesRegex(S.LedgerError, "never overwritten"):
            S.Ledger.create(self.path, "BSE", {"name": "x", "version": "0", "code_sha256": "0" * 64}, "in-process")

    def test_a_file_changed_behind_an_open_writer_is_refused(self):
        led = self.run_ledger(seal=False)
        lines = self.lines()
        self.write(lines[:-1])
        with self.assertRaisesRegex(S.LedgerError, "changed since it was opened"):
            led.seal(SEAL_TIME)

    def test_a_signal_from_another_strategy_is_refused(self):
        led = self.run_ledger(seal=False)
        inp = make_input(two_etfs(), "2024-02-09")
        with self.assertRaisesRegex(S.LedgerError, "not the ledger's strategy"):
            led.append_signal(inp, [], {"name": "other", "version": "0"})


class NoFutureInformation(unittest.TestCase):
    def test_the_input_holds_no_date_after_the_decision(self):
        inp = make_input(two_etfs(), "2024-01-19")
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", inp.to_json())
        self.assertTrue(dates)
        self.assertLessEqual(max(dates), "2024-01-19")

    def test_the_input_does_not_depend_on_later_data(self):
        later = two_etfs(after=lambda d, r: {"590901": etf_row(d, close=9999.0, value=1.0)})
        self.assertEqual(make_input(two_etfs()).to_json(), make_input(later).to_json())

    def test_the_input_exposes_no_view_universe_evidence_or_portfolio(self):
        inp = make_input(two_etfs())
        for name in ("view", "universe", "evidence", "market", "portfolio", "fills", "_view"):
            self.assertFalse(hasattr(inp, name), name)
        self.assertEqual(set(json.loads(inp.to_json())),
                         {"as_of", "venue", "eligible", "isins", "universe_digest", "market_digest", "lookback",
                          "sessions", "closes"})
        with self.assertRaises(AttributeError):
            inp.as_of = "2024-12-31"

    def test_only_eligible_keys_and_only_the_lookback_are_given(self):
        data = market(lambda d: {"590901": etf_row(d), "500001": etf_row(d, "500001", "INE000SYN099")})
        inp = make_input(data, "2024-01-19", lookback=3)
        self.assertEqual(inp.sessions, ("2024-01-17", "2024-01-18", "2024-01-19"))
        with self.assertRaises(KeyError):
            inp.closes("500001")
        self.assertEqual([d for d, _ in inp.closes("590901")], list(inp.sessions))

    def test_a_snapshot_for_another_day_is_refused(self):
        data = two_etfs()
        with self.assertRaises(S.SignalError):
            S.StrategyInput.build(data.view("2024-01-19"), universe(data).decide("2024-01-18"),
                                  data.digest_through("2024-01-19"), 3)

    def test_the_isolated_strategy_cannot_target_what_it_was_not_given(self):
        inp = make_input(two_etfs())
        raw = S.run_isolated(DUMMY, "PeeksPastLookback", inp)
        with self.assertRaisesRegex(S.SignalError, "not eligible"):
            S.validate_targets(raw, inp)

    def test_the_market_digest_is_past_only_and_stable(self):
        a, b = two_etfs(), two_etfs(after=lambda d, r: {})
        self.assertEqual(a.digest_through("2024-01-19"), b.digest_through("2024-01-19"))
        self.assertNotEqual(a.digest_through("2024-02-09"), b.digest_through("2024-02-09"))
        self.assertEqual(a.digest_through("2024-01-19"), a.digest_through("2024-01-19"))   # repeat calls agree


class Targets(unittest.TestCase):
    def test_validation(self):
        inp = make_input(two_etfs())
        self.assertEqual(S.validate_targets({"590902": "0.25", "590901": 0.5}, inp),
                         [{"key": "590901", "isin": A, "weight": "0.500000"},
                          {"key": "590902", "isin": B, "weight": "0.250000"}])
        self.assertEqual(S.validate_targets({"590901": "0"}, inp), [])
        for raw, why in (({"590901": "-0.1"}, "not negative"), ({"590901": "0.7", "590902": "0.4"}, "more than 1"),
                         ({"999999": "0.1"}, "not eligible"), ({"590901": "abc"}, "not a number"),
                         (["590901"], "must return"), ({"590901": "nan"}, "finite")):
            with self.assertRaisesRegex(S.SignalError, why):
                S.validate_targets(raw, inp)

    def test_an_overweight_strategy_is_stopped_before_the_ledger(self):
        inp = make_input(two_etfs())
        with self.assertRaisesRegex(S.SignalError, "more than 1"):
            S.validate_targets(S.run_in_process(S.load_strategy(DUMMY, "Overweight"), inp), inp)


class Generate(unittest.TestCase):
    def test_generate_writes_and_seals_one_signal_per_date(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "g.jsonl")
            data = two_etfs()
            seal = S.generate(data, universe(data), DUMMY, "DummyEqualWeight", DATES, path, isolation="subprocess")
            led = S.open_sealed(path, expected_seal=seal)
            self.assertEqual([s["decision_date"] for s in led.signals()], DATES)

    def test_generate_refuses_a_non_tradable_decision_date(self):
        with tempfile.TemporaryDirectory() as d:
            data = two_etfs()
            with self.assertRaises(pit.PitError):
                S.generate(data, universe(data), DUMMY, "DummyEqualWeight", ["2024-01-20"],
                           os.path.join(d, "g.jsonl"), isolation="in-process")


if __name__ == "__main__":
    unittest.main()
