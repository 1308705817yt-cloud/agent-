import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from agentdoctor.analysis import model_context
from agentdoctor.context_view import dependency_view, validation_view
from agentdoctor.core import digest, read_json, redact, write_json
from agentdoctor.engine import run
from agentdoctor.evidence import PREFIX, command_completed
from agentdoctor.project import load_config, snapshot

ROOT = Path(__file__).resolve().parents[1]


def decode_dependencies(view):
    resolved = Counter((g["from_path"], ref["name"], g["path"], *bounds)
                       for g in view["resolved"] for ref in g["references"] for bounds in ref["ranges"])
    unresolved = Counter((g["path"], ref["name"], line, g["reason"])
                         for g in view["unresolved"] for ref in g["references"] for line in ref["lines"])
    return resolved, unresolved


def row(repetition=1, detail="actual=7, expected=0", stdout="", status="failed"):
    completion = {"schema_version": 1, "invocation_id": f"invocation-{repetition}", "completed": True,
                  "executed_count": 1, "results": [{"id": "target:1", "status": status, "detail": detail}], "suite_error": None}
    return {"name": "target", "kind": "target", "repetition": repetition, "argv": ["{python}", "checks.py"],
            "expected_cases": ["target:1"], "invocation_id": completion["invocation_id"], "completion": completion,
            "returncode": 0 if status == "passed" else 1, "timed_out": False, "error": None,
            "completion_error": None, "duration_seconds": repetition / 10, "stdout_truncated": False, "stderr_truncated": False,
            "stdout": stdout + PREFIX + json.dumps(completion) + "\n", "stderr": ""}


class FeedbackProjectionTest(unittest.TestCase):
    def test_all_dependency_edges_occurrences_and_limits_round_trip(self):
        raw = {"status": "partial_static_subset", "method": "static", "limits": {"files": 32, "definitions": 256},
               "resolved": [{"from_path": "a.py", "path": "b.py", "reference": "api_key", "start_line": x, "end_line": x + 2} for x in (1, 10, 20)],
               "unresolved": [{"path": "a.py", "reference": "secret", "line": line, "reason": reason}
                              for reason, line in (("runtime_binding", 7), ("runtime_binding", 12), ("definition_limit", None))]}
        original = copy.deepcopy(raw)
        view = redact(dependency_view(raw))
        resolved, unresolved = decode_dependencies(view)
        self.assertEqual(resolved, Counter((e["from_path"], e["reference"], e["path"], e["start_line"], e["end_line"]) for e in raw["resolved"]))
        self.assertEqual(unresolved, Counter((e["path"], e["reference"], e["line"], e["reason"]) for e in raw["unresolved"]))
        self.assertEqual(view["limits"], raw["limits"])
        self.assertEqual(view["record_counts"], {"resolved": 3, "unresolved": 3})
        self.assertEqual(raw, original)

    def test_stdout_is_bounded_but_results_proposal_audit_and_repetitions_survive(self):
        log = "diagnostic " * 2000 + "\n"
        suite = {"commands": [row(1, stdout=log), row(2, stdout=log)], "integrity_changes": [], "executor": "local"}
        original = copy.deepcopy(suite)
        projected = validation_view(suite)
        self.assertEqual(len(projected["commands"]), 1)
        command = projected["commands"][0]
        self.assertEqual(command["repetitions"], [1, 2])
        self.assertEqual(projected["command_count"], 2)
        self.assertEqual(command["completion"]["results"], suite["commands"][0]["completion"]["results"])
        excerpt = command["stdout"]["additional_output"]
        self.assertEqual(excerpt["sha256"], digest(log))
        self.assertEqual(excerpt["omitted_chars"], len(log) - 1024)
        self.assertEqual(excerpt["head"], log[:512])
        self.assertEqual(excerpt["tail"], log[-512:])
        self.assertEqual(suite, original)
        self.assertTrue(all(command_completed(r) for r in suite["commands"]))

    def test_different_results_or_diagnostic_logs_do_not_merge(self):
        commands = [row(1), row(2, status="passed"), row(3, detail="different failure"), row(4, stdout="different log\n")]
        view = validation_view({"commands": commands, "integrity_changes": []})
        self.assertEqual([r["repetitions"] for r in view["commands"]], [[1], [2], [3], [4]])
        for source, projected in zip(commands, view["commands"]):
            self.assertEqual(projected["completion"]["results"], source["completion"]["results"])

    def test_timeouts_missing_forged_or_incomplete_protocol_keep_raw_logs(self):
        for failure in ("timeout", "missing", "mismatch", "truncated", "duplicate", "case_ids", "integrity"):
            source = row(stdout="unique failure detail\n" * 500)
            if failure == "timeout":
                source["timed_out"] = True
            elif failure == "missing":
                source["completion"] = None
            elif failure == "mismatch":
                source["completion"]["results"][0]["detail"] = "different from stdout"
            elif failure == "truncated":
                source["stdout_truncated"] = True
            elif failure == "duplicate":
                source["stdout"] += PREFIX + json.dumps(source["completion"]) + "\n"
            elif failure == "case_ids":
                source["expected_cases"] = ["target:2"]
            suite = {"commands": [source], "integrity_changes": ["added.py"] if failure == "integrity" else []}
            view = validation_view(suite)
            with self.subTest(failure=failure):
                if failure != "integrity":
                    self.assertEqual(view["commands"][0]["stdout"], source["stdout"])
                self.assertEqual(view["commands"][0]["completion"].get("results") if view["commands"][0]["completion"] else None,
                                 source["completion"].get("results") if source["completion"] else None)
                self.assertEqual(view["integrity_changes"], suite["integrity_changes"])

    def test_failure_detail_and_stderr_are_never_sampled(self):
        source = row(detail="important middle evidence " * 2000)
        source["stderr"] = "full traceback " * 2000
        view = validation_view({"commands": [source]})["commands"][0]
        self.assertEqual(view["completion"]["results"], source["completion"]["results"])
        self.assertEqual(view["stderr"], source["stderr"])

    def test_credential_redaction_precedes_optional_log_hashing(self):
        source = row(stdout="api_key=fixture-private-key\n")
        view = validation_view({"commands": [source]})
        excerpt = view["commands"][0]["stdout"]["additional_output"]
        self.assertNotIn("fixture-private-key", json.dumps(view))
        self.assertEqual(excerpt["sha256"], digest(redact("api_key=fixture-private-key\n")))


class ArchivedFeedbackRegressionTest(unittest.TestCase):
    def test_all_archived_overflows_fit_original_budgets_without_losing_dependencies(self):
        for filename in ("codex-retry-long-feedback-v0.5.3-2026-10-13.json", "codex-public-smolagents-v0.5.3-2026-10-13.json"):
            archive = read_json(ROOT / "benchmarks" / filename, limit=50_000_000)
            raw = archive["raw_report"]
            for row_ in raw["cases"]:
                with self.subTest(case=row_["id"]), tempfile.TemporaryDirectory() as folder:
                    source = (ROOT / "agentdoctor/workflow_data/cases/dependency_contract_fault" if raw["group"] == "retry" else
                              ROOT / "benchmarks/public-smolagents-v1/cases" / row_["id"])
                    project = Path(folder) / "project"
                    manifest = snapshot(source, project)
                    engine = row_["engine"]
                    history = [{"baseline": engine["baseline"]}] if row_["faulty"] else []
                    history += [{k: a.get(k) for k in ("summary", "reason", "validation", "proposal")} for a in engine["attempts"]]
                    original = copy.deepcopy((engine["diagnosis"], history))
                    audit = {}
                    config = load_config(project)
                    context = model_context(project, manifest, row_["traces"], engine["diagnosis"], config, history, audit=audit)
                    self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), config["policy"]["max_context_chars"])
                    self.assertEqual((engine["diagnosis"], history), original)
                    self.assertEqual(audit["previous_attempts"], history)
                    dep = audit["dependency_context"]
                    expected = (Counter((e["from_path"], e["reference"], e["path"], e["start_line"], e["end_line"]) for e in dep["resolved"]),
                                Counter((e["path"], e["reference"], e["line"], e["reason"]) for e in dep["unresolved"]))
                    self.assertEqual(decode_dependencies(context["dependency_context"]), expected)
                    if engine["attempts"]:
                        self.assertEqual(context["previous_attempts"][-1]["proposal"], engine["attempts"][-1]["proposal"])

    def test_formal_retry_after_long_feedback_keeps_full_audit_and_reaches_model(self):
        archive = read_json(ROOT / "benchmarks/codex-retry-long-feedback-v0.5.3-2026-10-13.json", limit=50_000_000)
        old = archive["raw_report"]["cases"][0]
        test = self

        class Model:
            provider, name, max_calls, calls = "test", "replay-and-preset", 3, 0

            def __init__(self):
                self.usage = []

            def complete(self, operation, context, schema):
                self.calls += 1
                if self.calls == 1:
                    return copy.deepcopy(old["engine"]["model_events"][0]["output"])
                if self.calls == 2:
                    return copy.deepcopy(old["engine"]["attempts"][0]["proposal"])
                test.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 24000)
                feedback = context["previous_attempts"][-1]
                test.assertEqual(feedback["proposal"], old["engine"]["attempts"][0]["proposal"])
                results = [c for c in feedback["validation"]["commands"] if c["kind"] == "target"]
                test.assertTrue(all(c["completion"]["results"][0]["status"] == "failed" for c in results))
                return {"summary": "Keep integer quantity", "finding_ids": [context["findings"][0]["id"]],
                        "edits": [{"path": "workflow/codec.py", "before": 'return {"quantity": str(quantity)}',
                                   "after": 'return {"quantity": quantity}', "reason": "Preserve tool input type and value"}]}

        with tempfile.TemporaryDirectory() as folder:
            project, traces = Path(folder) / "project", Path(folder) / "traces.json"
            snapshot(ROOT / "agentdoctor/workflow_data/cases/dependency_contract_fault", project)
            write_json(traces, old["traces"])
            result = run(project, traces, mode="repair", output=Path(folder) / "runs", executor="local", model=Model())
            self.assertEqual(result["status"], "verified", result["warnings"])
            self.assertEqual(result["attempts"][0]["failure_stage"], "visible_validation")
            event = result["model_events"][-1]
            path = Path(event["context_audit_file"])
            self.assertEqual(digest(path.read_bytes()), event["context_audit_sha256"])
            audit = read_json(path)
            self.assertEqual(audit["previous_attempts"][-1]["validation"], result["attempts"][0]["validation"])
            self.assertIn("retry-diagnostic " * 256, audit["previous_attempts"][-1]["validation"]["commands"][0]["stdout"])
