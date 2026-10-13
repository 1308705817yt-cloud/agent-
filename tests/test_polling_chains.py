"""New counterexamples collected through the public recorder, not old labels."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

from agentdoctor.core import digest
from agentdoctor.detectors import detect, diagnose
from agentdoctor.runner import execute
from agentdoctor.traces import load_traces


COLLECT = """
import asyncio, importlib, inspect, sys
from pathlib import Path
from agentdoctor.traces import TraceRecorder
project, trace = map(Path, sys.argv[1:])
sys.path.insert(0, str(project))
recorder = TraceRecorder(trace, 'new-chain')
for module_name, functions in [('endpoint', ['query']), ('bridge', ['relay']), ('worker', ['sample']), ('controller', ['drive']), ('entry', ['run'])]:
    module = importlib.import_module(module_name)
    for function in functions:
        kind = 'tool' if function == 'query' else 'node'
        setattr(module, function, recorder.wrap(function, getattr(module, function), kind=kind,
                tool='query' if kind == 'tool' else None, source={'path': module_name + '.py', 'function': function}))
try:
    value = module.run()
    if inspect.isawaitable(value):
        value = asyncio.run(value)
except Exception:
    pass
"""


class PollingChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.files = {
            "limits.py": "ATTEMPTS = 4\n",
            "endpoint.py": "async def query():\n    return {'completed': False, 'status': 'pending'}\n",
            "bridge.py": "import endpoint as remote\nasync def relay():\n    return await remote.query()\n",
            "worker.py": "from bridge import relay as poll_once\nasync def sample():\n    return await poll_once()\n",
            "controller.py": "import worker\nfrom limits import ATTEMPTS as RETRY_BUDGET\nasync def drive(limit):\n    for attempt in range(RETRY_BUDGET):\n        result = await worker.sample()\n        if result['completed']:\n            break\n    return result\n",
            "entry.py": "import controller\nasync def run():\n    return await controller.drive(4)\n",
        }
        self.config = {"source_paths": ["*.py"], "policy": {"protected_paths": []}, "tools": {"query": {"input_schema": {"type": "object"}}},
                       "checks": {"loop_threshold": 3, "preserve_state_keys": []}}

    def collect(self):
        for path, text in self.files.items():
            (self.project / path).write_text(text, encoding="utf-8", newline="")
        # Each subcase records to a distinct file so evidence never accumulates.
        trace = self.project / ("trace-" + str(len(list(self.project.glob("trace-*")))) + ".jsonl")
        result = execute([sys.executable, "-c", COLLECT, str(self.project), str(trace)], self.project, 10)
        self.assertEqual(result["returncode"], 0, result)
        return load_traces(trace)

    def diagnosis(self, spans):
        return diagnose(detect(spans, self.config, self.project, {p: digest(t) for p, t in self.files.items()}), spans)

    def assert_loop(self, result):
        self.assertIn("possible_loop", {f["category"] for f in result["findings"]})
        self.assertFalse(result["observations"])

    def test_async_aliases_multiple_wrappers_and_imported_constant(self):
        spans = self.collect()
        result = self.diagnosis(spans)
        self.assertFalse(result["findings"])
        observation = result["observations"][0]
        self.assertEqual(observation["category"], "bounded_polling")
        self.assertEqual(set(observation["evidence"]), {f"new-chain/{s['span_id']}" for s in spans})
        edges = observation["details"]["call_chain"]
        self.assertTrue(all(e["observed_calls"] == e["expected_calls"] for e in edges))
        self.assertEqual([e["expected_calls"] for e in edges if e["basis"] == "exhausted_loop"], [4])

    def test_sync_chain_while_budget_from_recorded_input(self):
        self.files["controller.py"] = "import worker\ndef drive(limit):\n    count = 0\n    while count < limit:\n        result = worker.sample()\n        count += 1\n    return result\n"
        self.files = {p: t.replace("async def", "def").replace("await ", "") for p, t in self.files.items()}
        self.assertFalse(self.diagnosis(self.collect())["findings"])

    def test_each_nested_budget_must_match_its_own_children(self):
        self.files["worker.py"] = "from bridge import relay as poll_once\nasync def sample():\n    for round in range(2):\n        result = await poll_once()\n    return result\n"
        spans = self.collect()
        result = self.diagnosis(spans)
        self.assertFalse(result["findings"])
        loops = [e for e in result["observations"][0]["details"]["call_chain"] if e["basis"] == "exhausted_loop"]
        self.assertEqual(sorted(e["expected_calls"] for e in loops), [2, 2, 2, 2, 4])
        # Total call count unchanged, but a leaf is attached to the wrong relay.
        wrong = copy.deepcopy(spans)
        leaves = [s for s in wrong if s["kind"] == "tool"]
        leaves[0]["parent_id"] = leaves[1]["parent_id"]
        self.assert_loop(self.diagnosis(wrong))

    def test_done_signal_continued_across_parents_is_never_suppressed(self):
        self.files["controller.py"] = self.files["controller.py"].replace("        if result['completed']:\n            break\n", "")
        self.files["endpoint.py"] = self.files["endpoint.py"].replace("False", "True").replace("'pending'", "'completed'")
        self.assert_loop(self.diagnosis(self.collect()))

    def test_unbounded_or_reset_outer_loop_is_not_proven_by_inner_budget(self):
        # A separate attempt guard lets the test process finish; it does not
        # make a reset/conditional counter into a valid supported proof.
        self.files["worker.py"] = "from bridge import relay as poll_once\nasync def sample():\n    for i in range(1):\n        result = await poll_once()\n    return result\n"
        for counter in ("turns = 0", "if result['completed']:\n            turns += 1"):
            self.files["controller.py"] = "import worker\nasync def drive(limit):\n    turns = 0\n    observed = 0\n    while turns < limit:\n        result = await worker.sample()\n        " + counter + "\n        observed += 1\n        if observed == 4:\n            return result\n"
            with self.subTest(counter=counter):
                self.assert_loop(self.diagnosis(self.collect()))

    def test_trace_order_is_irrelevant_but_missing_or_failed_ancestors_are_not(self):
        original = self.collect()
        self.assertFalse(self.diagnosis(list(reversed(original)))["findings"])
        for variant in ("missing_middle", "missing_root", "error", "failed_assertion", "unknown_output", "cycle", "wrong_source", "unobserved_parent"):
            spans = copy.deepcopy(original)
            middle = next(s for s in spans if s["node"] == "sample")
            if variant == "missing_middle":
                spans.remove(middle)
            elif variant == "missing_root":
                spans = [s for s in spans if s["node"] != "run"]
            elif variant == "error":
                middle["error"] = "TimeoutError"
            elif variant == "failed_assertion":
                middle["assertions"] = [{"name": "completed on time", "passed": False}]
            elif variant == "unknown_output":
                next(s for s in spans if s["kind"] == "tool")["output"] = {"answer": 0}
            elif variant == "cycle":
                middle["parent_id"] = middle["span_id"]
            elif variant == "wrong_source":
                middle["source"]["function"] = "relay"
            else:
                next(s for s in spans if s["node"] == "run")["parent_id"] = "missing"
            with self.subTest(variant=variant):
                self.assert_loop(self.diagnosis(spans))

    def test_separate_root_invocations_cannot_borrow_each_others_budget(self):
        spans = self.collect()
        duplicate = copy.deepcopy(spans)
        for span in duplicate:
            span["span_id"] += "-second"
            if span.get("parent_id"):
                span["parent_id"] += "-second"
        self.assert_loop(self.diagnosis(spans + duplicate))

    def test_mixed_runs_do_not_share_parent_ids_or_counts(self):
        spans = self.collect()
        duplicate = copy.deepcopy(spans)
        for span in duplicate:
            span["run_id"] = "another-run"
        result = self.diagnosis(spans + duplicate)
        self.assertFalse(result["findings"])
        self.assertEqual(len(result["observations"]), 2)

    def test_partial_or_extra_invocations_cannot_match_only_a_total(self):
        original = self.collect()
        for variant in ("partial", "extra"):
            spans = copy.deepcopy(original)
            leaf = next(s for s in spans if s["kind"] == "tool")
            if variant == "partial":
                spans.remove(leaf)
            else:
                spans.append({**leaf, "span_id": "extra"})
            with self.subTest(variant=variant):
                self.assert_loop(self.diagnosis(spans))

    def test_mutated_or_ambiguous_constants_do_not_establish_a_budget(self):
        for code in ("ATTEMPTS = 2\nATTEMPTS += 2\n", "ATTEMPTS = 9\nif True:\n    ATTEMPTS = 4\n"):
            self.files["limits.py"] = code
            with self.subTest(code=code):
                self.assert_loop(self.diagnosis(self.collect()))

    def test_imported_modules_are_not_executed_during_detection(self):
        spans = self.collect()
        # A fresh process already collected the trace. Analysis must only read
        # the module; importing this source would write the sentinel or fail.
        source = self.files["limits.py"] + "\nraise RuntimeError('never execute analysis inputs')\n"
        self.files["limits.py"] = source
        (self.project / "limits.py").write_text(source, encoding="utf-8")
        self.assertFalse(self.diagnosis(spans)["findings"])
