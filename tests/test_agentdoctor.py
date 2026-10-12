from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentdoctor.contracts import violations
from agentdoctor.core import DoctorError, contained_path, digest, parse_json, read_json, redact, write_json
from agentdoctor.detectors import detect, diagnose
from agentdoctor.engine import add_model_analysis, run
from agentdoctor.model import ANALYSIS_SCHEMA, PATCH_SCHEMA, ModelClient
from agentdoctor.patches import alias_proposals, apply_proposal
from agentdoctor.project import check_manifest, load_config, snapshot
from agentdoctor.runner import ValidationRunner, baseline_gate, candidate_gate, execute
from agentdoctor.traces import TraceRecorder, load_traces

FIXTURE = Path(__file__).resolve().parents[1] / "agentdoctor" / "demo_project"


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.manifest = snapshot(FIXTURE, self.project)
        self.config = load_config(self.project)
        result = execute([sys.executable, "record_trace.py"], self.project, 10)
        self.assertEqual(result["returncode"], 0, result)
        self.spans = load_traces(self.project / "trace.jsonl")
        self.findings = detect(self.spans, self.config, self.project, self.manifest)

    def tearDown(self):
        self.temporary.cleanup()

    def proposal(self):
        return alias_proposals(self.project, self.spans, self.findings, self.config)[0]

    def apply(self, proposal):
        return apply_proposal(self.project, proposal, self.config, {f["id"] for f in self.findings}, self.manifest)

    def test_real_bug_detected_and_aliased_at_correct_source(self):
        issue = next(f for f in self.findings if f["category"] == "tool_argument_schema")
        self.assertEqual(issue["source"]["function"], "lookup_customer")
        self.assertEqual(issue["evidence"], ["demo-lookup/tool-1"])
        proposal = self.proposal()
        self.assertEqual(proposal["edits"][0]["before"], '{"user_id": customer_id}')
        self.assertIn("customer_id", proposal["edits"][0]["after"])

    def test_no_alias_means_no_guessed_automatic_rename(self):
        self.config["tools"]["lookup_customer"]["aliases"] = {}
        self.assertEqual(alias_proposals(self.project, self.spans, self.findings, self.config), [])

    def test_unknown_source_function_yields_no_rule_patch(self):
        self.spans[0]["source"]["function"] = "not_present"
        self.assertEqual(alias_proposals(self.project, self.spans, self.findings, self.config), [])

    def test_protected_evaluator_cannot_be_changed_even_if_allowlisted(self):
        proposal = self.proposal()
        proposal["edits"][0].update(path="checks.py", before="raise SystemExit", after="print")
        self.config["source_paths"].append("checks.py")
        with self.assertRaisesRegex(DoctorError, "protected"):
            self.apply(proposal)

    def test_path_escape_is_rejected(self):
        proposal = self.proposal()
        for path in ("../outside.py", "C:/outside.py", "/outside.py", "src/../../outside.py", "agent.py:secret", "src\\agent.py"):
            with self.subTest(path=path):
                proposal["edits"][0]["path"] = path
                with self.assertRaises(DoctorError):
                    self.apply(proposal)

    def test_stale_source_hash_is_rejected(self):
        proposal = self.proposal()
        with (self.project / "agent.py").open("a", encoding="utf-8") as stream:
            stream.write("\n# concurrent change\n")
        with self.assertRaisesRegex(DoctorError, "Source changed"):
            self.apply(proposal)

    def test_multiple_matches_are_rejected(self):
        proposal = self.proposal()
        proposal["edits"][0].update(before="customer_id", after="identifier")
        with self.assertRaisesRegex(DoctorError, "exactly one"):
            self.apply(proposal)

    def test_invalid_python_patch_is_atomic(self):
        original = (self.project / "agent.py").read_bytes()
        proposal = self.proposal()
        proposal["edits"][0]["after"] = "{"
        with self.assertRaisesRegex(DoctorError, "invalid Python"):
            self.apply(proposal)
        self.assertEqual((self.project / "agent.py").read_bytes(), original)

    def test_fabricated_finding_reference_is_rejected(self):
        proposal = self.proposal()
        proposal["finding_ids"] = ["invented-finding"]
        with self.assertRaisesRegex(DoctorError, "existing finding"):
            self.apply(proposal)

    def test_changed_line_budget_is_enforced(self):
        self.config["policy"]["max_changed_lines"] = 1
        with self.assertRaisesRegex(DoctorError, "changed-line"):
            self.apply(self.proposal())

    def test_full_repair_preserves_original_and_frozen_checks(self):
        original = (self.project / "agent.py").read_bytes()
        frozen = (self.project / "checks.py").read_bytes()
        report = run(self.project, self.project / "trace.jsonl", mode="repair", output=self.root / "runs", executor="local")
        self.assertEqual(report["status"], "verified", report["reason"])
        self.assertEqual((self.project / "agent.py").read_bytes(), original)
        self.assertEqual((Path(report["candidate_project"]) / "checks.py").read_bytes(), frozen)
        self.assertEqual(len(report["baseline"]["commands"]), 4)
        self.assertTrue(all(r["returncode"] == 0 for r in report["attempts"][0]["validation"]["commands"]))
        self.assertTrue(Path(report["verified_patch"]).is_file())
        self.assertTrue((Path(report["workspace"]) / "report.html").is_file())

    def test_diagnosis_does_not_execute_target(self):
        with patch("agentdoctor.engine.ValidationRunner", side_effect=AssertionError("Must not execute")):
            report = run(self.project, self.project / "trace.jsonl", output=self.root / "runs")
        self.assertEqual(report["status"], "diagnosed")
        self.assertIsNone(report["baseline"])

    def test_missing_executor_cannot_yield_verified(self):
        with patch.object(ValidationRunner, "available", return_value=(False, "No Docker")):
            report = run(self.project, self.project / "trace.jsonl", mode="repair", output=self.root / "runs")
        self.assertEqual(report["status"], "inconclusive")
        self.assertIsNone(report["verified_patch"])

    def test_no_validation_commands_cannot_yield_verified(self):
        config = read_json(self.project / "doctor.json")
        config["validation"]["commands"] = []
        write_json(self.project / "doctor.json", config)
        report = run(self.project, self.project / "trace.jsonl", mode="repair", output=self.root / "runs", executor="local")
        self.assertEqual(report["status"], "inconclusive")

    def test_original_change_during_validation_invalidates_delivery(self):
        original_run = ValidationRunner.run_suite
        project = self.project
        def mutate_after_candidate(runner, candidate, config, manifest):
            result = original_run(runner, candidate, config, manifest)
            if candidate.name.startswith("candidate-"):
                with (project / "agent.py").open("a", encoding="utf-8") as stream:
                    stream.write("\n# external concurrent edit\n")
            return result
        with patch.object(ValidationRunner, "run_suite", mutate_after_candidate):
            report = run(self.project, self.project / "trace.jsonl", mode="repair", output=self.root / "runs", executor="local")
        self.assertEqual(report["status"], "inconclusive")
        self.assertIsNone(report["verified_patch"])

    def test_docker_invocation_is_offline_read_only_and_cleans_up(self):
        calls = []
        def fake_execute(argv, cwd, timeout):
            calls.append(argv)
            return {"returncode": 0, "timed_out": False, "error": None, "stdout": "", "stderr": "", "duration_seconds": 0}
        with patch("agentdoctor.runner.shutil.which", return_value="docker"), patch("agentdoctor.runner.execute", side_effect=fake_execute):
            suite = ValidationRunner("docker").run_suite(self.project, self.config, self.manifest)
        self.assertEqual(len(suite["commands"]), 4)
        executions = [argv for argv in calls if argv[1] == "run"]
        self.assertEqual(len(executions), 4)
        for argv in executions:
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertEqual(argv[argv.index("--pull") + 1], "never")
            self.assertIn("--read-only", argv)
            self.assertTrue(argv[argv.index("--mount") + 1].endswith(",readonly"))
        self.assertEqual(len([argv for argv in calls if argv[1:3] == ["rm", "-f"]]), 4)

    def test_unknown_configuration_key_is_rejected(self):
        config = read_json(self.project / "doctor.json")
        config["polciy"] = {}
        write_json(self.project / "doctor.json", config)
        with self.assertRaises(DoctorError):
            load_config(self.project)

    def test_fabricated_model_evidence_does_not_partially_merge(self):
        diagnosis = diagnose(self.findings, self.spans)
        count = len(diagnosis["findings"])
        result = {"findings": [{"category": "prompt_conflict", "severity": "medium", "summary": "test", "evidence": ["missing/span"]}], "hypotheses": []}
        with self.assertRaisesRegex(DoctorError, "fabricated"):
            add_model_analysis(diagnosis, result, self.spans, self.manifest)
        self.assertEqual(len(diagnosis["findings"]), count)

    def test_retry_feedback_can_lead_to_verified_model_patch(self):
        valid = self.proposal()
        wrong = copy.deepcopy(valid)
        wrong["summary"] = "Wrong default customer"
        wrong["edits"][0]["after"] = "{'customer_id': 'C001'}"
        config = read_json(self.project / "doctor.json")
        config["tools"]["lookup_customer"]["aliases"] = {}
        write_json(self.project / "doctor.json", config)
        seen_contexts = []

        class FakeModel:
            provider = "test"
            name = "fixture"
            max_calls = 8
            calls = 0
            usage = []

            def complete(self, operation, context, schema):
                self.calls += 1
                if schema == ANALYSIS_SCHEMA:
                    return {"findings": [], "hypotheses": []}
                seen_contexts.append(context)
                return wrong if len(seen_contexts) == 1 else valid

        report = run(self.project, self.project / "trace.jsonl", mode="repair", output=self.root / "runs", executor="local", model=FakeModel())
        self.assertEqual(report["status"], "verified", report)
        self.assertEqual([a["status"] for a in report["attempts"]], ["rejected", "verified"])
        self.assertIn("Candidate failed", json.dumps(seen_contexts[1]))
        self.assertNotEqual(Path(report["verified_patch"]).name, "candidate-1.patch")


class ContractsAndTracesTest(unittest.TestCase):
    def test_schema_detects_nested_types_and_extra_fields(self):
        schema = {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"], "additionalProperties": False}
        self.assertEqual(violations({"ids": [1, 2]}, schema), [])
        self.assertEqual(len(violations({"ids": ["1"], "extra": 2}, schema)), 2)

    def test_external_schema_refs_are_never_fetched(self):
        with self.assertRaisesRegex(DoctorError, "in-document"):
            violations({}, {"$ref": "https://example.test/schema"})

    def test_internal_schema_refs_work(self):
        self.assertEqual(violations(3, {"$defs": {"id": {"type": "integer"}}, "$ref": "#/$defs/id"}), [])

    def test_duplicate_json_and_non_finite_values_rejected(self):
        for value in ('{"a":1,"a":2}', '{"number":NaN}', '{"number":Infinity}'):
            with self.subTest(value=value), self.assertRaises(DoctorError):
                parse_json(value)

    def test_recorder_captures_actual_args_and_redacts_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.jsonl"
            recorder = TraceRecorder(path, "test-run")
            def tool(customer_id, api_key):
                return {"id": customer_id}
            wrapped = recorder.wrap("lookup", tool, kind="tool", tool="lookup")
            self.assertEqual(wrapped("C001", "secret_value"), {"id": "C001"})
            spans = load_traces(path)
            self.assertEqual(spans[0]["input"]["customer_id"], "C001")
            self.assertEqual(spans[0]["input"]["api_key"], "[REDACTED]")
            self.assertNotIn("secret_value", path.read_text())

    def test_async_recorder_preserves_parent_child_links(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = TraceRecorder(Path(folder) / "trace.jsonl", "run")
            async def child(value):
                return value + 1
            child = recorder.wrap("child", child)
            async def parent(value):
                return await child(value)
            parent = recorder.wrap("parent", parent)
            self.assertEqual(asyncio.run(parent(2)), 3)
            spans = load_traces(recorder.path)
            self.assertEqual(spans[0]["parent_id"], spans[1]["span_id"])

    def test_async_cancellation_resets_parent_context(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = TraceRecorder(Path(folder) / "trace.jsonl", "run")
            async def cancelled():
                raise asyncio.CancelledError()
            observed = recorder.wrap("cancelled", cancelled)
            async def exercise():
                with self.assertRaises(asyncio.CancelledError):
                    await observed()
                self.assertIsNone(recorder._parent.get())
            asyncio.run(exercise())
            self.assertIn("CancelledError", load_traces(recorder.path)[0]["error"])

    def test_state_mutation_cannot_overwrite_recorded_input(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = TraceRecorder(Path(folder) / "trace.jsonl")
            def mutate(state):
                state["customer"] = "new"
                return state
            wrapped = recorder.wrap("mutate", mutate)
            wrapped({"customer": "old"})
            span = load_traces(recorder.path)[0]
            self.assertEqual(span["input"]["state"]["customer"], "old")
            self.assertEqual(span["output"]["customer"], "new")

    def test_cycle_and_duplicate_span_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.json"
            span = {"run_id": "r", "span_id": "s", "parent_id": "s", "node": "n", "kind": "node"}
            write_json(path, [span])
            with self.assertRaisesRegex(DoctorError, "cycle"):
                load_traces(path)
            span.pop("parent_id")
            write_json(path, [span, span])
            with self.assertRaisesRegex(DoctorError, "Duplicate"):
                load_traces(path)

    def test_redaction_does_not_hide_business_token_counts(self):
        redacted = redact({"api_key": "secret", "input_tokens": 42, "nested": {"authorization": "Bearer longsecretvalue"}})
        self.assertEqual(redacted["input_tokens"], 42)
        self.assertEqual(redacted["nested"]["authorization"], "[REDACTED]")


class DetectorTest(unittest.TestCase):
    def config(self):
        return {"tools": {"lookup": {"input_schema": {"type": "object"}}}, "checks": {"loop_threshold": 3, "preserve_state_keys": ["customer_id"]}, "source_paths": ["*.py"], "policy": {"protected_paths": []}}

    def test_loop_is_scoped_by_run_and_requires_no_progress(self):
        spans = [{"run_id": "r", "span_id": str(i), "node": "n", "kind": "tool", "tool": "lookup", "input": {"id": 1}, "error": "timeout"} for i in range(3)]
        issues = detect(spans, self.config(), Path.cwd(), {})
        self.assertEqual(len([i for i in issues if i["category"] == "possible_loop"]), 1)
        spans[2]["run_id"] = "other-run"
        self.assertFalse(any(i["category"] == "possible_loop" for i in detect(spans, self.config(), Path.cwd(), {})))
        spans[2]["run_id"] = "r"
        for index, span in enumerate(spans):
            span.update(state_before={"counter": index}, state_after={"counter": index + 1})
        self.assertFalse(any(i["category"] == "possible_loop" for i in detect(spans, self.config(), Path.cwd(), {})))

    def test_state_invariant_and_false_completion(self):
        span = {"run_id": "r", "span_id": "1", "node": "n", "kind": "task", "task_status": "completed", "state_before": {"customer_id": "C001"}, "state_after": {}}
        categories = {f["category"] for f in detect([span], self.config(), Path.cwd(), {})}
        self.assertEqual(categories, {"state_invariant", "unverified_completion"})

    def test_invalid_json_output_is_distinguished_from_schema_error(self):
        span = {"run_id": "r", "span_id": "1", "node": "n", "kind": "llm", "output": "```json {} ```", "output_schema": {"type": "object"}}
        self.assertEqual(detect([span], self.config(), Path.cwd(), {})[0]["category"], "invalid_json")


class RunnerTest(unittest.TestCase):
    @staticmethod
    def suite(target=1, regression=0):
        return {"commands": [{"name": name, "kind": kind, "repetition": repeat, "returncode": code, "timed_out": False, "error": None} for name, kind, code in [("broken", "target", target), ("working", "regression", regression)] for repeat in (1, 2)], "integrity_changes": []}

    def test_no_reproduced_failure_is_inconclusive(self):
        self.assertFalse(baseline_gate(self.suite(target=0))[0])

    def test_broken_regression_baseline_is_inconclusive(self):
        self.assertFalse(baseline_gate(self.suite(regression=1))[0])

    def test_missing_exit_code_is_not_a_reproduced_failure(self):
        suite = self.suite(target=None)
        self.assertFalse(baseline_gate(suite)[0])

    def test_flaky_failure_does_not_count_as_reproduced(self):
        suite = self.suite()
        suite["commands"][1]["returncode"] = 0
        self.assertFalse(baseline_gate(suite)[0])

    def test_all_checks_required_for_verification(self):
        self.assertTrue(candidate_gate(self.suite(), self.suite(target=0))[0])
        self.assertFalse(candidate_gate(self.suite(), self.suite(target=0, regression=1))[0])
        incomplete = self.suite(target=0)
        incomplete["commands"].pop()
        self.assertFalse(candidate_gate(self.suite(), incomplete)[0])

    def test_frozen_source_mutation_rejects_otherwise_passing_candidate(self):
        candidate = self.suite(target=0)
        candidate["integrity_changes"] = ["checks.py"]
        self.assertFalse(candidate_gate(self.suite(), candidate)[0])

    def test_timeout_is_recorded_and_does_not_pass(self):
        result = execute([sys.executable, "-c", "import time; time.sleep(10)"], Path.cwd(), 1)
        self.assertTrue(result["timed_out"])

    def test_local_test_does_not_inherit_model_api_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sensitive-test-key"}):
            result = execute([sys.executable, "-c", "import os; print(os.environ.get('OPENAI_API_KEY', 'absent'))"], Path.cwd(), 5)
        self.assertEqual(result["stdout"].strip(), "absent")

    def test_output_is_bounded(self):
        result = execute([sys.executable, "-c", "print('x' * 200000)"], Path.cwd(), 5)
        self.assertLess(len(result["stdout"]), 66000)
        self.assertIn("truncated", result["stdout"])

    def test_snapshot_excludes_secrets_and_git(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            source.mkdir()
            (source / ".env").write_text("SECRET=1")
            (source / ".git").mkdir()
            (source / ".git" / "config").write_text("credential")
            (source / "safe.py").write_text("value=1")
            result = snapshot(source, Path(folder) / "copy")
            self.assertEqual(list(result), ["safe.py"])


class ModelTest(unittest.TestCase):
    def test_openai_refusal_and_truncation_are_not_valid_outputs(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            model = ModelClient("openai", "test-model")
        for data in ({"status": "incomplete"}, {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]}):
            with self.subTest(data=data), self.assertRaises(DoctorError):
                model.parse_response(data)

    def test_openai_extracts_message_after_reasoning_item(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            model = ModelClient("openai", "test-model")
        result = model.parse_response({"status": "completed", "output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}]})
        self.assertEqual(result, {"ok": True})

    def test_remote_plain_http_is_rejected(self):
        with self.assertRaisesRegex(DoctorError, "HTTPS"):
            ModelClient("ollama", "test", "http://example.test")

    def test_model_call_budget_is_enforced_without_network(self):
        model = ModelClient("ollama", "test", max_calls=0)
        with self.assertRaisesRegex(DoctorError, "budget"):
            model.complete("test", {}, ANALYSIS_SCHEMA)

    def test_both_http_adapters_send_and_validate_real_requests(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append((self.path, payload))
                text = json.dumps({"findings": [], "hypotheses": []})
                data = {"done": True, "message": {"content": text}} if self.path == "/api/chat" else {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
                encoded = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                for provider in ("ollama", "openai"):
                    model = ModelClient(provider, "fixture-model", base)
                    self.assertEqual(model.complete("test", {"api_key": "do-not-transmit"}, ANALYSIS_SCHEMA), {"findings": [], "hypotheses": []})
            self.assertNotIn("do-not-transmit", json.dumps(captured))
            self.assertEqual(captured[0][0], "/api/chat")
            self.assertEqual(captured[1][0], "/responses")
            self.assertFalse(captured[1][1]["store"])
            self.assertTrue(captured[1][1]["text"]["format"]["strict"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
