from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .core import DoctorError, digest, redact
from .evidence import command_completed, command_passed, completion_error, parse_completion
from .project import check_manifest


def _environment() -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC", "LANG", "LC_ALL"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return env


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def execute(argv: list[str], cwd: Path, timeout: int, env: dict | None = None) -> dict:
    started = time.monotonic()
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def consume(stream, key):
        try:
            while data := stream.read(4096):
                room = 65536 - len(captured[key])
                captured[key].extend(data[:max(room, 0)])
                if len(data) > room:
                    truncated[key] = True
        finally:
            stream.close()

    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env or _environment(), shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != "nt", creationflags=flags)
    except OSError as exc:
        return {"returncode": None, "timed_out": False, "error": f"Cannot start validation command: {type(exc).__name__}", "stdout": "", "stderr": "", "duration_seconds": 0}
    threads = [threading.Thread(target=consume, args=(getattr(process, key), key), daemon=True) for key in captured]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        process.wait(timeout=10)
    except BaseException:
        _kill_tree(process)
        process.wait(timeout=10)
        raise
    for thread in threads:
        thread.join(timeout=2)
    result = {"returncode": process.returncode, "timed_out": timed_out, "error": None, "duration_seconds": round(time.monotonic() - started, 3)}
    for key in captured:
        result[key] = redact(bytes(captured[key]).decode("utf-8", errors="replace") + ("\n[output truncated]" if truncated[key] else ""))
        result[key + "_truncated"] = truncated[key]
    return result


class ValidationRunner:
    def __init__(self, backend: str = "docker"):
        if backend not in ("docker", "local"):
            raise DoctorError("Executor must be docker or local")
        self.backend = backend

    def available(self) -> tuple[bool, str]:
        if self.backend == "local":
            return True, "Trusted local execution; file copies are not an OS security sandbox"
        docker = shutil.which("docker")
        if docker is None:
            return False, "Docker is unavailable. Install Docker or explicitly select --executor local for trusted code."
        result = execute([docker, "info", "--format", "{{.ServerVersion}}"], Path.cwd(), 15)
        if result["returncode"] != 0:
            return False, "Docker daemon is unavailable"
        return True, "Docker: read-only project mount, no network, resource limits"

    def run_process(self, project: Path, argv: list[str], timeout: int, config: dict, *,
                    environment: dict[str, str] | None = None, mounts: list[tuple[Path, str]] = ()) -> dict:
        """Use the same executor and limits for validation and diagnostic replay."""
        env = _environment()
        env.update(environment or {})
        python = sys.executable if self.backend == "local" else "python"
        argv = [python if part == "{python}" else part for part in argv]
        name = None
        if self.backend == "docker":
            bindings = [(project, "/workspace"), *mounts]
            if any("," in str(source) or "," in target for source, target in bindings):
                raise DoctorError("Docker mount paths cannot contain commas")
            name = "agentdoctor-" + uuid.uuid4().hex
            options = [shutil.which("docker"), "run", "--rm", "--pull", "never", "--name", name,
                       "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--pids-limit", "64", "--memory", "512m", "--cpus", "1", "--user", "65534:65534",
                       "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--workdir", "/workspace",
                       "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONUTF8=1"]
            for key, value in (environment or {}).items():
                options.extend(["--env", key + "=" + value])
            for source, target in bindings:
                options.extend(["--mount", f"type=bind,source={source.resolve()},target={target},readonly"])
            argv = [*options, config["validation"]["docker_image"], *argv]
        try:
            result = execute(argv, project, timeout, env=env)
        finally:
            if name:
                execute([shutil.which("docker"), "rm", "-f", name], project, 10)
        if self.backend == "docker" and result["returncode"] in (125, 126, 127):
            result["error"] = "Docker could not run the test command; pre-load the image and its dependencies"
        return result

    def run_suite(self, project: Path, config: dict, manifest: dict[str, str]) -> dict:
        results = []
        integrity = check_manifest(project, manifest)
        if integrity:
            return {"commands": [], "integrity_changes": integrity, "executor": self.backend}
        for repetition in range(config["validation"]["repetitions"]):
            for command in config["validation"]["commands"]:
                invocation_id = uuid.uuid4().hex
                result = self.run_process(project, command["argv"], command["timeout_seconds"], config,
                                          environment={"AGENTDOCTOR_VALIDATION_ID": invocation_id})
                result.update(name=command["name"], kind=command["kind"], repetition=repetition + 1, argv=command["argv"],
                              expected_cases=list(command.get("expected_cases", [])), invocation_id=invocation_id)
                try:
                    result["completion"] = parse_completion(result["stdout"])
                    result["completion_error"] = completion_error(result)
                except DoctorError as exc:
                    result.update(completion=None, completion_error=str(exc))
                results.append(result)
                integrity = check_manifest(project, manifest)
                if integrity:
                    return {"commands": results, "integrity_changes": integrity, "executor": self.backend}
        return {"commands": results, "integrity_changes": integrity, "executor": self.backend}


def baseline_gate(suite: dict) -> tuple[bool, str]:
    rows = suite["commands"]
    if suite["integrity_changes"]:
        return False, "Validation mutated frozen snapshot files"
    if any(r["timed_out"] or r["error"] or r["returncode"] is None for r in rows):
        return False, "Baseline contains a timeout or infrastructure error"
    if any(not command_completed(r) for r in rows):
        return False, "Baseline lacks complete, consistent case results; configure expected_cases and completion evidence"
    targets = [r for r in rows if r["kind"] == "target"]
    regressions = [r for r in rows if r["kind"] == "regression"]
    if not targets or not regressions:
        return False, "At least one target and one regression command are required"
    if any(not command_passed(r) for r in regressions):
        return False, "Baseline regression checks must pass before repair"
    by_name = {}
    for row in targets:
        by_name.setdefault(row["name"], []).append(row["returncode"])
    if not any(all(code != 0 for code in codes) for codes in by_name.values()):
        return False, "No target failure reproduced consistently across baseline repetitions"
    if any(len({code == 0 for code in codes}) > 1 for codes in by_name.values()):
        return False, "Baseline target results are flaky; investigate before claiming a repair"
    return True, "Target failure reproduced; baseline regression checks pass"


def candidate_gate(baseline: dict, candidate: dict) -> tuple[bool, str]:
    ok, reason = baseline_gate(baseline)
    if not ok:
        return False, reason
    if candidate["integrity_changes"]:
        return False, "Candidate tests mutated snapshot files"
    expected = {(r["name"], r["repetition"], r["kind"]) for r in baseline["commands"]}
    actual = {(r["name"], r["repetition"], r["kind"]) for r in candidate["commands"]}
    if actual != expected or len(candidate["commands"]) != len(expected):
        return False, "Candidate did not run the same frozen checks as baseline"
    frozen = {(r["name"], r["repetition"], r["kind"]): set(r["expected_cases"]) for r in baseline["commands"]}
    if any(set(r.get("expected_cases", [])) != frozen[(r["name"], r["repetition"], r["kind"])] for r in candidate["commands"]):
        return False, "Candidate case expectations differ from the frozen baseline"
    if any(not command_completed(r) for r in candidate["commands"]):
        return False, "Candidate lacks complete, consistent results for every expected case"
    if any(not command_passed(r) for r in candidate["commands"]):
        return False, "Candidate failed one or more target/regression checks"
    return True, "Every expected case completed and passed in every configured repetition"
