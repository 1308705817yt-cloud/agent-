import unittest
from pathlib import Path

from benchmarks.replay_analysis import replay_archives


class ArchivedAnalysisTest(unittest.TestCase):
    def test_replay_keeps_bad_ranges_rejected_and_never_invents_a_classification(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / "benchmarks" / f"codex-{suite}-v0.5.1-2026-10-13.json"
                 for suite in ("independent-v2", "workflow-v1")]
        report = replay_archives(paths)
        self.assertEqual(report["external_model_calls"], 0)
        self.assertFalse(report["is_new_model_accuracy_measurement"])
        self.assertEqual(report["summary"]["responses"], 18)
        self.assertEqual(report["summary"]["previously_accepted"], 12)
        self.assertEqual(report["summary"]["accepted_now"], 17)
        self.assertEqual(report["summary"]["newly_rejected"], [])
        self.assertEqual(report["summary"]["still_rejected"], ["loop_missing_stop"])
        self.assertEqual(set(report["summary"]["recovered"]), {"argument_alias", "argument_type", "state_merge_loss",
                                                             "async_state_merge_fault", "async_fanout_fault"})
        cases = {r["case_id"]: r for a in report["archives"] for r in a["cases"]}
        fanout = cases["async_fanout_fault"]
        self.assertEqual(fanout["fault_categories"], [])
        self.assertTrue(any(s["category"] == "task_assertion" and s["has_root_cause"] for s in fanout["unclassified_symptoms"]))
        for archive in report["archives"]:
            self.assertTrue(archive["unchanged"])
            for row in archive["cases"]:
                self.assertFalse(row["response_modified"])
                if row["case_id"] in report["summary"]["recovered"]:
                    self.assertTrue(any(c["audit"]["normalization"] == "line_endings" for c in row["citations"]))
