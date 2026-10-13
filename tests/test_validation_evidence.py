import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdoctor.benchmark import _heldout, _suite_pass, run_benchmark
from agentdoctor.core import digest, read_json, write_json
from agentdoctor.engine import run
from agentdoctor.evidence import command_completed, command_passed
from agentdoctor.project import load_config, snapshot
from agentdoctor.runner import ValidationRunner, baseline_gate, candidate_gate, execute

DATA = Path(__file__).resolve().parents[1] / "agentdoctor" / "benchmark_data"


class EarlyExitRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.manifest = snapshot(DATA / "cases" / "argument_alias", self.project)
        self.spec = next(c["heldout"] for c in read_json(DATA / "suite.json")["cases"] if c["id"] == "argument_alias")
        self.runner = ValidationRunner("local")

    def tearDown(self):
        self.temporary.cleanup()

    def candidate(self, text):
        (self.project / "subject.py").write_text(text, encoding="utf-8")
        self.manifest["subject.py"] = digest((self.project / "subject.py").read_bytes())

    def test_preset_import_exit_patch_cannot_be_verified(self):
        trace = self.root / "trace.jsonl"
        collected = execute([sys.executable, "-m", "agentdoctor.benchmark_collect", str(self.project), str(trace)], self.project, 15)
        self.assertEqual(collected["returncode"], 0)
        original = (self.project / "subject.py").read_text(encoding="utf-8")
        def preset(project, spans, findings, config):
            return [{"summary": "Premature exit probe", "finding_ids": [findings[0]["id"]],
                     "edits": [{"path": "subject.py", "before": original, "after": "raise SystemExit(0)\n" + original, "reason": "Probe"}]}]
        with patch("agentdoctor.engine.alias_proposals", side_effect=preset):
            report = run(self.project, trace, mode="repair", executor="local", output=self.root / "runs")
        self.assertEqual(report["status"], "rejected", report["reason"])
        self.assertIsNone(report["verified_patch"])
        self.assertEqual(len(report["attempts"]), 1)

    def test_import_exit_fails_both_development_and_independent_checks(self):
        self.candidate("raise SystemExit(0)\n")
        development = self.runner.run_suite(self.project, load_config(self.project), self.manifest)
        heldout = _heldout(self.project, self.root / "heldout", self.spec, self.runner)
        for suite in (development, heldout):
            self.assertFalse(_suite_pass(suite, "target"))
            self.assertFalse(_suite_pass(suite, "regression"))
            self.assertTrue(all(row["returncode"] != 0 for row in suite["commands"]))
            self.assertTrue(all(row["completion"]["executed_count"] == 0 for row in suite["commands"]))

    def test_call_exit_fails_both_development_and_independent_checks(self):
        self.candidate("def run(quantity):\n    raise SystemExit(0)\n\ndef stable(value):\n    return value + 10\n")
        development = self.runner.run_suite(self.project, load_config(self.project), self.manifest)
        heldout = _heldout(self.project, self.root / "heldout", self.spec, self.runner)
        for suite in (development, heldout):
            self.assertFalse(_suite_pass(suite, "target"))
            self.assertTrue(_suite_pass(suite, "regression"))
            target = next(row for row in suite["commands"] if row["kind"] == "target")
            self.assertTrue(command_completed(target))
            self.assertTrue(all(r["status"] == "error" and "SystemExit" in r["detail"] for r in target["completion"]["results"]))

    def test_hard_exit_without_any_results_fails_independent_checks(self):
        self.candidate("import os\nos._exit(0)\n")
        heldout = _heldout(self.project, self.root / "heldout", self.spec, self.runner)
        self.assertTrue(all(r["returncode"] == 0 for r in heldout["commands"]))
        self.assertFalse(_suite_pass(heldout, "target"))
        self.assertFalse(_suite_pass(heldout, "regression"))

    def test_hard_exit_after_one_case_cannot_satisfy_remaining_cases(self):
        self.candidate("import os\ndef run(quantity):\n    if quantity == 11:\n        os._exit(0)\n    return quantity * 7\n\ndef stable(value):\n    return value + 10\n")
        heldout = _heldout(self.project, self.root / "heldout", self.spec, self.runner)
        self.assertEqual(heldout["commands"][0]["returncode"], 0)
        self.assertFalse(_suite_pass(heldout, "target"))
        self.assertTrue(_suite_pass(heldout, "regression"))

    def test_real_repair_still_passes_with_all_expected_results(self):
        text = (self.project / "subject.py").read_text(encoding="utf-8").replace('"units"', '"quantity"')
        self.candidate(text)
        development = self.runner.run_suite(self.project, load_config(self.project), self.manifest)
        heldout = _heldout(self.project, self.root / "heldout", self.spec, self.runner)
        for suite in (development, heldout):
            self.assertTrue(_suite_pass(suite, "target"))
            self.assertTrue(_suite_pass(suite, "regression"))
            ids = [r["invocation_id"] for r in suite["commands"]]
            self.assertEqual(len(ids), len(set(ids)))
            for row in suite["commands"]:
                self.assertEqual(row["completion"]["executed_count"], len(row["expected_cases"]))

    def test_legacy_exit_code_only_config_is_inconclusive(self):
        config = read_json(self.project / "doctor.json")
        for command in config["validation"]["commands"]:
            command.pop("expected_cases")
        write_json(self.project / "doctor.json", config)
        trace = self.root / "trace.jsonl"
        collected = execute([sys.executable, "-m", "agentdoctor.benchmark_collect", str(self.project), str(trace)], self.project, 15)
        self.assertEqual(collected["returncode"], 0)
        report = run(self.project, trace, mode="repair", executor="local", output=self.root / "runs")
        self.assertEqual(report["status"], "inconclusive")
        self.assertIn("expected_cases", report["reason"])
        self.assertIsNone(report["baseline"])

    def test_independent_rejection_counts_as_false_verified_not_incomplete(self):
        suite = read_json(DATA / "suite.json")
        suite["cases"] = [suite["cases"][0]]
        def preset(project, spans, findings, config):
            original = (project / "subject.py").read_text(encoding="utf-8")
            return [{"summary": "Hard exit probe", "finding_ids": [findings[0]["id"]],
                     "edits": [{"path": "subject.py", "before": original,
                                "after": "import os\nos._exit(0)\n" + original, "reason": "Probe"}]}]
        def legacy_verified(*args, **kwargs):
            report = run(*args, **kwargs)
            self.assertEqual(report["status"], "rejected")
            report["status"] = "verified"  # Simulate an incorrect delivery by an older gate.
            report["attempts"][0]["status"] = "verified"
            return report
        with patch("agentdoctor.benchmark.read_json", return_value=suite), \
             patch("agentdoctor.engine.alias_proposals", side_effect=preset), \
             patch("agentdoctor.benchmark.run", side_effect=legacy_verified):
            report = run_benchmark(self.root / "benchmark")
        self.assertTrue(report["complete"])
        self.assertEqual(report["metrics"]["measured_cases"], 1)
        self.assertEqual(report["metrics"]["repair_success_rate"]["value"], 0)
        self.assertEqual(report["metrics"]["false_verified_rate"]["value"], 1)
        self.assertEqual(report["metrics"]["candidate_regression_failure_rate"]["value"], 1)


class CompletionProtocolRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.document = {"schema_version": 1, "invocation_id": "placeholder", "completed": True,
                         "executed_count": 2, "results": [{"id": "first", "status": "passed"}, {"id": "second", "status": "passed"}]}

    def tearDown(self):
        self.temporary.cleanup()

    def execute_report(self, document=None, *, code=0, replay=False, prefix="", suffix="", expected=None):
        document = copy.deepcopy(document if document is not None else self.document)
        source = "import json,os\nreport=" + repr(document) + "\n"
        if not replay:
            source += "report['invocation_id']=os.environ['AGENTDOCTOR_VALIDATION_ID']\n"
        source += prefix + "\nprint('AGENTDOCTOR_RESULT '+json.dumps(report))\n" + suffix + f"\nraise SystemExit({code})"
        command = {"name": "checks", "kind": "target", "argv": ["{python}", "-c", source], "timeout_seconds": 10,
                   "expected_cases": ["first", "second"] if expected is None else expected}
        suite = ValidationRunner("local").run_suite(self.project, {"validation": {"commands": [command], "repetitions": 1}}, {})
        return suite["commands"][0]

    def test_empty_missing_partial_duplicate_and_unknown_case_results_are_rejected(self):
        variants = [
            {"executed_count": 0, "results": []},
            {"executed_count": 1, "results": [{"id": "first", "status": "passed"}]},
            {"results": [{"id": "first", "status": "passed"}] * 2},
            {"results": [{"id": "first", "status": "passed"}, {"id": "unexpected", "status": "passed"}]},
            {"executed_count": 5}, {"completed": False},
            {"results": [{"id": "first", "status": "skipped"}, {"id": "second", "status": "passed"}]},
        ]
        for change in variants:
            with self.subTest(change=change):
                row = self.execute_report({**self.document, **change})
                self.assertEqual(row["returncode"], 0)
                self.assertFalse(command_passed(row))
                self.assertTrue(row["completion_error"])

    def test_zero_discovered_unittest_cases_cannot_pass(self):
        command = {"name": "empty", "kind": "target", "argv": ["{python}", "-m", "unittest", "discover"],
                   "timeout_seconds": 10, "expected_cases": ["required_test"]}
        suite = ValidationRunner("local").run_suite(self.project, {"validation": {"commands": [command], "repetitions": 1}}, {})
        row = suite["commands"][0]
        self.assertEqual(row["returncode"], 0)
        self.assertIn("Ran 0 tests", row["stderr"])
        self.assertFalse(command_passed(row))
        self.assertFalse(_suite_pass(suite, "target"))

    def test_empty_expected_cases_cannot_disable_coverage_check(self):
        row = self.execute_report({**self.document, "executed_count": 0, "results": []}, expected=[])
        self.assertFalse(command_passed(row))

    def test_replayed_completion_cannot_pass_a_new_invocation(self):
        previous = self.execute_report()
        self.assertTrue(command_passed(previous))
        replayed = self.execute_report(previous["completion"], replay=True)
        self.assertNotEqual(previous["invocation_id"], replayed["invocation_id"])
        self.assertFalse(command_passed(replayed))

    def test_exit_code_and_individual_results_must_agree(self):
        failed = copy.deepcopy(self.document)
        failed["results"][0]["status"] = "failed"
        self.assertFalse(command_completed(self.execute_report(failed, code=0)))
        self.assertFalse(command_completed(self.execute_report(code=1)))
        self.assertTrue(command_completed(self.execute_report(failed, code=1)))

    def test_duplicate_or_malformed_completion_record_cannot_pass(self):
        for extra in ("print('AGENTDOCTOR_RESULT '+json.dumps(report))", "print('AGENTDOCTOR_RESULT {bad json}')"):
            with self.subTest(extra=extra):
                self.assertFalse(command_passed(self.execute_report(suffix=extra)))

    def test_truncated_output_even_after_valid_report_cannot_pass(self):
        row = self.execute_report(suffix="print('x' * 100000)")
        self.assertTrue(row["stdout_truncated"])
        self.assertFalse(command_passed(row))

    def test_frozen_case_expectations_and_command_multiplicity_are_checked(self):
        from test_agentdoctor import RunnerTest
        baseline = RunnerTest.suite()
        self.assertTrue(baseline_gate(baseline)[0])
        candidate = RunnerTest.suite(target=0)
        row = candidate["commands"][0]
        row["expected_cases"] = ["replacement"]
        row["completion"]["results"][0]["id"] = "replacement"
        self.assertTrue(command_passed(row))
        self.assertFalse(candidate_gate(baseline, candidate)[0])
        candidate = RunnerTest.suite(target=0)
        candidate["commands"].append(copy.deepcopy(candidate["commands"][0]))
        self.assertFalse(candidate_gate(baseline, candidate)[0])

    def test_missing_completion_on_failing_baseline_is_not_reproduction(self):
        from test_agentdoctor import RunnerTest
        suite = RunnerTest.suite()
        suite["commands"][0].pop("completion")
        self.assertFalse(baseline_gate(suite)[0])


if __name__ == "__main__":
    unittest.main()
