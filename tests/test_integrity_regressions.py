import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdoctor.core import DoctorError, write_json
from agentdoctor.engine import run
from agentdoctor.patches import apply_proposal
from agentdoctor.project import check_manifest, editable, load_config, snapshot
from agentdoctor.runner import ValidationRunner, candidate_gate, execute
from agentdoctor.traces import TraceRecorder, load_traces

FIXTURE = Path(__file__).resolve().parents[1] / "agentdoctor" / "demo_project"


class RecorderArgumentRegressions(unittest.TestCase):
    def exercise(self, function, args, kwargs, asynchronous=False):
        with tempfile.TemporaryDirectory() as folder:
            recorder = TraceRecorder(Path(folder) / "trace.jsonl", "argument-error")
            wrapped = recorder.wrap("lookup", function, kind="tool", tool="lookup")
            with self.assertRaises(TypeError) as original:
                function(*args, **kwargs) if not asynchronous else asyncio.run(function(*args, **kwargs))
            with self.assertRaises(TypeError) as observed:
                wrapped(*args, **kwargs) if not asynchronous else asyncio.run(wrapped(*args, **kwargs))
            self.assertEqual(str(observed.exception), str(original.exception))
            self.assertTrue(recorder.path.exists(), "Parameter binding failure must produce a trace")
            spans = load_traces(recorder.path)
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0]["metadata"]["call"], {"args": list(args), "kwargs": kwargs})
            self.assertIn("TypeError", spans[0]["error"])
            self.assertIsNone(recorder._parent.get())
            return spans[0]

    def test_wrong_keyword_records_the_actual_tool_input(self):
        def lookup(customer_id):
            return customer_id
        span = self.exercise(lookup, (), {"user_id": "C001"})
        self.assertEqual(span["input"], {"user_id": "C001"})

    def test_missing_and_extra_positional_and_duplicate_arguments(self):
        def lookup(customer_id):
            return customer_id
        for args, kwargs in [((), {}), (("C001", "extra"), {}), (("C001",), {"customer_id": "C002"})]:
            with self.subTest(args=args, kwargs=kwargs):
                self.exercise(lookup, args, kwargs)

    def test_async_wrong_keyword_is_also_recorded(self):
        async def lookup(customer_id):
            return customer_id
        self.exercise(lookup, (), {"user_id": "C001"}, asynchronous=True)

    def test_failed_child_call_preserves_parent_link_and_next_call_context(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = TraceRecorder(Path(folder) / "trace.jsonl", "nested")
            child = recorder.wrap("child", lambda customer_id: customer_id)
            def parent():
                try:
                    child(user_id="C001")
                except TypeError:
                    pass
                return child(customer_id="C002")
            self.assertEqual(recorder.wrap("parent", parent)(), "C002")
            spans = load_traces(recorder.path)
            self.assertEqual(len(spans), 3)
            self.assertEqual(spans[0]["parent_id"], spans[2]["span_id"])
            self.assertEqual(spans[1]["parent_id"], spans[2]["span_id"])


class ProjectIntegrityRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        snapshot(FIXTURE, self.project)
        recorded = execute([sys.executable, "record_trace.py"], self.project, 10)
        self.assertEqual(recorded["returncode"], 0, recorded)
        self.manifest = snapshot(self.project, self.root / "baseline")

    def tearDown(self):
        self.temp.cleanup()

    def test_addition_deletion_and_modification_are_all_reported(self):
        (self.project / "new_module.py").write_text("value = 1\n")
        (self.project / "agent.py").write_text("value = 2\n")
        (self.project / "record_trace.py").unlink()
        self.assertEqual(check_manifest(self.project, self.manifest), ["agent.py", "new_module.py", "record_trace.py"])

    def test_snapshot_output_exclusion_does_not_hide_sibling_additions(self):
        output = self.project / "reports"
        manifest = snapshot(self.project, output / "snapshot", excluded=[output])
        write_json(output / "report.json", {"status": "running"})
        self.assertEqual(check_manifest(self.project, manifest, excluded=[output]), [])
        (self.project / "new_module.py").write_text("value=1")
        self.assertEqual(check_manifest(self.project, manifest, excluded=[output]), ["new_module.py"])

    def test_validation_file_addition_stops_before_next_command(self):
        config = load_config(self.project)
        config["validation"]["commands"] = [
            {"name": "mutator", "kind": "target", "argv": ["{python}", "-c", "from pathlib import Path; Path('injected.py').write_text('value=1')"], "timeout_seconds": 10},
            {"name": "later", "kind": "regression", "argv": ["{python}", "-c", "raise RuntimeError('must not run')"], "timeout_seconds": 10},
        ]
        suite = ValidationRunner("local").run_suite(self.project, config, self.manifest)
        self.assertEqual(suite["integrity_changes"], ["injected.py"])
        self.assertEqual(len(suite["commands"]), 1)

    def test_added_original_file_invalidates_verified_delivery(self):
        original = ValidationRunner.run_suite
        def add_file(runner, project, config, manifest):
            result = original(runner, project, config, manifest)
            if project.name.startswith("candidate-"):
                (self.project / "new_module.py").write_text("new=1")
            return result
        with patch.object(ValidationRunner, "run_suite", add_file):
            report = run(self.project, self.project / "trace.jsonl", mode="repair", executor="local", output=self.root / "runs")
        self.assertEqual(report["status"], "inconclusive")
        self.assertIsNone(report["verified_patch"])

    def configure_module(self, module, files):
        for relative in files:
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("raise RuntimeError('module must not be imported during configuration')\n")
        config = json.loads((self.project / "doctor.json").read_text())
        config["source_paths"] = ["**"]
        config["validation"]["commands"][0]["argv"] = ["{python}", "-m", module]
        write_json(self.project / "doctor.json", config)
        return load_config(self.project)

    def test_module_evaluator_is_automatically_protected(self):
        config = self.configure_module("evaluator", ["evaluator.py"])
        self.assertFalse(editable("evaluator.py", config))
        manifest = snapshot(self.project, self.root / "module-snapshot")
        proposal = {"summary": "Bypass evaluator", "finding_ids": ["f1"], "edits": [{"path": "evaluator.py", "before": "raise RuntimeError", "after": "print", "reason": "bad patch"}]}
        with self.assertRaisesRegex(DoctorError, "protected"):
            apply_proposal(self.project, proposal, config, {"f1"}, manifest)

    def test_package_module_and_ancestor_initializers_are_protected(self):
        files = ["quality/__init__.py", "quality/evaluator/__init__.py", "quality/evaluator/__main__.py", "quality/evaluator/assertions.py"]
        config = self.configure_module("quality.evaluator", files)
        self.assertTrue(all(not editable(p, config) for p in files))
        self.assertTrue(editable("agent.py", config))

    def test_src_layout_module_is_protected_without_importing(self):
        config = self.configure_module("evaluator", ["src/evaluator.py"])
        self.assertFalse(editable("src/evaluator.py", config))

    def test_unittest_dotted_evaluator_selector_is_protected(self):
        self.configure_module("unittest", ["quality/__init__.py", "quality/evaluator.py"])
        config = json.loads((self.project / "doctor.json").read_text())
        config["validation"]["commands"][0]["argv"].append("quality.evaluator.Case.test_something")
        write_json(self.project / "doctor.json", config)
        protected = load_config(self.project)
        self.assertFalse(editable("quality/evaluator.py", protected))
        self.assertFalse(editable("quality/__init__.py", protected))
