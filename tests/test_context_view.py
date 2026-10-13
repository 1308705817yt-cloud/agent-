import copy
import json
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import model_context
from agentdoctor.attribution import checked_cause
from agentdoctor.citations import _physical_lines
from agentdoctor.contracts import validate
from agentdoctor.core import DoctorError, digest, read_json, redact, write_json
from agentdoctor.detectors import diagnose, finding
from agentdoctor.engine import run
from agentdoctor.project import load_config, snapshot
from agentdoctor.source_context import source_variants

ROOT = Path(__file__).resolve().parents[1]


def resolve(context, cause):
    ref = cause["source_evidence"]
    source = next(s for s in context["sources"] if s["source_id"] == ref["source_id"])
    assert source["start_line"] <= ref["start_line"] <= ref["end_line"] <= source["end_line"]
    return "".join(_physical_lines(source["text"])[ref["start_line"] - source["start_line"]:ref["end_line"] - source["start_line"] + 1])


class SharedSourceContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.content = "# Unrelated source padding.\n" * 800 + "def failing(value):\n    return str(value)\n"
        (self.project / "producer.py").write_text(self.content, encoding="utf-8", newline="")
        self.manifest = {"producer.py": digest(self.content)}
        self.span = {"run_id": "r", "span_id": "s", "node": "producer", "kind": "node",
                     "source": {"path": "producer.py", "function": "failing"}}
        sources = source_variants("producer.py", self.manifest["producer.py"], self.content, [])[0]
        cause = checked_cause({"summary": "The return conversion changes the required type", "evidence": ["r/s"],
                               "source": {"path": "producer.py", "start_line": 802, "end_line": 802, "text": "    return str(value)"}},
                              ["r/s"], {"r/s": self.span}, {"r/s"}, self.manifest, sources)
        issue = finding("tool_argument_schema", "high", "wrong type", [self.span])
        issue["root_causes"] = [cause]
        self.diagnosis = diagnose([issue], [self.span])
        self.config = {"source_paths": ["*.py"], "tools": {}, "checks": {},
                       "policy": {"protected_paths": [], "max_context_chars": 7000}}

    def context(self):
        return model_context(self.project, self.manifest, [self.span], self.diagnosis, self.config, [])

    def test_compaction_rebinds_source_ids_and_retains_distinct_hypotheses(self):
        cause = self.diagnosis["findings"][0]["root_causes"][0]
        for index in range(1, 4):
            alternative = copy.deepcopy(cause)
            alternative["summary"] = f"Alternative explanation {index}"
            self.diagnosis["findings"][0]["root_causes"].append(alternative)
        original = copy.deepcopy(self.diagnosis)
        compact = self.context()
        self.config["policy"]["max_context_chars"] = 200000
        full = self.context()
        self.assertEqual(self.diagnosis, original)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 7000)
        self.assertEqual(len(compact["findings"][0]["root_causes"]), 4)
        cause_list = original["findings"][0]["root_causes"]
        for context in (compact, full):
            for projected, stored in zip(context["findings"][0]["root_causes"], cause_list):
                self.assertEqual(resolve(context, projected), stored["source_evidence"]["text"])
                self.assertEqual(projected["summary"], stored["summary"])
                self.assertEqual(projected["evidence"], stored["evidence"])
                self.assertNotIn("citation", projected)
                self.assertNotIn("text", projected["source_evidence"])
            self.assertEqual(len(context["sources"]), 1)
            self.assertEqual(len(context["spans"]), 1)
        self.assertNotEqual(compact["sources"][0]["source_id"], full["sources"][0]["source_id"])
        self.assertEqual(full["sources"][0]["source_id"], cause_list[0]["source_evidence"]["source_id"])

    def test_invalid_required_source_or_trace_fails_without_losing_report_data(self):
        for variant in ("hash", "text", "missing_trace", "protected", "budget"):
            diagnosis, config = copy.deepcopy(self.diagnosis), copy.deepcopy(self.config)
            cause = diagnosis["findings"][0]["root_causes"][0]
            if variant == "hash":
                cause["source_evidence"]["sha256"] = "stale"
            elif variant == "text":
                cause["source_evidence"]["text"] = "    return value\n"
            elif variant == "missing_trace":
                cause["evidence"].append("r/absent")
            elif variant == "protected":
                config["policy"]["protected_paths"] = ["producer.py"]
            else:
                config["policy"]["max_context_chars"] = 200
            original = copy.deepcopy(diagnosis)
            with self.subTest(variant=variant), self.assertRaises(DoctorError):
                model_context(self.project, self.manifest, [self.span], diagnosis, config, [])
            self.assertEqual(diagnosis, original)

    def test_identical_text_in_different_files_keeps_distinct_references(self):
        (self.project / "alternate.py").write_text(self.content, encoding="utf-8", newline="")
        self.manifest["alternate.py"] = digest(self.content)
        cause = copy.deepcopy(self.diagnosis["findings"][0]["root_causes"][0])
        cause["source"]["path"] = "alternate.py"
        cause["source_evidence"]["path"] = "alternate.py"
        self.diagnosis["findings"][0]["root_causes"].append(cause)
        context = self.context()
        causes = context["findings"][0]["root_causes"]
        self.assertNotEqual(causes[0]["source_evidence"]["source_id"], causes[1]["source_evidence"]["source_id"])
        for projected in causes:
            source = next(s for s in context["sources"] if s["source_id"] == projected["source_evidence"]["source_id"])
            self.assertEqual(source["path"], projected["source"]["path"])
            self.assertEqual(resolve(context, projected), "    return str(value)\n")

    def test_redacted_source_evidence_rebinds_without_exposing_original_text(self):
        text = 'def failing(value):\n    secret = "fixture-private-value"\n    return str(value)\n'
        (self.project / "producer.py").write_text(text, encoding="utf-8", newline="")
        self.manifest["producer.py"] = digest(text)
        sources = redact(source_variants("producer.py", digest(text), text, [])[0])
        cause = checked_cause({"summary": "Return conversion", "evidence": ["r/s"],
                               "source": {"source_id": sources[0]["source_id"], "text": sources[0]["text"]}},
                              ["r/s"], {"r/s": self.span}, {"r/s"}, self.manifest, sources)
        self.diagnosis["findings"][0]["root_causes"] = [cause]
        original = copy.deepcopy(self.diagnosis)
        context = self.context()
        self.assertEqual(resolve(context, context["findings"][0]["root_causes"][0]), redact(text))
        self.assertNotIn("fixture-private-value", json.dumps(context))
        self.assertEqual(self.diagnosis, original)


class ArchivedPatchContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        archive = read_json(ROOT / "benchmarks/codex-workflow-v1-v0.5.2-2026-10-13.json", limit=50_000_000)
        cls.row = next(r for r in archive["cases"] if r["case_id"] == "dependency_contract_fault")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.manifest = snapshot(ROOT / "agentdoctor/workflow_data/cases/dependency_contract_fault", self.project)

    def test_completed_diagnosis_fits_original_patch_budget_with_all_references(self):
        diagnosis = copy.deepcopy(self.row["engine"]["diagnosis"])
        original = copy.deepcopy(diagnosis)
        config = load_config(self.project)
        self.assertEqual(config["policy"]["max_context_chars"], 24000)
        context = model_context(self.project, self.manifest, self.row["traces"], diagnosis, config,
                                [{"baseline": self.row["engine"]["baseline"]}])
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 24000)
        self.assertEqual(diagnosis, original)
        self.assertEqual(len(context["findings"]), len(diagnosis["findings"]))
        self.assertEqual(context["hypotheses"], sorted(diagnosis["hypotheses"], key=lambda h: h.get("origin") != "model"))
        self.assertEqual(sum(len(f["root_causes"]) for f in context["findings"]), 7)
        available = {s["evidence_id"] for s in context["spans"]}
        required = {ref for issue in diagnosis["findings"] for ref in issue["evidence"] if not ref.startswith("validation/")}
        required.update(ref for issue in diagnosis["findings"] for cause in issue["root_causes"] for ref in cause["evidence"])
        required.update(ref for h in diagnosis["hypotheses"] for ref in h["evidence"])
        self.assertTrue(required <= available)
        source_ids = set()
        for projected, stored in zip(context["findings"], diagnosis["findings"]):
            for cause, original_cause in zip(projected["root_causes"], stored["root_causes"]):
                self.assertEqual(resolve(context, cause), original_cause["source_evidence"]["text"])
                source_ids.add(cause["source_evidence"]["source_id"])
        self.assertEqual(len(source_ids), 1)

    def test_formal_repair_reaches_patch_request_after_archived_diagnosis(self):
        row = self.row
        trace = Path(self.temp.name) / "trace.json"
        write_json(trace, row["traces"])
        response = next(e["output"] for e in row["model_events"] if e["operation"] == "semantic_analysis")
        test = self

        class ReplayModel:
            provider, name, max_calls, calls, usage = "test", "archived-diagnosis", 2, 0, []

            def complete(self, operation, context, schema):
                self.calls += 1
                if self.calls == 1:
                    result = copy.deepcopy(response)
                else:
                    test.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 24000)
                    for issue in context["findings"]:
                        for cause in issue["root_causes"]:
                            test.assertIn("str(quantity)", resolve(context, cause))
                    result = {"summary": "Preserve the integer tool argument", "finding_ids": [context["findings"][0]["id"]],
                              "base_snapshot_id": context["patch_base"]["snapshot_id"],
                              "edits": [{"path": "workflow/codec.py", "before": 'return {"quantity": str(quantity)}',
                                         "after": 'return {"quantity": quantity}', "reason": "Match the integer contract"}]}
                validate(result, schema, "replay output")
                return result

        report = run(self.project, trace, mode="repair", model=ReplayModel(), executor="local", output=Path(self.temp.name) / "runs")
        self.assertEqual(report["model"]["calls"], 2, report["warnings"])
        self.assertEqual(report["status"], "verified", report["warnings"])
        self.assertEqual([e["operation"] for e in report["model_events"]], ["semantic_analysis", "patch_proposal"])
        saved = read_json(Path(report["workspace"]) / "report.json")
        causes = [c for f in saved["diagnosis"]["findings"] for c in f["root_causes"]]
        self.assertEqual(len(causes), 7)
        self.assertTrue(all("citation" in c and "text" in c["source_evidence"] for c in causes))
