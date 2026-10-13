"""Adapter protocol tests use a fake local process, never a model service."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdoctor.codex_cli import CodexCLIClient, codex_command
from agentdoctor.contracts import obj
from agentdoctor.core import DoctorError


DRIVER = '''import json, sys, time
from pathlib import Path
mode = sys.argv[1]
prompt = sys.stdin.read()
assert "do-not-transmit" not in prompt
assert "[REDACTED]" in prompt
assert sorted(p.name for p in Path.cwd().iterdir()) == ["schema.json"]
output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
if mode == "timeout":
    time.sleep(30)
if mode != "missing_result":
    output.write_text(json.dumps({"ok": True} if mode != "invalid_result" else {"other": 1}))
if mode == "tool":
    print(json.dumps({"type": "item.started", "item": {"type": "command_execution"}}))
if mode == "bad_event":
    print("[]")
if mode == "failed_turn":
    print(json.dumps({"type": "turn.failed", "error": {"message": "test failure"}}))
if mode != "missing_completion":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}))
sys.exit(1 if mode == "exit_failure" else 0)
'''


class CodexCLIProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.driver = Path(self.temporary.name) / "fake_cli.py"
        self.driver.write_text(DRIVER, encoding="utf-8")

    def client(self, mode="success", **kwargs):
        return CodexCLIClient(command=[sys.executable, str(self.driver), mode], **kwargs)

    def complete(self, client):
        return client.complete("protocol test", {"api_key": "do-not-transmit"}, obj({"ok": {"type": "boolean"}}))

    def test_completed_structured_result_and_usage(self):
        client = self.client()
        self.assertEqual(self.complete(client), {"ok": True})
        self.assertEqual(client.calls, 1)
        self.assertEqual(client.usage, [{"input_tokens": 3, "output_tokens": 2}])

    def test_zero_exit_needs_both_completion_and_schema_valid_result(self):
        for mode in ("missing_result", "missing_completion", "invalid_result", "bad_event", "failed_turn", "exit_failure"):
            with self.subTest(mode=mode), self.assertRaises(DoctorError):
                self.complete(self.client(mode))

    def test_tool_action_invalidates_result(self):
        with self.assertRaisesRegex(DoctorError, "tool action"):
            self.complete(self.client("tool"))

    def test_timeout_reaps_process_and_counts_attempt(self):
        client = self.client("timeout", timeout=1)
        with self.assertRaisesRegex(DoctorError, "timed out"):
            self.complete(client)
        self.assertEqual(client.calls, 1)

    def test_budget_does_not_launch_process(self):
        with patch("agentdoctor.codex_cli.subprocess.Popen") as launch:
            with self.assertRaisesRegex(DoctorError, "budget"):
                self.complete(self.client(max_calls=0))
            launch.assert_not_called()

    def test_arguments_disable_tools_and_user_configuration(self):
        folder = Path(self.temporary.name)
        argv = self.client(name="explicit-model").arguments(folder, folder / "schema.json", folder / "result.json")
        for flag in ("--ignore-user-config", "--ephemeral", "--output-schema", "--json"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--model") + 1], "explicit-model")
        self.assertIn('approval_policy="never"', argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn("project_doc_max_bytes=0", argv)
        disabled = {argv[i + 1] for i, value in enumerate(argv) if value == "--disable"}
        self.assertTrue({"shell_tool", "apps", "plugins", "hooks", "multi_agent", "memories"} <= disabled)
        self.assertEqual(argv[-1], "-")

    def test_windows_npm_shim_uses_node_without_shell(self):
        folder = Path(self.temporary.name)
        entrypoint = folder / "node_modules/@openai/codex/bin/codex.js"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.touch()
        with patch("agentdoctor.codex_cli.shutil.which", side_effect=lambda name: str(folder / "codex.cmd") if name == "codex" else "node.exe"):
            self.assertEqual(codex_command(), ["node.exe", str(entrypoint)])

    def test_unauthenticated_preflight_does_not_launch_model(self):
        from subprocess import CompletedProcess
        with patch("agentdoctor.codex_cli.subprocess.run", side_effect=[CompletedProcess([], 1), CompletedProcess([], 0, stdout="codex-cli test")]) as launch:
            with self.assertRaisesRegex(DoctorError, "not logged in"):
                self.client().preflight()
            self.assertEqual(len(launch.call_args_list), 2)
            self.assertFalse(any("exec" in c.args[0] for c in launch.call_args_list))


if __name__ == "__main__":
    unittest.main()
