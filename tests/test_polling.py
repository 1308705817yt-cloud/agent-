import copy
import tempfile
import unittest
from pathlib import Path

from agentdoctor.detectors import detect, diagnose
from agentdoctor.traces import TraceRecorder, load_traces


class BoundedPollingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.source = "def watch(limit):\n    turns = 0\n    while turns < limit:\n        status = fetch_status()\n        turns += 1\n    return status\n"
        self.config = {"tools": {"fetch_status": {"input_schema": {"type": "object"}}}, "checks": {"loop_threshold": 3, "preserve_state_keys": []}, "source_paths": ["*.py"], "policy": {"protected_paths": []}}
        self.parent = {"run_id": "job", "span_id": "parent", "kind": "node", "node": "watch", "input": {"limit": 3}, "output": {"done": False}, "source": {"path": "worker.py", "function": "watch"}}
        self.calls = [{"run_id": "job", "span_id": str(i), "parent_id": "parent", "node": "watch", "kind": "tool", "tool": "fetch_status", "input": {}, "output": {"done": False}, "source": self.parent["source"]} for i in range(3)]

    def diagnose(self):
        (self.project / "worker.py").write_text(self.source, encoding="utf-8")
        spans = self.calls + [self.parent]
        return diagnose(detect(spans, self.config, self.project, {"worker.py": "hash"}), spans)

    def test_completed_bounded_pending_poll_is_an_observation(self):
        result = self.diagnose()
        self.assertFalse(result["findings"])
        self.assertEqual(result["observations"][0]["category"], "bounded_polling")
        self.assertIn("job/parent", result["observations"][0]["evidence"])

    def test_range_budget_and_status_protocol(self):
        self.source = "def watch(limit):\n    for attempt in range(limit):\n        status = fetch_status()\n    return status\n"
        for s in self.calls:
            s["output"] = {"status": "pending"}
        self.assertFalse(self.diagnose()["findings"])

    def test_terminal_response_still_flags_missing_stop(self):
        for s in self.calls:
            s["output"] = {"done": True}
        self.assertTrue(any(f["category"] == "possible_loop" for f in self.diagnose()["findings"]))

    def test_recorder_detects_loop_across_repeated_node_invocations(self):
        recorder = TraceRecorder(self.project / "trace.jsonl", run_id="graph")
        tool = recorder.wrap("watch", lambda: {"done": True}, kind="tool", tool="fetch_status")
        node = recorder.wrap("step", lambda: tool())
        for _ in range(4):
            node()
        spans = load_traces(recorder.path)
        calls = [s for s in spans if s["kind"] == "tool"]
        self.assertEqual(len({s["parent_id"] for s in calls}), 4)
        issues = detect(spans, self.config, self.project, {})
        loops = [f for f in issues if f["category"] == "possible_loop"]
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["evidence"], [f"graph/{s['span_id']}" for s in calls[:3]])

    def test_local_bounded_polls_do_not_prove_outer_loop_is_bounded(self):
        self.source = "def watch(limit):\n    for attempt in range(limit):\n        status = fetch_status()\n    return status\n"
        (self.project / "worker.py").write_text(self.source, encoding="utf-8")
        spans = []
        for i in range(4):
            parent = {**copy.deepcopy(self.parent), "span_id": f"parent-{i}", "input": {"limit": 1}}
            spans += [{**copy.deepcopy(self.calls[0]), "span_id": f"call-{i}", "parent_id": parent["span_id"]}, parent]
        result = diagnose(detect(spans, self.config, self.project, {"worker.py": "hash"}), spans)
        self.assertEqual([f["category"] for f in result["findings"]], ["possible_loop"])
        self.assertFalse(result["observations"])

    def test_unbounded_reset_or_conditional_counter_does_not_suppress_alarm(self):
        for source in (self.source.replace("turns < limit", "True"),
                       self.source.replace("turns += 1", "turns = 0"),
                       self.source.replace("turns += 1", "if status['done']:\n            turns += 1")):
            with self.subTest(source=source):
                self.source = source
                self.assertTrue(any(f["category"] == "possible_loop" for f in self.diagnose()["findings"]))

    def test_partial_trace_failure_and_unknown_status_remain_alerts(self):
        base_calls, base_parent = copy.deepcopy(self.calls), copy.deepcopy(self.parent)
        for variant in ("partial", "parent_error", "assertion", "unknown", "tool_error", "no_parent"):
            self.calls, self.parent = copy.deepcopy(base_calls), copy.deepcopy(base_parent)
            if variant == "partial":
                self.parent["input"]["limit"] = 8
            elif variant == "parent_error":
                self.parent["error"] = "TimeoutError"
            elif variant == "assertion":
                self.parent["assertions"] = [{"name": "business outcome", "passed": False}]
            elif variant == "unknown":
                for s in self.calls:
                    s["output"] = {"value": 0}
            elif variant == "tool_error":
                for s in self.calls:
                    s["error"] = "TimeoutError"
            else:
                for s in self.calls:
                    s.pop("parent_id")
            with self.subTest(variant=variant):
                self.assertTrue(any(f["category"] == "possible_loop" for f in self.diagnose()["findings"]))
