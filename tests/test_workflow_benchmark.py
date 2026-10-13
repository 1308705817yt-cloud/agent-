import json
import sys
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import model_context
from agentdoctor.benchmark import _heldout, _suite_pass
from agentdoctor.core import digest, read_json
from agentdoctor.detectors import detect, diagnose
from agentdoctor.evidence import command_completed
from agentdoctor.project import load_config, snapshot
from agentdoctor.runner import ValidationRunner, execute
from agentdoctor.traces import load_traces

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agentdoctor/workflow_data"


class WorkflowBenchmarkTest(unittest.TestCase):
    def test_frozen_suite_and_reference_edits_are_separate_from_repair_projects(self):
        lock = read_json(ROOT / "benchmarks/workflow-v1.lock.json")
        with tempfile.TemporaryDirectory() as folder:
            files = snapshot(DATA, Path(folder) / "data")
        self.assertEqual(files, lock["files"])
        self.assertEqual(digest(json.dumps(files, sort_keys=True)), lock["dataset_hash"])
        suite = read_json(DATA / "suite.json")
        self.assertEqual([c["id"] for c in suite["cases"]], lock["case_ids"])
        self.assertEqual(len(suite["cases"]), 8)
        self.assertEqual(sum(bool(c["expected"]) for c in suite["cases"]), 4)
        for case in suite["cases"]:
            project = DATA / "cases" / case["id"]
            self.assertFalse((project / "suite.json").exists())
            self.assertFalse((project / "workflow-v1.reference-patches.json").exists())

    def test_all_baselines_collect_real_traces_and_fit_dependency_context(self):
        runner = ValidationRunner("local")
        suite = read_json(DATA / "suite.json")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for case in suite["cases"]:
                with self.subTest(case=case["id"]):
                    project = root / case["id"]
                    manifest = snapshot(DATA / "cases" / case["id"], project)
                    trace = root / (case["id"] + ".jsonl")
                    collected = execute([sys.executable, "-m", "agentdoctor.benchmark_collect", str(project), str(trace)], project, 15)
                    self.assertEqual(collected["returncode"], 0, collected)
                    spans = load_traces(trace)
                    self.assertTrue(spans)
                    config = load_config(project)
                    diagnosis = diagnose(detect(spans, config, project, manifest), spans)
                    context = model_context(project, manifest, spans, diagnosis, config, [])
                    self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 24000)
                    self.assertTrue(context["dependency_context"]["resolved"])
                    text = "\n".join(s["text"] for s in context["sources"])
                    constant = {"dependency_contract": "RATE = 7", "async_state_merge": "UNIT_VALUE = 9",
                                "async_fanout": "MULTIPLIER = 2", "nested_retry": "MAX_ATTEMPTS = 5"}[case["family"]]
                    self.assertIn(constant, text)
                    if case["family"] == "nested_retry":
                        self.assertIn("async def execute", text)
                    baseline = _heldout(project, root / (case["id"] + "-oracle"), case["heldout"], runner)
                    self.assertTrue(all(command_completed(r) for r in baseline["commands"]), baseline)
                    self.assertTrue(_suite_pass(baseline, "regression"))
                    self.assertEqual(_suite_pass(baseline, "target"), not bool(case["expected"]))

    def test_reference_repairs_pass_visible_and_independent_checks(self):
        runner = ValidationRunner("local")
        references = read_json(ROOT / "benchmarks/workflow-v1.reference-patches.json")
        suite = read_json(DATA / "suite.json")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for case in suite["cases"]:
                if case["id"] not in references:
                    continue
                with self.subTest(case=case["id"]):
                    project = root / case["id"]
                    manifest = snapshot(DATA / "cases" / case["id"], project)
                    edit = references[case["id"]]
                    path = project / edit["path"]
                    source = path.read_text(encoding="utf-8")
                    self.assertEqual(source.count(edit["before"]), 1)
                    path.write_text(source.replace(edit["before"], edit["after"]), encoding="utf-8", newline="")
                    manifest[edit["path"]] = digest(path.read_bytes())
                    visible = runner.run_suite(project, load_config(project), manifest)
                    independent = _heldout(project, root / (case["id"] + "-oracle"), case["heldout"], runner)
                    for result in (visible, independent):
                        self.assertTrue(_suite_pass(result, "target"), result)
                        self.assertTrue(_suite_pass(result, "regression"), result)

    def test_independent_oracle_catches_system_exit_while_awaiting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            (project / "subject.py").write_text("async def run():\n    raise SystemExit(0)\n\ndef stable():\n    return 1\n")
            spec = {"target": [{"function": "run", "kwargs": {}, "expected": 1}],
                    "regression": [{"function": "stable", "kwargs": {}, "expected": 1}]}
            result = _heldout(project, root / "oracle", spec, ValidationRunner("local"))
            self.assertTrue(all(command_completed(r) for r in result["commands"]))
            self.assertFalse(_suite_pass(result, "target"))
            self.assertTrue(_suite_pass(result, "regression"))
