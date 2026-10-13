import copy
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import add_model_analysis
from agentdoctor.benchmark import score_diagnosis
from agentdoctor.contracts import validate
from agentdoctor.core import DoctorError, digest, read_json, write_json
from agentdoctor.detectors import diagnose, finding
from agentdoctor.engine import run
from agentdoctor.source_context import source_variants
from agentdoctor.taxonomy import SYMPTOMS


class SymptomClassificationTest(unittest.TestCase):
    def setUp(self):
        self.text = "def arrange(values):\n    return sorted(values)\n"
        self.manifest = {"worker.py": digest(self.text), "consumer.py": "consumer-hash"}
        self.sources = source_variants("worker.py", digest(self.text), self.text, [])[0]
        self.span = {"run_id": "r", "span_id": "s", "kind": "task", "node": "completion", "input": [8, 2], "output": [2, 8],
                     "source": {"path": "consumer.py", "function": "run"},
                     "assertions": [{"name": "preserve input order", "passed": False, "expected": [8, 2], "actual": [2, 8]}]}
        self.spans = [self.span]
        self.symptom = finding("task_assertion", "high", "order assertion failed", self.spans,
                               details={"assertion": self.span["assertions"][0]})
        self.diagnosis = diagnose([self.symptom], self.spans)
        self.cause = {"summary": "Sorting changes the requested order", "evidence": ["r/s"],
                      "source": {"source_id": self.sources[0]["source_id"], "text": "    return sorted(values)"}}
        self.classification = {"category": "other", "contract": "Preserve the caller's input order",
                               "violation": "Sorting changes [8, 2] into [2, 8]"}

    def response(self, classification=True):
        return {"findings": [], "hypotheses": [], "attributions": [
            {"finding_id": self.symptom["id"], "root_cause": copy.deepcopy(self.cause),
             "classification": copy.deepcopy(self.classification) if classification else None}]}

    def apply(self, result=None):
        return add_model_analysis(self.diagnosis, result or self.response(), self.spans, self.manifest, supplied_sources=self.sources)

    def test_explicit_business_fault_preserves_and_links_symptom(self):
        self.assertEqual(self.apply()["classified"], 1)
        symptom, fault = self.diagnosis["findings"]
        self.assertEqual(symptom["category"], "task_assertion")
        self.assertEqual(symptom["classification_level"], "symptom")
        self.assertEqual(fault["category"], "other")
        self.assertEqual(fault["symptom_ids"], [symptom["id"]])
        self.assertEqual(symptom["classified_by"], [fault["id"]])
        self.assertEqual(fault["root_causes"][0]["source"], {"path": "worker.py", "line": 2})
        self.assertEqual(fault["observed_source"]["path"], "consumer.py")
        self.assertEqual(self.diagnosis["coverage"]["unclassified_symptoms"], [])
        self.assertTrue(score_diagnosis(self.diagnosis["findings"], [{"category": "other", "path": "worker.py"}])["root_cause_correct"])

    def test_explaining_a_symptom_does_not_automatically_classify_it(self):
        self.apply(self.response(False))
        self.assertEqual([f["category"] for f in self.diagnosis["findings"]], ["task_assertion"])
        self.assertEqual(self.diagnosis["coverage"]["unclassified_symptoms"], [self.symptom["id"]])
        self.assertFalse(score_diagnosis(self.diagnosis["findings"], [{"category": "other", "path": "worker.py"}])["root_cause_correct"])

    def test_generic_exception_cannot_become_other_without_a_contract(self):
        self.span.pop("assertions")
        for category in ("execution_error", "task_failed", "unverified_completion"):
            self.symptom = finding(category, "high", "operation failed", self.spans)
            self.diagnosis = diagnose([self.symptom], self.spans)
            with self.subTest(category=category):
                counts = self.apply()
                self.assertEqual(counts["hypothesis_only"], 1)
                self.assertEqual(len(self.diagnosis["findings"]), 1)
                self.assertIn("contract", self.diagnosis["analysis_decisions"][0]["reason"])

    def test_failed_schema_can_support_an_explicit_business_classification(self):
        self.symptom = finding("output_schema", "high", "empty output", self.spans,
                               details={"violations": [{"rule": "minItems", "message": "too short"}]})
        self.diagnosis = diagnose([self.symptom], self.spans)
        self.assertEqual(self.apply()["classified"], 1)

    def test_known_fault_category_cannot_be_relabelled_through_attribution(self):
        self.symptom = finding("tool_argument_schema", "high", "argument type", self.spans)
        self.diagnosis = diagnose([self.symptom], self.spans)
        before = copy.deepcopy(self.diagnosis)
        with self.assertRaisesRegex(DoctorError, "existing fault categories"):
            self.apply()
        self.assertEqual(self.diagnosis, before)

    def test_bad_classification_or_citation_rejects_response_before_any_mutation(self):
        for variant in ("empty_contract", "bad_quote", "unknown_category", "unknown_finding"):
            response = self.response()
            bad = copy.deepcopy(response["attributions"][0])
            if variant == "empty_contract":
                bad["classification"]["contract"] = " "
            elif variant == "bad_quote":
                bad["root_cause"]["source"]["text"] = "    return values"
            elif variant == "unknown_category":
                bad["classification"]["category"] = "new_benchmark_label"
            else:
                bad["finding_id"] = "f_unseen"
            response["attributions"].append(bad)
            before = copy.deepcopy(self.diagnosis)
            with self.subTest(variant=variant), self.assertRaises(DoctorError):
                self.apply(response)
            self.assertEqual(self.diagnosis, before)

    def test_classification_cannot_bypass_plan_or_bounded_polling_checks(self):
        for category in ("plan_action_mismatch", "possible_loop"):
            self.diagnosis = diagnose([self.symptom], self.spans)
            self.classification["category"] = category
            if category == "possible_loop":
                self.diagnosis["observations"] = [finding("bounded_polling", "low", "pending", self.spans)]
            with self.subTest(category=category):
                self.assertEqual(self.apply()["hypothesis_only"], 1)
                self.assertEqual(len(self.diagnosis["findings"]), 1)

    def test_repeated_classifications_merge_without_losing_backlinks(self):
        response = self.response()
        second = copy.deepcopy(response["attributions"][0])
        second["classification"]["violation"] = "The returned order differs from the requested order"
        response["attributions"].append(second)
        self.apply(response)
        symptom, fault = self.diagnosis["findings"]
        self.assertEqual(fault["symptom_ids"], [symptom["id"]])
        self.assertEqual(symptom["classified_by"], [fault["id"]])
        self.assertEqual(len(fault["classification_bases"]), 2)
        self.assertEqual(len(self.diagnosis["findings"]), 2)

    def test_failed_assertion_from_another_step_is_not_borrowed(self):
        self.span["assertions"][0]["passed"] = True
        other = copy.deepcopy(self.span)
        other["span_id"] = "unrelated"
        other["assertions"][0]["passed"] = False
        self.spans.append(other)
        self.cause["evidence"].append("r/unrelated")
        self.assertEqual(self.apply()["hypothesis_only"], 1)

    def test_historical_symptom_labels_remain_excluded_from_fault_scores(self):
        self.assertEqual(SYMPTOMS, {"execution_error", "task_failed", "task_assertion", "validation_failure", "input_schema", "output_schema", "invalid_json", "unverified_completion"})
        for category in SYMPTOMS:
            result = score_diagnosis([{"category": category, "source": {"path": "worker.py"}}], [{"category": "other", "path": "worker.py"}])
            self.assertFalse(result["correct"])
            self.assertFalse(result["root_cause_correct"])

    def test_formal_entry_records_linked_classification_and_canonical_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "worker.py").write_text(self.text, encoding="utf-8", newline="")
            (project / "consumer.py").write_text("from worker import arrange\ndef run(values):\n    return arrange(values)\n", encoding="utf-8")
            write_json(project / "doctor.json", {"schema_version": 1, "project_name": "order-contract",
                                                "source_paths": ["*.py"], "tools": {}, "validation": {"commands": []}})
            write_json(project / "trace.json", self.spans)
            classification = copy.deepcopy(self.classification)

            class Model:
                provider, name, max_calls, calls, usage = "test", "fixture", 1, 0, []

                def complete(self, operation, context, schema):
                    self.calls += 1
                    symptom = next(f for f in context["findings"] if f["category"] == "task_assertion")
                    source = next(s for s in context["sources"] if s["path"] == "worker.py")
                    result = {"findings": [], "hypotheses": [], "attributions": [{
                        "finding_id": symptom["id"], "classification": classification,
                        "root_cause": {"summary": "Sorting breaks the order contract", "evidence": ["r/s"],
                                       "source": {"source_id": source["source_id"], "text": "    return sorted(values)"}}}]}
                    validate(result, schema, "fixture")
                    return result

            report = run(project, project / "trace.json", output=project / "runs", model=Model())
            self.assertEqual(report["status"], "diagnosed", report["warnings"])
            saved = read_json(Path(report["workspace"]) / "report.json")
            symptom, fault = saved["diagnosis"]["findings"]
            self.assertEqual(symptom["classified_by"], [fault["id"]])
            self.assertEqual(fault["symptom_ids"], [symptom["id"]])
            self.assertEqual(fault["classification_bases"][0]["contract"], classification["contract"])
            self.assertEqual(fault["observed_source"]["path"], "consumer.py")
            cause = fault["root_causes"][0]
            self.assertEqual(cause["source"], {"path": "worker.py", "line": 2})
            self.assertEqual(cause["source_evidence"]["text"], "    return sorted(values)\n")
            self.assertEqual(cause["citation"]["normalization"], "line_endings")
            html = (Path(report["workspace"]) / "report.html").read_text(encoding="utf-8")
            self.assertIn("classification_bases", html)
            self.assertIn("classified_by", html)
