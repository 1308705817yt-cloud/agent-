import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdoctor.benchmark import run_benchmark
from agentdoctor.core import read_json
from agentdoctor.runner import ValidationRunner, execute

DATA = Path(__file__).resolve().parents[1] / "agentdoctor" / "benchmark_data"


class CandidateFailureAccountingTest(unittest.TestCase):
    def benchmark(self, source, *, environment_failure=False, baseline_timeout=False, preparation_failure=False, protected_edit=False):
        suite = read_json(DATA / "suite.json")
        suite["cases"] = [next(c for c in suite["cases"] if c["id"] == "argument_alias")]
        def proposal(project, spans, findings, config):
            if source is None:
                return []
            return [{"summary": "Independent validation probe", "finding_ids": [findings[0]["id"]],
                     "edits": [{"path": "evaluator.py" if protected_edit else "subject.py", "before": (project / "subject.py").read_text(encoding="utf-8"),
                                "after": source, "reason": "Regression probe"}]}]
        original_suite = ValidationRunner.run_suite
        def bounded(runner, project, config, manifest):
            config = copy.deepcopy(config)
            if project.name.startswith("oracle-"):
                for command in config["validation"]["commands"]:
                    command["timeout_seconds"] = 1
            return original_suite(runner, project, config, manifest)
        def launch(argv, cwd, timeout, env=None):
            if baseline_timeout and cwd.name == "oracle-baseline" and argv[-1] == "target":
                return {"returncode": 1, "timed_out": True, "error": None, "stdout": "", "stderr": "", "duration_seconds": 1}
            if environment_failure and cwd.name.startswith("oracle-candidate-") and argv[-1] == "target":
                return {"returncode": None, "timed_out": False, "error": "Cannot start validation command: OSError",
                        "stdout": "", "stderr": "", "duration_seconds": 0}
            return execute(argv, cwd, timeout, env)
        from agentdoctor.benchmark import _heldout
        def heldout(project, destination, spec, runner):
            if preparation_failure and destination.name.startswith("oracle-candidate-"):
                raise OSError("Executor workspace is unavailable")
            return _heldout(project, destination, spec, runner)
        with tempfile.TemporaryDirectory() as folder, \
             patch("agentdoctor.benchmark.read_json", return_value=suite), \
             patch("agentdoctor.engine.alias_proposals", side_effect=proposal), \
             patch.object(ValidationRunner, "run_suite", bounded), \
             patch("agentdoctor.runner.execute", side_effect=launch), \
             patch("agentdoctor.benchmark._heldout", side_effect=heldout):
            return run_benchmark(Path(folder))

    def assert_delivery_failure_counted(self, report):
        row = report["cases"][0]
        self.assertEqual(row["engine_status"], "verified")
        self.assertEqual(row["status"], "measured")
        self.assertTrue(report["complete"])
        metrics = report["metrics"]
        self.assertEqual(metrics["candidate_count"], 1)
        self.assertEqual(metrics["measured_cases"], 1)
        self.assertEqual(metrics["diagnosis_accuracy"]["denominator"], 1)
        self.assertEqual(metrics["repair_success_rate"], {"numerator": 0, "denominator": 1, "value": 0.0})
        self.assertEqual(metrics["false_verified_rate"], {"numerator": 1, "denominator": 1, "value": 1.0})
        self.assertEqual(row["repair_outcome"], "independent_acceptance_failed")
        self.assertEqual(report["repair_breakdown"]["case_outcomes"]["independent_acceptance_failed"], 1)

    def test_no_candidate_is_reported_as_no_patch(self):
        report = self.benchmark(None)
        self.assertTrue(report["complete"])
        self.assertEqual(report["repair_breakdown"]["case_outcomes"]["no_patch_generated"], 1)
        self.assertEqual(report["repair_breakdown"]["candidate_attempts"], 0)

    def test_protected_edit_is_reported_as_policy_rejected(self):
        report = self.benchmark("def run(quantity): return 7 * quantity", protected_edit=True)
        self.assertTrue(report["complete"])
        self.assertEqual(report["repair_breakdown"]["case_outcomes"]["policy_rejected"], 1)
        self.assertEqual(report["cases"][0]["candidates"][0]["failure_stage"], "policy")

    def test_visible_failure_is_not_double_counted_as_independent_failure(self):
        report = self.benchmark("def run(quantity):\n    return 0\ndef stable(value):\n    return value + 10\n")
        self.assertTrue(report["complete"])
        self.assertEqual(report["repair_breakdown"]["case_outcomes"]["visible_test_failed"], 1)
        self.assertEqual(report["repair_breakdown"]["case_outcomes"]["independent_acceptance_failed"], 0)

    def test_candidate_target_timeout_remains_a_failed_delivery(self):
        report = self.benchmark("import time\ndef run(quantity):\n    if quantity > 2:\n        time.sleep(60)\n    return quantity * 7\ndef stable(value):\n    return value + 10\n")
        self.assertTrue(report["cases"][0]["candidates"][0]["heldout"]["commands"][0]["timed_out"])
        self.assert_delivery_failure_counted(report)
        self.assertEqual(report["metrics"]["candidate_regression_failure_rate"], {"numerator": 0, "denominator": 1, "value": 0.0})

    def test_candidate_regression_timeout_is_counted_as_regression_failure(self):
        report = self.benchmark("import time\ndef run(quantity):\n    return quantity * 7\ndef stable(value):\n    if value > 5:\n        time.sleep(60)\n    return value + 10\n")
        self.assertTrue(report["cases"][0]["candidates"][0]["heldout"]["commands"][1]["timed_out"])
        self.assert_delivery_failure_counted(report)
        self.assertEqual(report["metrics"]["candidate_regression_failure_rate"], {"numerator": 1, "denominator": 1, "value": 1.0})

    def test_environment_failure_keeps_completed_diagnosis_and_attempt_count(self):
        report = self.benchmark("def run(quantity):\n    return quantity * 7\ndef stable(value):\n    return value + 10\n", environment_failure=True)
        self.assertFalse(report["complete"])
        metrics = report["metrics"]
        self.assertEqual(metrics["candidate_count"], 1)
        self.assertEqual(metrics["diagnosis_accuracy"]["denominator"], 1)
        self.assertEqual(metrics["delivered_count"], 1)
        self.assertEqual(metrics["unassessed_deliveries"], 1)
        self.assertEqual(metrics["false_verified_rate"]["denominator"], 0)
        self.assertEqual(report["cases"][0]["candidates"][0]["heldout_outcomes"]["target"], "environment_error")

    def test_candidate_mutation_is_failure_not_environment_outage(self):
        report = self.benchmark("from pathlib import Path\ndef run(quantity):\n    if quantity > 2:\n        Path('added.py').write_text('x=1')\n    return quantity * 7\ndef stable(value):\n    return value + 10\n")
        self.assertTrue(report["cases"][0]["candidates"][0]["heldout"]["integrity_changes"])
        self.assert_delivery_failure_counted(report)

    def test_unavailable_candidate_workspace_does_not_erase_prior_stages(self):
        report = self.benchmark("def run(quantity):\n    return quantity * 7\ndef stable(value):\n    return value + 10\n", preparation_failure=True)
        self.assertFalse(report["complete"])
        self.assertEqual(report["metrics"]["candidate_count"], 1)
        self.assertEqual(report["metrics"]["diagnosis_accuracy"]["denominator"], 1)
        self.assertEqual(report["metrics"]["incomplete_candidates"], 1)
        self.assertEqual(report["metrics"]["unassessed_deliveries"], 1)

    def test_baseline_timeout_remains_incomplete_before_diagnosis_and_repair(self):
        report = self.benchmark("def run(quantity):\n    return quantity * 7\ndef stable(value):\n    return value + 10\n", baseline_timeout=True)
        self.assertFalse(report["complete"])
        self.assertEqual(report["metrics"]["candidate_count"], 0)
        self.assertEqual(report["metrics"]["diagnosis_accuracy"]["denominator"], 0)
        self.assertEqual(report["metrics"]["repair_success_rate"]["denominator"], 0)
        self.assertEqual(report["metrics"]["invalid_or_incomplete_cases"], 1)
