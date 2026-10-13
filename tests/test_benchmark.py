import json
import tempfile
import unittest
from pathlib import Path

from agentdoctor.benchmark import _heldout, _suite_pass, score_diagnosis, summarize
from agentdoctor.core import read_json
from agentdoctor.project import editable, load_config, snapshot
from agentdoctor.runner import ValidationRunner

DATA = Path(__file__).resolve().parents[1] / "agentdoctor" / "benchmark_data"


class BenchmarkScoringTest(unittest.TestCase):
    def test_generic_errors_do_not_count_as_correct_root_diagnosis(self):
        result = score_diagnosis([{"category": "execution_error", "source": {"path": "subject.py"}}], [{"category": "state_invariant", "path": "subject.py"}])
        self.assertFalse(result["correct"])
        self.assertEqual(result["binary_outcome"], "fn")

    def test_right_category_at_wrong_source_is_not_correct(self):
        result = score_diagnosis([{"category": "prompt_conflict", "source": {"path": "evaluator.py"}}], [{"category": "prompt_conflict", "path": "prompt.txt"}])
        self.assertFalse(result["correct"])
        self.assertEqual(result["binary_outcome"], "tp")

    def test_false_positives_on_controls_count_against_accuracy(self):
        result = score_diagnosis([{"category": "possible_loop", "source": {"path": "subject.py"}}], [])
        self.assertFalse(result["correct"])
        self.assertEqual(result["binary_outcome"], "fp")

    def test_unknown_categories_cannot_disappear_from_metrics(self):
        result = score_diagnosis([{"category": "invented", "source": {}}], [])
        self.assertFalse(result["correct"])
        self.assertEqual(result["predicted_categories"], ["invented"])

    def test_absent_attempts_have_null_regression_rate(self):
        result = summarize([{"status": "measured", "faulty": True, "diagnosis": {"correct": False, "binary_outcome": "fn"}, "candidates": []}])
        self.assertEqual(result["repair_success_rate"], {"numerator": 0, "denominator": 1, "value": 0.0})
        self.assertEqual(result["candidate_regression_failure_rate"], {"numerator": 0, "denominator": 0, "value": None})

    def test_heldout_failures_and_regressions_are_counted(self):
        result = summarize([{"status": "measured", "faulty": True, "delivered": True, "repair_success": False,
                             "diagnosis": {"correct": True, "binary_outcome": "tp"},
                             "candidates": [{"regression_evaluated": True, "regression_failed": True}]}])
        self.assertEqual(result["false_verified_rate"]["value"], 1.0)
        self.assertEqual(result["candidate_regression_failure_rate"]["value"], 1.0)
        self.assertEqual(result["repair_success_rate"]["value"], 0.0)

    def test_incomplete_cases_are_visible_and_excluded_from_measured_denominator(self):
        metrics = summarize([{"status": "incomplete", "faulty": True}])
        self.assertEqual(metrics["invalid_or_incomplete_cases"], 1)
        self.assertIsNone(metrics["diagnosis_accuracy"]["value"])


class IndependentDatasetTest(unittest.TestCase):
    def test_fixture_projects_do_not_include_gold_labels_or_holdout_inputs(self):
        suite = read_json(DATA / "suite.json")
        self.assertEqual(len(suite["cases"]), 10)
        self.assertEqual({c["family"] for c in suite["cases"]}, {"parameters", "state_merge", "termination", "prompt"})
        for case in suite["cases"]:
            project = DATA / "cases" / case["id"]
            config = load_config(project)
            self.assertFalse(editable("evaluator.py", config), case["id"])
            self.assertFalse((project / "suite.json").exists())
            self.assertFalse((project / "_heldout.json").exists())
            self.assertNotIn("expected_categories", (project / "recording.json").read_text())

    def test_independent_oracle_rejects_a_patch_overfit_to_visible_examples(self):
        suite = read_json(DATA / "suite.json")
        case = next(c for c in suite["cases"] if c["id"] == "argument_alias")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "candidate"
            manifest = snapshot(DATA / "cases" / "argument_alias", project)
            # Hard-code the visible examples: development checks pass, unseen
            # quantities must fail in the independent oracle.
            (project / "subject.py").write_text("def run(quantity):\n    return {0: 0, 2: 14}.get(quantity, 0)\n\ndef stable(value):\n    return value + 10\n")
            from agentdoctor.core import digest
            manifest["subject.py"] = digest((project / "subject.py").read_bytes())
            runner = ValidationRunner("local")
            development = runner.run_suite(project, load_config(project), manifest)
            self.assertTrue(_suite_pass(development, "target"))
            heldout = _heldout(project, root / "oracle", case["heldout"], runner)
            self.assertFalse(_suite_pass(heldout, "target"))
            self.assertTrue(_suite_pass(heldout, "regression"))
            self.assertFalse((project / "_heldout.json").exists())
