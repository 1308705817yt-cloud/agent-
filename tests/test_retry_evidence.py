import copy
import json
import tempfile
import unittest
from pathlib import Path

from agentdoctor.contracts import validate
from agentdoctor.core import DoctorError, digest, read_json, write_json
from agentdoctor.engine import run
from agentdoctor.model import patch_schema
from agentdoctor.project import check_manifest, editable, load_config, snapshot
from agentdoctor.retry import _replay, failure_feedback, snapshot_version
from agentdoctor.runner import ValidationRunner, candidate_gate
from agentdoctor.visible_cases import visible_cases


ROOT = Path(__file__).resolve().parents[1]
HIDDEN = "HIDDEN_INPUT_MUST_NOT_ENTER_RETRY_e76b93"
HELPERS = "def first(value):\n    return value + 1\n\nasync def second(value):\n    return value + 2\n"
FIRST_EDIT = {"path": "helpers.py", "before": "return value + 1", "after": "return value + 10\n\n", "reason": "Fix first calculation"}
SECOND_EDIT = {"path": "helpers.py", "before": "return value + 2", "after": "return value + 20", "reason": "Fix second calculation"}


def fixture(root):
    project = root / "project"
    project.mkdir()
    (project / "helpers.py").write_text(HELPERS, encoding="utf-8")
    (project / "subject.py").write_text("import helpers\nasync def run(kind, value):\n    if kind == 'first':\n        return helpers.first(value)\n    return await helpers.second(value)\n\ndef stable(value):\n    return value * 2\n", encoding="utf-8")
    # Reuse the frozen evaluator verbatim; only configure visible replay, never
    # teach the evaluator a new acceptance rule or replace it with the worker.
    (project / "evaluator.py").write_bytes((ROOT / "benchmarks/public-smolagents-v1/cases/loop_else_fault/evaluator.py").read_bytes())
    write_json(project / "visible.json", {"target": [
        {"function": "run", "kwargs": {"kind": "first", "value": 3}, "expected": 13},
        {"function": "run", "kwargs": {"kind": "second", "value": 3}, "expected": 23}],
        "regression": [{"function": "stable", "kwargs": {"value": 4}, "expected": 8}]})
    write_json(project / "oracle-only.json", {"hidden": HIDDEN})
    config = {"schema_version": 1, "project_name": "retry evidence", "source_paths": ["subject.py", "helpers.py"], "tools": {},
              "validation": {"repetitions": 2, "commands": [
                  {"name": kind, "kind": kind, "argv": ["{python}", "-m", "evaluator", kind], "timeout_seconds": 10,
                   "expected_cases": [f"{kind}:{i}" for i in range(1, 3 if kind == "target" else 2)],
                   "replay": {"module": "subject", "cases_file": "visible.json", "case_group": kind}}
                  for kind in ("target", "regression")]},
              "policy": {"max_candidates": 2, "max_model_calls": 3, "max_context_chars": 24000}}
    write_json(project / "doctor.json", config)
    traces = root / "traces.json"
    write_json(traces, [{"run_id": "original", "span_id": "failure", "node": "task", "kind": "task",
                         "input": {"kind": "first", "value": 3}, "output": 4, "source": {"path": "helpers.py", "function": "first", "line": 1},
                         "assertions": [{"name": "visible calculation", "passed": False, "actual": 4, "expected": 13}]}])
    return project, traces


class RetryIntegrationTest(unittest.TestCase):
    def model(self, mode="complete"):
        test = self

        class Model:
            provider, name, calls, max_calls = "test", "preset-no-external-calls", 0, 3

            def __init__(self):
                self.usage, self.contexts = [], []

            def complete(self, instruction, context, schema):
                self.calls += 1
                self.contexts.append(copy.deepcopy(context))
                test.assertNotIn(HIDDEN, json.dumps(context))
                test.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 24000)
                if self.calls == 1:
                    return {"findings": [], "hypotheses": [], "attributions": []}
                base = context["patch_base"]
                test.assertEqual(base["kind"], "original_snapshot")
                test.assertEqual(base["application"], "complete_replacement_not_incremental")
                test.assertEqual(schema, patch_schema(base["snapshot_id"]))
                proposal = {"summary": "Repair calculations", "base_snapshot_id": base["snapshot_id"],
                            "finding_ids": [context["findings"][0]["id"]], "edits": [copy.deepcopy(FIRST_EDIT)]}
                if self.calls == 3:
                    feedback = context["retry_feedback"]
                    test.assertEqual(feedback["scope"], "visible_only_diagnostic_not_acceptance")
                    test.assertEqual(len(feedback["failed_cases"]), 1)
                    failed = feedback["failed_cases"][0]
                    test.assertEqual((failed["command"], failed["case_id"]), ("target", "target:2"))
                    test.assertEqual(failed["input"]["kwargs"], {"kind": "second", "value": 3})
                    test.assertEqual(failed["validation_results"], [{"status": "failed", "actual": 5, "expected": 23,
                                                                   "actual_available": True, "expected_available": True, "repetitions": [1, 2]}])
                    test.assertTrue(failed["reproduces_validation"])
                    test.assertEqual(failed["replay"]["status"], "completed")
                    test.assertEqual(failed["replay"]["result"]["actual"], 5)
                    test.assertNotEqual(feedback["candidate"]["version"], base["snapshot_id"])
                    spans = failed["replay"]["trace"]["spans"]
                    test.assertTrue(any(s["node"] == "second" and s.get("source", {}).get("path") == "helpers.py" for s in spans))
                    test.assertTrue(all(s.get("source", {}).get("function") != "first" for s in spans))
                    test.assertIn(failed["replay"]["case_evidence_id"], {s["evidence_id"] for s in spans})
                    original = next(s for s in context["sources"] if s["path"] == "helpers.py")
                    test.assertIn("    return value + 1", original["text"].splitlines())
                    candidate_hash = next(s for s in spans if s.get("source", {}).get("path") == "helpers.py")["metadata"]["source_sha256"]
                    test.assertNotEqual(original["sha256"], candidate_hash)
                    test.assertEqual(base["preserve_observed_improvements"], [{"command": "target", "case_id": "target:1", "candidate_version": feedback["candidate"]["version"]}])
                    test.assertEqual(context["previous_attempts"][-1]["proposal"]["edits"], [FIRST_EDIT])
                    proposal["edits"] = [copy.deepcopy(SECOND_EDIT)] if mode == "incremental" else [copy.deepcopy(FIRST_EDIT), copy.deepcopy(SECOND_EDIT)]
                    if mode == "wrong_base":
                        proposal["base_snapshot_id"] = feedback["candidate"]["version"]
                if mode != "wrong_base":
                    validate(proposal, schema, "test model response")
                return proposal

        return Model()

    def exercise(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, traces = fixture(root)
            before = {p.name: digest(p.read_bytes()) for p in project.iterdir()}
            model = self.model(mode)
            report = run(project, traces, mode="repair", executor="local", output=root / "runs", model=model)
            self.assertEqual(model.calls, 3, report["warnings"])
            self.assertEqual(before, {p.name: digest(p.read_bytes()) for p in project.iterdir()})
            feedback = report["attempts"][0]["retry_feedback"]
            audit = feedback["failed_cases"][0]["replay"]
            self.assertEqual(digest(Path(audit["audit_file"]).read_bytes()), audit["audit_sha256"])
            self.assertNotIn(HIDDEN, Path(audit["audit_file"]).read_text(encoding="utf-8"))
            self.assertEqual(report["snapshot_id"], model.contexts[2]["patch_base"]["snapshot_id"])
            self.assertEqual(report["attempts"][1]["base_snapshot_id"], report["snapshot_id"])
            if mode == "complete":
                self.assertEqual(report["status"], "verified", report["warnings"])
                text = (Path(report["candidate_project"]) / "helpers.py").read_text(encoding="utf-8")
                self.assertIn("return value + 10", text)
                self.assertIn("return value + 20", text)
            else:
                self.assertEqual(report["status"], "rejected", report["warnings"])
                last = report["attempts"][-1]
                if mode == "incremental":
                    self.assertEqual(last["failure_stage"], "visible_validation")
                    results = last["validation"]["commands"][0]["completion"]["results"]
                    self.assertEqual([r["status"] for r in results], ["failed", "passed"])
                else:
                    self.assertEqual(last["failure_stage"], "policy")
                    self.assertIn("original snapshot", last["reason"])

    def test_complete_retry_retains_first_fix_and_links_async_multifile_failure(self):
        self.exercise("complete")

    def test_incremental_retry_loses_first_fix_and_is_rejected_by_frozen_checks(self):
        self.exercise("incremental")

    def test_candidate_revision_cannot_be_used_as_patch_base(self):
        self.exercise("wrong_base")


class ReplayBoundariesTest(unittest.TestCase):
    def replay(self, source, *, timeout=3, max_spans=64):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, _ = fixture(root)
            (project / "subject.py").write_text(source, encoding="utf-8")
            config = load_config(project)
            command = config["validation"]["commands"][0]
            command["timeout_seconds"] = timeout
            command["replay"]["max_spans"] = max_spans
            manifest = snapshot(project, root / "candidate")
            case = visible_cases(project, command)["target:2"]
            result = _replay(root / "candidate", root / "replay", manifest, command, "target:2", case, config, ValidationRunner("local"))
            self.assertEqual(check_manifest(root / "candidate", manifest), [])
            return result, read_json(Path(result["audit_file"]))

    def test_import_exit_and_call_exit_are_completed_error_observations(self):
        for source in ("raise SystemExit(0)\n", "def run(kind, value):\n    raise SystemExit(0)\n"):
            with self.subTest(source=source):
                result, audit = self.replay(source)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["result"]["status"], "error")
                self.assertIn("SystemExit", result["result"]["error"])
                self.assertFalse(result["result"]["actual_available"])
                self.assertEqual(audit["execution"]["returncode"], 1)

    def test_zero_exit_without_evidence_is_incomplete(self):
        result, _ = self.replay("import os\nos._exit(0)\n")
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("trace", result)

    def test_timeout_and_mutating_replays_cannot_supply_trusted_traces(self):
        for source in ("import time\ndef run(kind,value):\n    time.sleep(5)\n",
                       "from pathlib import Path\ndef run(kind,value):\n    Path('added.py').write_text('x=1')\n    return 23\n"):
            with self.subTest(source=source):
                result, _ = self.replay(source, timeout=1)
                self.assertEqual(result["status"], "incomplete")
                self.assertNotIn("trace", result)

    def test_trace_capture_limit_is_explicit_and_keeps_case_link(self):
        result, _ = self.replay("def sub(value):\n    return value\ndef run(kind,value):\n    return sub(value)\n", max_spans=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["trace"]["status"], "partial")
        self.assertGreater(result["trace"]["omitted_calls"], 0)
        self.assertTrue(result["case_evidence_id"].endswith("/case"))

    def test_binding_validation_and_automatic_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            project, _ = fixture(Path(folder))
            config = load_config(project)
            self.assertFalse(editable("visible.json", {**config, "source_paths": ["**"]}))
            command = config["validation"]["commands"][0]
            cases = read_json(project / "visible.json")
            for index, case in enumerate(cases["target"], 1):
                case["id"] = f"target:{index}"
            cases["target"].reverse()
            write_json(project / "visible.json", cases)
            self.assertEqual(visible_cases(project, command)["target:1"]["kwargs"]["kind"], "first")
            cases["target"][0]["id"] = "not-visible"
            write_json(project / "visible.json", cases)
            with self.assertRaisesRegex(DoctorError, "case IDs"):
                load_config(project)
            command["replay"]["cases_file"] = "../oracle.json"
            with self.assertRaises(DoctorError):
                visible_cases(project, command)

    def test_replay_success_does_not_replace_original_failed_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, _ = fixture(root)
            config, runner = load_config(project), ValidationRunner("local")
            manifest = snapshot(project, root / "candidate")
            baseline = runner.run_suite(project, config, manifest)
            original = copy.deepcopy(baseline)
            path = root / "candidate/helpers.py"
            path.write_text(HELPERS.replace("value + 1", "value + 10").replace("value + 2", "value + 20"), encoding="utf-8")
            patched = {**manifest, "helpers.py": digest(path.read_bytes())}
            attempt = {"number": 1, "validation": baseline}
            feedback = failure_feedback(project, root / "candidate", root / "replay", manifest, patched, attempt, baseline, config, runner)
            self.assertEqual(len(feedback["failed_cases"]), 2)
            for case in feedback["failed_cases"]:
                self.assertEqual(case["replay"]["result"]["status"], "passed")
                self.assertFalse(case["reproduces_validation"])
            self.assertEqual(baseline, original)
            self.assertFalse(candidate_gate(baseline, attempt["validation"])[0])

    def test_missing_binding_is_explicit_and_never_guesses_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, _ = fixture(root)
            config, runner = load_config(project), ValidationRunner("local")
            config["validation"]["commands"][0].pop("replay")
            manifest = snapshot(project, root / "candidate")
            baseline = runner.run_suite(project, config, manifest)
            feedback = failure_feedback(project, root / "candidate", root / "replay", manifest, manifest,
                                        {"number": 1, "validation": baseline}, baseline, config, runner)
            self.assertEqual(len(feedback["failed_cases"]), 2)
            for case in feedback["failed_cases"]:
                self.assertFalse(case["input_available"])
                self.assertNotIn("input", case)
                self.assertEqual(case["replay"]["status"], "unavailable")
                self.assertNotIn("trace", case["replay"])

    def test_nested_protocol_values_are_redacted_in_trace_request_and_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, _ = fixture(root)
            (project / "subject.py").write_text("def run(**kwargs):\n    return kwargs\n", encoding="utf-8")
            config = load_config(project)
            command = config["validation"]["commands"][0]
            manifest = snapshot(project, root / "candidate")
            case = visible_cases(project, command)["target:2"]
            private_value = "sensitive-test-value-e3177"
            case["kwargs"]["api_key"] = private_value
            result = _replay(root / "candidate", root / "replay", manifest, command, "target:2", case, config, ValidationRunner("local"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["result"]["actual"]["api_key"], "[REDACTED]")
            for filename in ("audit.json", "request.json", "traces.json"):
                self.assertNotIn(private_value, (root / "replay" / filename).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
