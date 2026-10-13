"""Read-only profiling of actual upstream executor calls plus public assertions."""
import ast
import importlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentdoctor.core import read_json
from agentdoctor.traces import TraceRecorder


def collect(project, output):
    sys.path.insert(0, str(project))
    subject = importlib.import_module("subject")
    config = read_json(project / "recording.json")
    recorder = TraceRecorder(output, "observed-public")
    captured, stack = [], []
    selected = {"evaluate_python_code", "evaluate_for", "evaluate_while"}
    source_path = (project / "smolagents/local_python_executor.py").resolve()

    def profile(frame, event, value):
        if frame.f_code.co_name not in selected or Path(frame.f_code.co_filename).resolve() != source_path:
            return
        if event == "call":
            code = frame.f_locals.get("code")
            node = frame.f_locals.get("for_loop", frame.f_locals.get("while_loop"))
            if node is not None:
                code = ast.unparse(node)
            span = {"span_id": uuid.uuid4().hex[:12], "node": frame.f_code.co_name, "kind": "node",
                    "source": {"path": "smolagents/local_python_executor.py", "function": frame.f_code.co_name, "line": frame.f_code.co_firstlineno},
                    "input": {"code": code}}
            if stack:
                span["parent_id"] = stack[-1][1]["span_id"]
            stack.append((frame, span))
        elif event == "return" and stack and stack[-1][0] is frame:
            _, span = stack.pop()
            span["output"] = value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)
            captured.append(span)

    observed = recorder.wrap("agent_code_steps", subject.run, source={"path": "subject.py", "function": "run"})
    sys.setprofile(profile)
    try:
        actual = observed(**config["kwargs"])
    finally:
        sys.setprofile(None)
    for span in captured:
        recorder.record(span)
    # Assertion is located at the observable public adapter, not prelabelled at a cause.
    recorder.record({"node": "public_behavior", "kind": "task", "source": {"path": "subject.py", "function": "run"},
                     "input": config["kwargs"], "output": actual,
                     "assertions": [{"name": config["contract"], "expected": config["expected"], "actual": actual,
                                     "passed": actual == config["expected"]}]})


if __name__ == "__main__":
    collect(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
