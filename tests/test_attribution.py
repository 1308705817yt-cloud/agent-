import copy
import json
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import add_model_analysis, model_context
from agentdoctor.benchmark import score_diagnosis, summarize
from agentdoctor.contracts import validate
from agentdoctor.core import DoctorError, digest, read_json, write_json
from agentdoctor.detectors import diagnose, finding, merge_findings
from agentdoctor.engine import run
from agentdoctor.model import analysis_schema
from agentdoctor.source_context import source_variants


class StructuredAttributionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.text = {"packing.py": "def pack(count):\n    return {'count': str(count)}\n",
                     "transport.py": "def send(count):\n    return count * 3\n",
                     "reducer.py": "def merge(old, update):\n    return update\n"}
        self.manifest, self.sources = {}, []
        for path, text in self.text.items():
            (self.project / path).write_text(text, encoding="utf-8", newline="")
            self.manifest[path] = digest(text)
            self.sources += source_variants(path, digest(text), text, [])[0]
        self.spans = [{"run_id": "job", "span_id": "caller", "kind": "node", "node": "pack",
                       "source": {"path": "packing.py", "function": "pack"}},
                      {"run_id": "job", "span_id": "tool", "parent_id": "caller", "kind": "tool", "node": "send", "tool": "send",
                       "input": {"count": "2"}, "output": "222", "source": {"path": "transport.py", "function": "send"}}]
        self.issue = finding("tool_argument_schema", "high", "argument type mismatch", [self.spans[1]])
        self.diagnosis = diagnose([self.issue], self.spans)

    def cause(self, path="packing.py"):
        return {"summary": "The upstream value construction violates the observed contract", "evidence": ["job/tool", "job/caller"],
                "source": {"path": path, "start_line": 2, "end_line": 2, "text": self.text[path].splitlines(keepends=True)[1]}}

    def response(self):
        return {"findings": [], "hypotheses": [], "attributions": [{"finding_id": self.issue["id"], "root_cause": self.cause()}]}

    def live_response(self, context):
        result = self.response()
        entry = result["attributions"][0]
        quote = entry["root_cause"]["source"]
        source = next(s for s in context["sources"] if s["path"] == quote["path"] and s["start_line"] <= quote["start_line"] <= quote["end_line"] <= s["end_line"])
        entry["root_cause"]["source"] = {"source_id": source["source_id"], "text": quote["text"]}
        entry["classification"] = None
        return result

    def apply(self, response=None, sources=None):
        return add_model_analysis(self.diagnosis, response or self.response(), self.spans, self.manifest,
                                  supplied_sources=self.sources if sources is None else sources)

    def test_cross_file_cause_enriches_existing_category_without_relocating_observation(self):
        counts = self.apply()
        issue = self.diagnosis["findings"][0]
        self.assertEqual(counts["attributed"], 1)
        self.assertEqual(len(self.diagnosis["findings"]), 1)
        self.assertEqual(issue["category"], "tool_argument_schema")
        self.assertEqual(issue["source"], self.spans[1]["source"])
        self.assertEqual(issue["observed_source"], issue["source"])
        self.assertEqual(issue["root_causes"][0]["source"], {"path": "packing.py", "line": 2})
        self.assertEqual(issue["root_causes"][0]["status"], "hypothesized")
        self.assertEqual(issue["causal_status"], "unconfirmed")

    def test_same_mechanism_handles_state_loss_in_a_separate_reducer(self):
        self.issue = finding("state_invariant", "high", "trace context disappeared", [self.spans[1]])
        self.diagnosis = diagnose([self.issue], self.spans)
        response = self.response()
        response["attributions"][0]["root_cause"] = self.cause("reducer.py")
        self.apply(response)
        issue = self.diagnosis["findings"][0]
        self.assertEqual(issue["category"], "state_invariant")
        self.assertEqual(issue["root_causes"][0]["source"]["path"], "reducer.py")

    def test_new_finding_observation_is_not_chosen_by_citation_order(self):
        self.spans[1]["assertions"] = [{"name": "independent business contract", "passed": False}]
        entry = {"category": "other", "severity": "high", "summary": "independent business defect",
                 "evidence": ["job/caller", "job/tool"], "observed_at": "job/tool", "root_cause": self.cause()}
        for evidence in (entry["evidence"], list(reversed(entry["evidence"]))):
            self.diagnosis = diagnose([], self.spans)
            self.apply({"findings": [{**entry, "evidence": evidence}], "hypotheses": [], "attributions": []})
            self.assertEqual(self.diagnosis["findings"][0]["observed_source"], self.spans[1]["source"])

    def test_invalid_attribution_rejects_whole_response_atomically(self):
        for variant in ("unknown_finding", "unknown_span", "unrelated_span", "unknown_file", "unseen_file", "wrong_quote", "reversed_range", "wrong_line", "unrelated_run"):
            response = self.response()
            bad = copy.deepcopy(response["attributions"][0])
            root = bad["root_cause"]
            sources = copy.deepcopy(self.sources)
            if variant == "unknown_finding":
                bad["finding_id"] = "f_invented"
            elif variant == "unknown_span":
                root["evidence"].append("job/missing")
            elif variant == "unrelated_span":
                root["evidence"] = ["job/caller"]
            elif variant == "unknown_file":
                root["source"]["path"] = "missing.py"
            elif variant == "unseen_file":
                sources = [s for s in sources if s["path"] != "packing.py"]
            elif variant == "wrong_quote":
                root["source"]["text"] = "    return {'count': count}\n"
            elif variant == "reversed_range":
                root["source"]["end_line"] = 1
            elif variant == "wrong_line":
                root["source"].update(start_line=1, end_line=1)
            elif variant == "unrelated_run":
                self.spans.append({**self.spans[0], "run_id": "unrelated"})
                root["evidence"].append("unrelated/caller")
            response["attributions"].append(bad)
            before = copy.deepcopy(self.diagnosis)
            with self.subTest(variant=variant), self.assertRaises(DoctorError):
                self.apply(response, sources)
            self.assertEqual(self.diagnosis, before)

    def test_source_gaps_and_stale_hash_cannot_support_a_cause(self):
        for variant in ("gap", "hash"):
            sources = copy.deepcopy(self.sources)
            if variant == "gap":
                sources[0].update(end_line=1, text=self.text["packing.py"].splitlines(keepends=True)[0])
            else:
                sources[0]["sha256"] = "stale"
            with self.subTest(variant=variant), self.assertRaises(DoctorError):
                self.apply(sources=sources)

    def test_explicit_observation_must_belong_to_the_finding_evidence(self):
        response = {"findings": [{"category": "other", "severity": "high", "summary": "defect",
                                  "evidence": ["job/tool"], "observed_at": "job/caller", "root_cause": None}],
                    "hypotheses": []}
        with self.assertRaisesRegex(DoctorError, "Observed location"):
            self.apply(response)

    def test_merging_retains_distinct_causal_hypotheses_and_independent_defects(self):
        self.apply()
        first = self.diagnosis["findings"][0]
        duplicate = copy.deepcopy(first)
        duplicate["id"] = "f_duplicate"
        duplicate["root_causes"][0]["summary"] = "An alternative causal explanation"
        independent = finding("other", "high", "independent business defect", [self.spans[1]])
        merged, aliases = merge_findings([first, duplicate, independent])
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(merged[0]["root_causes"]), 2)
        self.assertEqual(aliases, {"f_duplicate": first["id"]})

    def test_historical_scoring_is_unchanged_and_root_scoring_is_separate(self):
        self.apply()
        score = score_diagnosis(self.diagnosis["findings"], [{"category": "tool_argument_schema", "path": "packing.py"}])
        self.assertFalse(score["correct"])
        self.assertFalse(score["localized"])
        self.assertTrue(score["root_cause_correct"])
        self.assertTrue(score["root_cause_localized"])
        overclaimed = copy.deepcopy(self.diagnosis["findings"])
        overclaimed[0]["root_causes"].append({"source": {"path": "reducer.py"}})
        self.assertFalse(score_diagnosis(overclaimed, [{"category": "tool_argument_schema", "path": "packing.py"}])["root_cause_correct"])
        historical = summarize([{"status": "measured", "faulty": True, "diagnosis": {"correct": False, "binary_outcome": "tp"}, "candidates": []}])
        self.assertEqual(historical["root_cause_diagnosis_accuracy"]["denominator"], 0)

    def test_context_retains_root_evidence_and_source_and_schema_limits_targets(self):
        self.apply()
        config = {"source_paths": ["*.py"], "tools": {}, "checks": {}, "policy": {"protected_paths": [], "max_context_chars": 24000}}
        context = model_context(self.project, self.manifest, self.spans, self.diagnosis, config, [])
        self.assertIn("job/caller", {e["id"] for e in context["evidence_catalog"]})
        cause = context["findings"][0]["root_causes"][0]
        original = self.diagnosis["findings"][0]["root_causes"][0]
        self.assertEqual(cause["evidence"], original["evidence"])
        self.assertEqual(cause["source"], original["source"])
        self.assertEqual(cause["summary"], original["summary"])
        self.assertNotIn("citation", cause)
        self.assertIn("citation", original)
        source = next(s for s in context["sources"] if s["source_id"] == cause["source_evidence"]["source_id"])
        self.assertIn(original["source_evidence"]["text"], source["text"])
        self.assertTrue(any(s["path"] == "packing.py" and self.cause()["source"]["text"] in s["text"] for s in context["sources"]))
        schema = analysis_schema(context)
        validate(self.live_response(context), schema, "analysis")
        bad = self.live_response(context)
        bad["attributions"][0]["finding_id"] = "f_fabricated"
        with self.assertRaises(DoctorError):
            validate(bad, schema, "analysis")

    def test_formal_entry_saves_both_locations_and_readable_report(self):
        config = {"schema_version": 1, "project_name": "attribution", "source_paths": ["*.py"],
                  "tools": {"send": {"input_schema": {"type": "object", "properties": {"count": {"type": "integer"}}}}},
                  "validation": {"commands": []}}
        write_json(self.project / "doctor.json", config)
        write_json(self.project / "trace.json", self.spans)
        response_cause = self.cause()

        class Model:
            provider, name, max_calls, calls, usage = "test", "fixture", 1, 0, []

            def complete(self, operation, context, schema):
                self.calls += 1
                cause = copy.deepcopy(response_cause)
                source = next(s for s in context["sources"] if s["path"] == cause["source"]["path"])
                cause["source"] = {"source_id": source["source_id"], "text": cause["source"]["text"].removesuffix("\n")}
                response = {"findings": [], "hypotheses": [], "attributions": [
                    {"finding_id": context["findings"][0]["id"], "root_cause": cause, "classification": None}]}
                validate(response, schema, "fixture")
                return response

        report = run(self.project, self.project / "trace.json", output=self.project / "runs", model=Model())
        self.assertEqual(report["status"], "diagnosed", report["warnings"])
        saved = read_json(Path(report["workspace"]) / "report.json")
        issue = saved["diagnosis"]["findings"][0]
        self.assertEqual(issue["observed_source"]["path"], "transport.py")
        self.assertEqual(issue["root_causes"][0]["source"]["path"], "packing.py")
        html = (Path(report["workspace"]) / "report.html").read_text(encoding="utf-8")
        self.assertIn("错误观察位置", html)
        self.assertIn("根因假设", html)
        self.assertIn("因果关系尚未证实", html)
