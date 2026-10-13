import copy
import json
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import add_model_analysis, model_context
from agentdoctor.contracts import validate
from agentdoctor.core import DoctorError, digest, read_json, write_json
from agentdoctor.detectors import detect, diagnose, finding, merge_findings
from agentdoctor.engine import run
from agentdoctor.model import analysis_schema
from agentdoctor.traces import TraceRecorder, load_traces


class SemanticContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / "worker.py").write_text("def apply(state):\n    return state\n", encoding="utf-8")
        self.manifest = {"worker.py": digest((self.project / "worker.py").read_bytes())}
        self.config = {"tools": {}, "checks": {}, "source_paths": ["*.py"],
                       "policy": {"protected_paths": [], "max_context_chars": 48000}}
        self.current = [{"run_id": "bad", "span_id": "step", "node": "apply", "kind": "node", "input": {"x": 1},
                         "source": {"path": "worker.py", "function": "apply"}, "metadata": {"task_id": "task"}}]
        self.reference = [{**copy.deepcopy(self.current[0]), "run_id": "good", "input": {"x": 2},
                           "task_status": "completed", "assertions": [{"name": "task", "passed": True}]}]
        self.issue = finding("state_invariant", "high", "state changed", self.current)
        self.diagnosis = diagnose([self.issue], self.current, self.reference)
        add_model_analysis(self.diagnosis, {"findings": [], "hypotheses": [{"summary": "Update replaces the existing state", "evidence": ["bad/step", "good/step"], "source_path": "worker.py"}]}, self.current, self.manifest, self.reference)

    def context(self, spans=None):
        return model_context(self.project, self.manifest, spans or self.current, self.diagnosis, self.config, [], self.reference)

    def test_hypotheses_and_comparisons_have_actual_citable_traces(self):
        context = self.context()
        self.assertEqual(context["spans"][0]["evidence_id"], "bad/step")
        self.assertEqual(context["reference_spans"][0]["evidence_id"], "good/step")
        self.assertEqual(context["trace_comparisons"][0]["first_difference"], ["bad/step", "good/step"])
        self.assertEqual(context["hypotheses"][0]["origin"], "model")
        self.assertEqual(context["hypotheses"][0]["status"], "hypothesized")

    def test_budget_trimming_preserves_links_and_reports_omissions(self):
        self.config["policy"]["max_context_chars"] = 6500
        spans = self.current + [{**copy.deepcopy(self.current[0]), "span_id": str(i), "input": "x" * 1000} for i in range(60)]
        context = self.context(spans)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 6500)
        self.assertGreater(context["omitted_spans"], 0)
        self.assertTrue(context["hypotheses"])
        self.assertTrue(context["trace_comparisons"])
        known = {e["id"] for e in context["evidence_catalog"]}
        findings = {f["id"] for f in context["findings"]}
        for hypothesis in context["hypotheses"]:
            self.assertTrue(set(hypothesis["evidence"]) <= known)
            self.assertTrue(set(hypothesis["finding_ids"]) <= findings)
        for comparison in context["trace_comparisons"]:
            self.assertTrue(set(comparison["first_difference"]) <= known)

    def test_essential_context_cannot_be_silently_discarded(self):
        self.config["policy"]["max_context_chars"] = 1000
        with self.assertRaisesRegex(DoctorError, "Essential model context"):
            self.context()

    def test_loop_evidence_above_forty_is_complete_or_explicitly_rejected(self):
        self.config["tools"] = {"fetch": {"input_schema": {"type": "object"}}}
        self.config["checks"] = {"loop_threshold": 41, "preserve_state_keys": []}
        spans = [{"run_id": "loop", "span_id": str(i), "kind": "tool", "node": "watch",
                  "tool": "fetch", "input": {}, "output": {"done": True}} for i in range(41)]
        diagnosis = diagnose(detect(spans, self.config, self.project, self.manifest), spans)
        self.assertEqual(len(diagnosis["findings"]), 1)
        self.assertEqual(len(diagnosis["hypotheses"]), 1)
        self.config["policy"]["max_context_chars"] = 200000
        context = model_context(self.project, self.manifest, spans, diagnosis, self.config, [])
        self.assertEqual(context["findings"], diagnosis["findings"])
        self.assertEqual(context["hypotheses"], diagnosis["hypotheses"])
        self.assertEqual({e["id"] for e in context["evidence_catalog"]}, {f"loop/{i}" for i in range(41)})
        self.assertEqual(context["omitted_spans"], 0)
        self.config["policy"]["max_context_chars"] = 5000
        with self.assertRaisesRegex(DoctorError, "Essential model context"):
            model_context(self.project, self.manifest, spans, diagnosis, self.config, [])

    def test_many_findings_and_their_shared_hypothesis_keep_all_dependencies(self):
        spans = [{**copy.deepcopy(self.current[0]), "span_id": str(i)} for i in range(45)]
        findings = [finding("state_invariant", "high", "state changed", [s]) for s in spans]
        diagnosis = diagnose(findings, spans)
        self.config["policy"]["max_context_chars"] = 200000
        context = model_context(self.project, self.manifest, spans, diagnosis, self.config, [])
        self.assertEqual(len(context["findings"]), 45)
        self.assertEqual(len(context["hypotheses"]), 1)
        self.assertEqual(set(context["hypotheses"][0]["finding_ids"]), {f["id"] for f in context["findings"]})
        self.assertEqual(set(context["hypotheses"][0]["evidence"]), {e["id"] for e in context["evidence_catalog"]})

    def test_missing_root_evidence_is_explicitly_rejected(self):
        self.diagnosis["hypotheses"][0]["evidence"].append("bad/missing")
        with self.assertRaisesRegex(DoctorError, "[Ii]ncomplete model context"):
            self.context()

    def test_missing_hypothesis_finding_is_explicitly_rejected(self):
        self.diagnosis["hypotheses"][0]["finding_ids"].append("f_missing")
        with self.assertRaisesRegex(DoctorError, "[Ii]ncomplete model context"):
            self.context()

    def test_reference_and_observation_evidence_above_forty_is_preserved(self):
        references = [{**copy.deepcopy(self.reference[0]), "span_id": str(i)} for i in range(41)]
        spans = [{**copy.deepcopy(self.current[0]), "span_id": str(i)} for i in range(41)]
        diagnosis = diagnose([], spans)
        diagnosis["hypotheses"] = [{"id": "h_compare", "finding_ids": [], "evidence": ["bad/0"] + [f"good/{i}" for i in range(41)]}]
        diagnosis["observations"] = [finding("bounded_polling", "low", "pending", spans)]
        self.config["policy"]["max_context_chars"] = 200000
        context = model_context(self.project, self.manifest, spans, diagnosis, self.config, [], references)
        self.assertEqual(context["hypotheses"], diagnosis["hypotheses"])
        self.assertEqual(context["observations"], diagnosis["observations"])
        self.assertEqual(len(context["reference_spans"]), 41)
        self.assertEqual(len(context["spans"]), 41)

    def test_formal_diagnosis_preserves_evidence_or_reports_incomplete_without_model_call(self):
        recorder = TraceRecorder(self.project / "trace.jsonl", run_id="graph")
        tool = recorder.wrap("watch", lambda: {"done": True}, kind="tool", tool="fetch")
        node = recorder.wrap("step", lambda: tool())
        for _ in range(41):
            node()
        expected = {f"graph/{s['span_id']}" for s in load_traces(recorder.path) if s["kind"] == "tool"}
        config = {"schema_version": 1, "project_name": "graph", "source_paths": ["worker.py"],
                  "tools": {"fetch": {"input_schema": {"type": "object"}}}, "validation": {"commands": []},
                  "checks": {"loop_threshold": 41}, "policy": {}}

        class CapturingModel:
            provider, name, max_calls = "test", "fixture", 8

            def __init__(self):
                self.calls, self.usage, self.contexts = 0, [], []

            def complete(self, operation, context, schema):
                self.calls += 1
                self.contexts.append(context)
                return {"findings": [], "hypotheses": []}

        for budget in (200000, 5000):
            config["policy"]["max_context_chars"] = budget
            write_json(self.project / "doctor.json", config)
            model = CapturingModel()
            with self.subTest(budget=budget):
                report = run(self.project, recorder.path, output=self.project / "runs", model=model)
                self.assertEqual(len(report["diagnosis"]["findings"]), 1)
                self.assertEqual(len(report["diagnosis"]["hypotheses"]), 1)
                self.assertEqual(set(report["diagnosis"]["findings"][0]["evidence"]), expected)
                saved = read_json(Path(report["workspace"]) / "report.json")
                self.assertEqual(saved["diagnosis"], report["diagnosis"])
                if budget == 200000:
                    self.assertEqual(report["status"], "diagnosed")
                    self.assertEqual(model.calls, 1)
                    self.assertEqual(set(model.contexts[0]["hypotheses"][0]["evidence"]), expected)
                    self.assertTrue(expected <= {e["id"] for e in model.contexts[0]["evidence_catalog"]})
                else:
                    self.assertEqual(report["status"], "inconclusive")
                    self.assertEqual(model.calls, 0)
                    self.assertEqual(report["diagnosis"]["coverage"]["semantic_checks"], "failed")
                    self.assertIn("Essential model context", " ".join(saved["warnings"]))

    def test_dynamic_schema_rejects_invented_and_reference_only_findings(self):
        schema = analysis_schema(self.context())
        for ref in ("bad/invented", "good/step"):
            result = {"findings": [{"category": "state_invariant", "severity": "high", "summary": "state changed", "evidence": [ref]}], "hypotheses": []}
            with self.subTest(ref=ref), self.assertRaises(DoctorError):
                validate(result, schema, "analysis")

    def test_runtime_rejects_unsupplied_evidence_atomically(self):
        previous = copy.deepcopy(self.diagnosis)
        result = {"findings": [], "hypotheses": [{"summary": "cause", "source_path": "worker.py", "evidence": ["bad/step", "good/step"]}]}
        with self.assertRaisesRegex(DoctorError, "supplied current context"):
            add_model_analysis(self.diagnosis, result, self.current, self.manifest, self.reference, {"bad/step"})
        self.assertEqual(self.diagnosis, previous)

    def test_secrets_in_hypotheses_and_reference_traces_are_redacted(self):
        self.reference[0]["metadata"]["api_key"] = "private-value"
        self.diagnosis["hypotheses"][0]["summary"] = "password=private-value"
        text = json.dumps(self.context())
        self.assertNotIn("private-value", text)
        self.assertIn("[REDACTED]", text)


class AlertMergingTest(unittest.TestCase):
    def setUp(self):
        self.span = {"run_id": "run", "span_id": "call", "node": "worker", "kind": "node", "source": {"path": "worker.py", "function": "work"}}
        self.rule = finding("tool_argument_schema", "high", "type mismatch", [self.span], details={"tool": "fetch"})
        self.diagnosis = diagnose([self.rule], [self.span])

    def response(self, category, summary="different wording"):
        return {"findings": [{"category": category, "severity": "medium", "summary": summary, "evidence": ["run/call"]}], "hypotheses": []}

    def test_duplicate_rule_and_model_alerts_merge_with_provenance(self):
        counts = add_model_analysis(self.diagnosis, self.response("tool_argument_schema"), [self.span], {"worker.py": "hash"})
        self.assertEqual(len(self.diagnosis["findings"]), 1)
        item = self.diagnosis["findings"][0]
        self.assertEqual(item["id"], self.rule["id"])
        self.assertEqual(item["origins"], ["model", "rule"])
        self.assertEqual(len(item["supporting_findings"]), 2)
        self.assertEqual(counts["merged"], 1)

    def test_inferred_plan_mismatch_becomes_auditable_hypothesis(self):
        add_model_analysis(self.diagnosis, self.response("plan_action_mismatch"), [self.span], {"worker.py": "hash"})
        self.assertEqual([f["category"] for f in self.diagnosis["findings"]], ["tool_argument_schema"])
        self.assertEqual(self.diagnosis["analysis_decisions"][0]["disposition"], "hypothesis_only")
        self.assertEqual(self.diagnosis["hypotheses"][-1]["finding_ids"], [self.rule["id"]])

    def test_independent_explicit_plan_mismatch_is_not_hidden(self):
        self.span["metadata"] = {"plan": "retrieve the receipt", "action": "delete the receipt"}
        add_model_analysis(self.diagnosis, self.response("plan_action_mismatch"), [self.span], {"worker.py": "hash"})
        self.assertEqual({f["category"] for f in self.diagnosis["findings"]}, {"tool_argument_schema", "plan_action_mismatch"})

    def test_plan_and_action_are_compared_within_the_same_step(self):
        for variant in ("aligned", "unlinked", "mismatch"):
            spans = [{**copy.deepcopy(self.span), "metadata": {"plan": "fetch", "action": "fetch"}},
                     {**copy.deepcopy(self.span), "span_id": "save", "metadata": {"plan": "save", "action": "save"}}]
            if variant == "unlinked":
                del spans[0]["metadata"]["action"]
                del spans[1]["metadata"]["plan"]
            elif variant == "mismatch":
                spans[1]["metadata"]["action"] = "delete"
            diagnosis = diagnose([], spans)
            response = self.response("plan_action_mismatch")
            response["findings"][0]["evidence"].append("run/save")
            with self.subTest(variant=variant):
                counts = add_model_analysis(diagnosis, response, spans, {"worker.py": "hash"})
                expected = "accepted" if variant == "mismatch" else "hypothesis_only"
                self.assertEqual(counts[expected], 1)
                self.assertEqual(len(diagnosis["findings"]), int(variant == "mismatch"))
                if variant != "mismatch":
                    self.assertEqual(len(diagnosis["hypotheses"]), 1)
                    self.assertIn("same", diagnosis["analysis_decisions"][0]["reason"])

    def test_different_locations_and_disjoint_events_remain_separate(self):
        other_location = {**self.span, "source": {"path": "other.py", "function": "work"}}
        other_event = {**self.span, "span_id": "next"}
        result, _ = merge_findings([self.rule, finding("tool_argument_schema", "high", "elsewhere", [other_location]), finding("tool_argument_schema", "high", "later", [other_event])])
        self.assertEqual(len(result), 3)

