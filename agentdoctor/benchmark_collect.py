"""Collect real observations from a trusted bundled fixture in a child process.

No expected labels, reference patches, or held-out tests enter this process.
"""
from __future__ import annotations

import copy
import asyncio
import importlib
import importlib.util
import inspect
import sys
from pathlib import Path

from .core import read_json
from .traces import TraceRecorder


def collect(project: Path, output: Path) -> None:
    # Trusted fixture projects run in separate processes in the benchmark.
    previous_path = sys.path[:]
    sys.path.insert(0, str(project))
    try:
        _collect(project, output)
    finally:
        sys.path[:] = previous_path


def _collect(project: Path, output: Path) -> None:
    recording = read_json(project / "recording.json")
    spec = importlib.util.spec_from_file_location("subject", project / "subject.py")
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    recorder = TraceRecorder(output, "observed-run")
    source = {"path": "subject.py", "function": "run"}
    for tool in recording.get("tools", []):
        setattr(subject, tool, recorder.wrap("run", getattr(subject, tool), kind="tool", tool=tool, source=source))
    for binding in recording.get("bindings", []):
        module = importlib.import_module(binding["module"])
        function = binding["function"]
        setattr(module, function, recorder.wrap(binding.get("node", function), getattr(module, function),
                                               kind=binding.get("kind", "node"), tool=binding.get("tool"), source=binding["source"]))
    observed = recorder.wrap("run", subject.run, source=source)
    kwargs = copy.deepcopy(recording["kwargs"])
    before = copy.deepcopy(kwargs.get("state"))
    result, error = None, None
    try:
        result = observed(**kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if recording.get("capture_state") and isinstance(before, dict) and isinstance(result, dict):
        recorder.record({"node": "merge", "kind": "node", "source": recording.get("state_source", source),
                         "state_before": before, "state_after": result, "error": error})
    if recording.get("output_count_from") and isinstance(result, list):
        count = len(kwargs[recording["output_count_from"]])
        recorder.record({"node": "completion", "kind": "task", "source": recording.get("state_source", source),
                         "input": kwargs, "output": result,
                         "assertions": [{"name": "one result per requested item", "passed": len(result) == count,
                                         "expected": count, "actual": len(result)}]})
    if recording.get("prompt_file"):
        recorder.record({"node": "response", "kind": "llm", "input": kwargs, "output": result,
                         "prompt": (project / recording["prompt_file"]).read_text(encoding="utf-8"),
                         "source": {"path": recording["prompt_file"]}, "error": error,
                         "output_schema": recording["output_schema"]})


if __name__ == "__main__":
    collect(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
