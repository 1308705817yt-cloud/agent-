import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentdoctor.cli import _model_setup, main
from agentdoctor.core import DoctorError, read_json, write_json
from agentdoctor.project import snapshot
from agentdoctor.runner import execute
import sys

FIXTURE = Path(__file__).resolve().parents[1] / "agentdoctor/demo_project"


class FakeCodex:
    provider = "codex"
    instances = []
    def __init__(self, name="codex-default", metadata=None):
        self.name, self.metadata = name, metadata or {}
        self.calls, self.max_calls, self.usage = 0, 8, []
        self.preflights = 0
        self.instances.append(self)

    def preflight(self):
        self.preflights += 1
        self.metadata = {"version": "test-cli", "authenticated": True}
        return self.metadata

    def complete(self, operation, context, schema):
        self.calls += 1
        if "findings" in schema["properties"]:
            return {"findings": [], "hypotheses": []}
        return {"summary": "Correct argument mapping", "finding_ids": [next(f["id"] for f in context["findings"] if f["category"] == "tool_argument_schema")],
                "edits": [{"path": "agent.py", "before": '{"user_id": customer_id}', "after": '{"customer_id": customer_id}', "reason": "Match the tool contract"}]}


class FormalCLIProviderTest(unittest.TestCase):
    def test_diagnose_and_repair_codex_reach_engine_and_model_proposal(self):
        FakeCodex.instances = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            snapshot(FIXTURE, project)
            collected = execute([sys.executable, "record_trace.py"], project, 15)
            self.assertEqual(collected["returncode"], 0)
            config = read_json(project / "doctor.json")
            config["tools"]["lookup_customer"]["aliases"] = {}
            write_json(project / "doctor.json", config)
            original = (project / "agent.py").read_bytes()
            for mode in ("diagnose", "repair"):
                argv = [mode, str(project), "--traces", str(project / "trace.jsonl"), "--provider", "codex", "--output", str(root / mode)]
                if mode == "repair":
                    argv += ["--executor", "local", "--model", "chosen-model"]
                captured = []
                with patch("agentdoctor.codex_cli.CodexCLIClient", FakeCodex), patch("agentdoctor.cli._summary", side_effect=captured.append), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(argv), 0)
                self.assertEqual(captured[0]["model"]["provider"], "codex")
                self.assertEqual(captured[0]["model"]["metadata"]["version"], "test-cli")
                if mode == "repair":
                    self.assertEqual(captured[0]["status"], "verified")
                    self.assertEqual(captured[0]["attempts"][0]["origin"], "model")
                    self.assertEqual(captured[0]["model"]["name"], "chosen-model")
            self.assertEqual([m.preflights for m in FakeCodex.instances], [1, 1])
            self.assertEqual([m.calls for m in FakeCodex.instances], [1, 2])
            self.assertEqual((project / "agent.py").read_bytes(), original)

    def test_benchmark_factory_uses_fresh_budgets_after_one_preflight(self):
        options = SimpleNamespace(provider="codex", model=None, base_url=None)
        with patch("agentdoctor.codex_cli.CodexCLIClient", FakeCodex):
            initial, factory, metadata = _model_setup(options)
            first, second = factory(), factory()
        first.calls = 4
        self.assertEqual(second.calls, 0)
        self.assertEqual(initial.preflights, 1)
        self.assertEqual(second.preflights, 0)
        self.assertEqual(second.metadata, metadata)

    def test_codex_does_not_silently_ignore_an_api_endpoint(self):
        with self.assertRaisesRegex(DoctorError, "base-url"):
            _model_setup(SimpleNamespace(provider="codex", model=None, base_url="https://example.invalid"))

    def test_unauthenticated_formal_cli_stops_before_engine(self):
        with patch("agentdoctor.codex_cli.CodexCLIClient", FakeCodex), patch.object(FakeCodex, "preflight", side_effect=DoctorError("login unavailable")), patch("agentdoctor.cli.run") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["repair", ".", "--traces", "unused.jsonl", "--provider", "codex"]), 1)
            run.assert_not_called()
