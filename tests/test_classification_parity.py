import copy
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import add_model_analysis
from agentdoctor.contracts import validate
from agentdoctor.core import digest, read_json, write_json
from agentdoctor.detectors import detect, diagnose
from agentdoctor.engine import run
from agentdoctor.project import load_config
from agentdoctor.source_context import source_variants


class ClassificationParityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        text = "def wait_for_result():\n    raise TimeoutError('pending')\n"
        (self.project / "worker.py").write_text(text, encoding="utf-8", newline="")
        write_json(self.project / "doctor.json", {"schema_version": 1, "project_name": "timeout-contract",
                                                "source_paths": ["*.py"], "tools": {}, "validation": {"commands": []}})
        self.config = load_config(self.project)
        self.manifest = {"worker.py": digest(text)}
        self.sources = source_variants("worker.py", digest(text), text, [])[0]
        self.span = {"run_id": "r", "span_id": "s", "kind": "node", "node": "wait",
                     "source": {"path": "worker.py", "function": "wait_for_result"}, "error": "TimeoutError: pending"}

    def diagnosis(self, spans):
        return diagnose(detect(spans, self.config, self.project, self.manifest), spans)

    def response(self, route, findings, sources=None, refs=None):
        refs = refs or ["r/s"]
        cause = {"summary": "The wait may violate the requested contract", "evidence": refs,
                 "source": {"source_id": (sources or self.sources)[0]["source_id"], "text": "    raise TimeoutError('pending')"}}
        if route == "finding":
            return {"findings": [{"category": "other", "severity": "high", "summary": "Inferred business defect",
                                  "evidence": refs, "observed_at": "r/s", "root_cause": cause}], "hypotheses": [], "attributions": []}
        symptom = next(f for f in findings if f["category"] == "execution_error" and f["evidence"] == ["r/s"])
        return {"findings": [], "hypotheses": [], "attributions": [{"finding_id": symptom["id"], "root_cause": cause,
                "classification": {"category": "other", "contract": "The task must satisfy its recorded contract",
                                   "violation": "Inferred business defect"}}]}

    def check_both(self, spans, supported, refs=None):
        reasons = []
        for route in ("attribution", "finding"):
            diagnosis = self.diagnosis(spans)
            result = self.response(route, diagnosis["findings"], refs=refs)
            original = copy.deepcopy(spans)
            with self.subTest(route=route):
                counts = add_model_analysis(diagnosis, result, spans, self.manifest, supplied_sources=self.sources)
                self.assertEqual(any(f["category"] == "other" for f in diagnosis["findings"]), supported)
                if supported:
                    self.assertEqual(counts["classified" if route == "attribution" else "accepted"], 1)
                else:
                    self.assertEqual(counts["hypothesis_only"], 1)
                    hypotheses = [h for h in diagnosis["hypotheses"] if h["origin"] == "model"]
                    self.assertEqual(len(hypotheses), 1)
                    self.assertIn("contract", hypotheses[0]["assessment_note"])
                    reasons.append(diagnosis["analysis_decisions"][-1]["reason"])
                self.assertEqual(spans, original)
        if not supported:
            self.assertEqual(reasons[0], reasons[1])

    def test_timeout_without_contract_is_hypothesis_in_both_routes(self):
        self.check_both([self.span], False)

    def test_passing_assertion_or_schema_does_not_support_other(self):
        for contract in ({"assertions": [{"name": "fallback accepted", "passed": True}]},
                         {"output_schema": {"type": "array", "minItems": 1}, "output": [1]}):
            with self.subTest(contract=contract):
                self.check_both([{**self.span, **contract}], False)

    def test_failed_contract_supports_both_routes(self):
        for contract in ({"assertions": [{"name": "must produce a value", "passed": False}]},
                         {"output_schema": {"type": "array", "minItems": 1}, "output": []},
                         {"input_schema": {"type": "integer", "minimum": 0}, "input": -1},
                         {"output_schema": {"type": "object"}, "output": "invalid JSON"}):
            with self.subTest(contract=contract):
                self.check_both([{**self.span, **contract}], True)

    def test_contracts_on_another_step_cannot_be_borrowed(self):
        for contract in ({"assertions": [{"name": "independent result", "passed": False}]},
                         {"output_schema": {"type": "integer"}, "output": "invalid"}):
            other = {**self.span, "span_id": "other", **contract}
            with self.subTest(contract=contract):
                self.check_both([self.span, other], False, refs=["r/other", "r/s"])

    def test_new_finding_without_root_cause_still_requires_a_contract(self):
        diagnosis = self.diagnosis([self.span])
        response = self.response("finding", diagnosis["findings"])
        response["findings"][0]["root_cause"] = None
        counts = add_model_analysis(diagnosis, response, [self.span], self.manifest)
        self.assertEqual(counts["hypothesis_only"], 1)
        self.assertFalse(any(f["category"] == "other" for f in diagnosis["findings"]))

    def test_formal_entry_keeps_timeout_only_as_symptom_and_hypothesis(self):
        write_json(self.project / "trace.json", [self.span])
        test = self
        for route in ("attribution", "finding"):
            class Model:
                provider, name, max_calls, calls, usage = "test", "timeout", 1, 0, []

                def complete(self, operation, context, schema):
                    self.calls += 1
                    response = test.response(route, context["findings"], context["sources"])
                    validate(response, schema, "fixture output")
                    return response

            with self.subTest(route=route):
                report = run(self.project, self.project / "trace.json", model=Model(), output=self.project / "runs")
                self.assertEqual(report["status"], "diagnosed", report["warnings"])
                saved = read_json(Path(report["workspace"]) / "report.json")
                diagnosis = saved["diagnosis"]
                self.assertEqual([f["category"] for f in diagnosis["findings"]], ["execution_error"])
                self.assertEqual(diagnosis["analysis_decisions"][-1]["disposition"], "hypothesis_only")
                self.assertTrue(any(h["origin"] == "model" and "assessment_note" in h for h in diagnosis["hypotheses"]))
