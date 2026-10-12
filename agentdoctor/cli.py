from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from . import __version__
from .core import DoctorError, read_json, write_json
from .engine import run
from .model import ModelClient
from .project import snapshot
from .runner import execute


def _progress(stage: str):
    print(f"[AgentDoctor] {stage}", file=sys.stderr, flush=True)


def _summary(report: dict):
    print(json.dumps({"status": report["status"], "reason": report["reason"],
                      "findings": len(report["diagnosis"].get("findings", [])),
                      "attempts": len(report["attempts"]), "patch": report.get("verified_patch"),
                      "report": str(Path(report["workspace"]) / "report.html"),
                      "json": str(Path(report["workspace"]) / "report.json")}, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentdoctor", description="Detect, diagnose, and verify bounded repairs for Python agents")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run the bundled trusted broken-agent example without a model/API key")
    demo.add_argument("--output", type=Path, default=Path("artifacts/demo"))
    initialize = sub.add_parser("init", help="Write an integration config template; does not execute the project")
    initialize.add_argument("project", type=Path)
    inspect_parser = sub.add_parser("show", help="Inspect a saved report or run directory")
    inspect_parser.add_argument("report", type=Path)
    for name in ("diagnose", "repair"):
        command = sub.add_parser(name)
        command.add_argument("project", type=Path)
        command.add_argument("--traces", type=Path, required=True)
        command.add_argument("--reference", type=Path, help="Optional successful traces with matching metadata.task_id")
        command.add_argument("--config", default="doctor.json", help="Project-relative config path")
        command.add_argument("--output", type=Path)
        command.add_argument("--provider", choices=["none", "openai", "ollama"], default="none")
        command.add_argument("--model", default=None)
        command.add_argument("--base-url", default=None)
        if name == "repair":
            command.add_argument("--executor", choices=["docker", "local"], default="docker", help="local executes trusted code with your account; it is not a security sandbox")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            project = arguments.project.resolve()
            project.mkdir(parents=True, exist_ok=True)
            path = project / "doctor.json"
            if path.exists():
                raise DoctorError("doctor.json already exists; refusing to overwrite it")
            write_json(path, {"schema_version": 1, "project_name": project.name, "source_paths": ["src/**", "prompts/**"],
                              "tools": {}, "checks": {"loop_threshold": 3, "preserve_state_keys": []},
                              "validation": {"commands": [], "repetitions": 2, "docker_image": "python:3.11-slim"},
                              "policy": {"protected_paths": ["tests/**", "doctor.json"], "max_candidates": 3, "max_changed_files": 3, "max_changed_lines": 120, "max_model_calls": 8, "max_context_chars": 48000}})
            print(f"Created {path}. Configure source_paths, tool contracts, and frozen target/regression checks.")
            return 0
        if arguments.command == "show":
            path = arguments.report / "report.json" if arguments.report.is_dir() else arguments.report
            _summary(read_json(path, limit=20_000_000))
            return 0
        if arguments.command == "demo":
            output = arguments.output.resolve()
            output.mkdir(parents=True, exist_ok=True)
            project = output / ("demo-project-" + uuid.uuid4().hex[:8])
            snapshot(Path(__file__).parent / "demo_project", project)
            _progress("record_actual_demo_failure")
            recorded = execute([sys.executable, "record_trace.py"], project, 15)
            if recorded["returncode"] != 0:
                raise DoctorError("Demo trace recording failed: " + recorded["stderr"])
            report = run(project, project / "trace.jsonl", mode="repair", output=output / "runs", executor="local", progress=_progress)
            write_json(output / "last-run.json", {"project": str(project), "workspace": report["workspace"], "status": report["status"]})
        else:
            model = None
            if arguments.provider != "none":
                name = arguments.model or os.environ.get("OPENAI_MODEL" if arguments.provider == "openai" else "OLLAMA_MODEL", "")
                model = ModelClient(arguments.provider, name, arguments.base_url)
            report = run(arguments.project, arguments.traces, mode=arguments.command, config_file=arguments.config,
                         output=arguments.output, executor=getattr(arguments, "executor", "docker"), model=model,
                         reference=arguments.reference, progress=_progress)
        _summary(report)
        return 0 if report["status"] in ("diagnosed", "verified") else 2
    except (DoctorError, OSError, UnicodeError) as exc:
        print(f"AgentDoctor: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Saved state.json files remain available for inspection.", file=sys.stderr)
        return 130
