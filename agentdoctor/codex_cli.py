"""Use the user's authenticated Codex CLI as a structured proposal provider."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import validate
from .core import DoctorError, parse_json, read_json, redact, write_json
from .model import SYSTEM
from .runner import _kill_tree


def codex_command() -> list[str]:
    executable = shutil.which("codex")
    if not executable:
        raise DoctorError("Codex CLI is unavailable on PATH")
    path = Path(executable)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        # Avoid PowerShell execution policies and cmd.exe quoting of user data.
        entrypoint = path.parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node")
        if node and entrypoint.is_file():
            return [node, str(entrypoint)]
        native = shutil.which("codex.exe")
        if native:
            return [native]
        raise DoctorError("Cannot locate a native Codex executable or its Node entrypoint")
    return [executable]


@dataclass
class CodexCLIClient:
    name: str = "codex-default"
    timeout: int = 300
    max_calls: int = 8
    calls: int = 0
    usage: list[dict] = field(default_factory=list)
    provider: str = field(default="codex", init=False)
    command: list[str] = field(default_factory=codex_command)
    metadata: dict = field(default_factory=dict)

    def preflight(self) -> dict:
        options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 15,
                   "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
        try:
            status = subprocess.run([*self.command, "login", "status"], **options)
            version = subprocess.run([*self.command, "--version"], **options)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DoctorError(f"Codex CLI preflight failed: {type(exc).__name__}") from exc
        if status.returncode:
            raise DoctorError("Codex CLI is not logged in; run codex.cmd login (Windows) or codex login")
        if version.returncode:
            raise DoctorError("Cannot determine Codex CLI version")
        self.metadata = {"version": version.stdout.strip(), "authenticated": True}
        return self.metadata

    def arguments(self, workdir: Path, schema_path: Path, result_path: Path) -> list[str]:
        argv = [*self.command, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--json", "--color", "never", "--cd", str(workdir),
                "--output-schema", str(schema_path), "--output-last-message", str(result_path),
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0"]
        # The CLI receives only the same redacted context as other providers.
        # Disable filesystem/connector tools and external instruction sources;
        # a fresh temp directory has no dataset, gold labels, or sibling cases.
        for feature in ("shell_tool", "unified_exec", "code_mode_host", "apps", "plugins", "hooks",
                        "multi_agent", "browser_use", "computer_use", "image_generation", "skill_search", "memories"):
            argv.extend(["--disable", feature])
        if self.name != "codex-default":
            argv.extend(["--model", self.name])
        return [*argv, "-"]

    def complete(self, operation: str, context: dict, schema: dict) -> dict:
        if self.calls >= self.max_calls:
            raise DoctorError("Model call budget exhausted")
        self.calls += 1
        prompt = SYSTEM + "\nDo not use tools or inspect files. Return the requested JSON using only the supplied context.\n" + json.dumps(
            {"operation": operation, "untrusted_data": redact(context)}, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="agentdoctor-codex-") as folder:
            workdir = Path(folder)
            schema_path, result_path = workdir / "schema.json", workdir / "result.json"
            write_json(schema_path, schema)
            argv = self.arguments(workdir, schema_path, result_path)
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            try:
                process = subprocess.Popen(argv, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, shell=False, start_new_session=os.name != "nt", creationflags=flags)
                try:
                    stdout, stderr = process.communicate(prompt.encode("utf-8"), timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    _kill_tree(process)
                    process.communicate(timeout=10)
                    raise DoctorError("Codex CLI request timed out") from exc
                except BaseException:
                    _kill_tree(process)
                    process.communicate(timeout=10)
                    raise
            except OSError as exc:
                raise DoctorError(f"Codex CLI cannot start: {type(exc).__name__}") from exc
            if len(stdout) > 2_000_000 or len(stderr) > 2_000_000:
                raise DoctorError("Codex CLI output exceeds size limit")
            completed = False
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                event = parse_json(line)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise DoctorError("Codex CLI emitted an invalid event")
                if event.get("type") == "turn.completed":
                    completed = True
                    self.usage.append(event.get("usage") or {})
                if event.get("type") == "turn.failed":
                    raise DoctorError("Codex CLI turn failed; check login, model access, or quota")
                if event.get("type", "").startswith("item."):
                    item = event.get("item")
                    item_type = item.get("type") if isinstance(item, dict) else None
                    if item_type not in ("agent_message", "reasoning"):
                        raise DoctorError("Codex CLI attempted a tool action; result excluded from context-only evaluation")
            if process.returncode or not completed or not result_path.is_file():
                raise DoctorError(f"Codex CLI returned no completed structured result (exit {process.returncode})")
            result = read_json(result_path)
            validate(result, schema, "Codex CLI output")
            return result
