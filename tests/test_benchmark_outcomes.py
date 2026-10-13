import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agentdoctor.benchmark import candidate_outcome, repair_breakdown, repair_outcome, run_benchmark
from agentdoctor.core import DoctorError, digest, read_json, write_json
from agentdoctor.project import snapshot

ROOT = Path(__file__).resolve().parents[1]


def candidate(stage):
    item = {"status": "rejected", "origin": "model", "failure_stage": None,
            "visible_outcomes": {"target": "passed", "regression": "passed"},
            "heldout_outcomes": {"target": "passed", "regression": "passed"}, "heldout_passed": False}
    if stage == "policy_rejected":
        item.update(failure_stage="policy", visible_outcomes={})
    elif stage == "visible_test_failed":
        item["visible_outcomes"]["target"] = "failed"
    elif stage == "independent_acceptance_failed":
        item.update(status="verified")
        item["heldout_outcomes"]["target"] = "failed"
    elif stage == "repaired":
        item.update(status="verified", heldout_passed=True)
    elif stage == "environment_error":
        item["heldout_outcomes"]["target"] = "environment_error"
    return item


def row(*candidates, **kwargs):
    return {"faulty": True, "repair_status": "measured", "engine_status": "rejected",
            "repair_termination": "candidate_budget", "candidates": list(candidates), **kwargs}


class OutcomeAccountingTest(unittest.TestCase):
    def test_four_failure_categories_have_disjoint_case_counts(self):
        rows = [row(repair_termination="no_patch")]
        for stage in ("policy_rejected", "visible_test_failed", "independent_acceptance_failed"):
            item = candidate(stage)
            self.assertEqual(candidate_outcome(item), stage)
            rows.append(row(item))
        result = repair_breakdown(rows)
        self.assertEqual(sum(result["case_outcomes"].values()), 4)
        for stage in ("no_patch_generated", "policy_rejected", "visible_test_failed", "independent_acceptance_failed"):
            self.assertEqual(result["case_outcomes"][stage], 1)
        self.assertEqual(result["candidate_attempts"], 3)
        self.assertEqual(sum(result["candidate_outcomes"].values()), 3)

    def test_successful_retry_keeps_failed_attempt_without_double_counting_case(self):
        result = repair_breakdown([row(candidate("policy_rejected"), candidate("visible_test_failed"), candidate("repaired"), repair_success=True)])
        self.assertEqual(result["case_outcomes"]["repaired"], 1)
        self.assertEqual(sum(result["case_outcomes"].values()), 1)
        self.assertEqual(result["candidate_outcomes"]["policy_rejected"], 1)
        self.assertEqual(result["candidate_outcomes"]["visible_test_failed"], 1)
        self.assertEqual(result["candidate_outcomes"]["repaired"], 1)

    def test_failed_retries_use_furthest_completed_gate(self):
        self.assertEqual(repair_outcome(row(candidate("policy_rejected"), candidate("visible_test_failed"), candidate("independent_acceptance_failed"))), "independent_acceptance_failed")
        self.assertEqual(repair_outcome(row(candidate("policy_rejected"), candidate("visible_test_failed"), repair_termination="no_patch")), "visible_test_failed")

    def test_controls_and_infrastructure_errors_are_not_no_patch(self):
        rows = [row(faulty=False, repair_termination="no_patch"),
                row(repair_termination="model_error", repair_status="incomplete"),
                row(candidate("environment_error"), repair_status="incomplete"),
                row(engine_status=None, repair_status="incomplete")]
        result = repair_breakdown(rows)
        self.assertEqual(result["faulty_cases"], 3)
        self.assertEqual(result["case_outcomes"]["no_patch_generated"], 0)
        for stage in ("model_error", "environment_error", "not_evaluated"):
            self.assertEqual(result["case_outcomes"][stage], 1)

    def test_visible_failure_takes_precedence_even_if_heldout_passes(self):
        self.assertEqual(candidate_outcome(candidate("visible_test_failed")), "visible_test_failed")

    def test_rule_candidates_are_not_attributed_to_model(self):
        alias = candidate("repaired")
        alias["origin"] = "declared_alias"
        result = repair_breakdown([row(alias, repair_success=True)])
        self.assertEqual(result["candidate_origins"], {"model": 0, "declared_alias": 1, "unknown": 0})


class FixedDatasetTest(unittest.TestCase):
    def test_lock_matches_files_case_ids_and_previous_baseline(self):
        lock = read_json(ROOT / "benchmarks/independent-v2.lock.json")
        with tempfile.TemporaryDirectory() as folder:
            files = snapshot(ROOT / "agentdoctor/benchmark_data", Path(folder) / "dataset")
        self.assertEqual(lock["files"], files)
        self.assertEqual(lock["dataset_hash"], digest(json.dumps(files, sort_keys=True)))
        self.assertEqual(lock["dataset_hash"], read_json(ROOT / "benchmarks/baseline.json")["dataset_hash"])
        suite = read_json(ROOT / "agentdoctor/benchmark_data/suite.json")
        self.assertEqual(lock["case_ids"], [c["id"] for c in suite["cases"]])

    def test_drift_stops_before_any_model_call(self):
        lock = copy.deepcopy(read_json(ROOT / "benchmarks/independent-v2.lock.json"))
        lock["dataset_hash"] = "unexpected_hash"
        factory = Mock()
        with tempfile.TemporaryDirectory() as folder:
            lock_path = Path(folder) / "lock.json"
            write_json(lock_path, lock)
            with self.assertRaisesRegex(DoctorError, "no model calls"):
                run_benchmark(Path(folder) / "runs", model_factory=factory, dataset_lock=lock_path)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
