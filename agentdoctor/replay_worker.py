"""Stdlib-only child worker for ONE explicitly supplied visible case.

Executed as a script in an isolated candidate copy, also inside Docker. It has
no benchmark/oracle discovery code. A replay is diagnostic, never acceptance.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import math
import sys
from pathlib import Path


PREFIX = "AGENTDOCTOR_REPLAY "


def preview(value, depth=0):
    """Bound incidental frame values; mark omissions and never call user repr."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str):
        return value if len(value) <= 512 else {"head": value[:512], "omitted_chars": len(value) - 512}
    if isinstance(value, ast.AST):
        try:
            return {"ast": preview(ast.unparse(value))}
        except (RecursionError, ValueError):
            return {"type": type(value).__name__}
    if depth < 2 and type(value) in (list, tuple, dict):
        entries = list(value.items()) if type(value) is dict else list(enumerate(value))
        visible = {str(k): preview(v, depth + 1) for k, v in entries[:8]}
        return {"items": visible, "omitted_items": max(0, len(entries) - 8)}
    return {"type": type(value).__name__, "value_omitted": True}


def main(request_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    root = Path.cwd().resolve()
    # Import the candidate and its dependencies, never the orchestration project.
    sys.path.insert(0, str(root))
    paths = {str((root / name).resolve()): name for name in request["trace_paths"]}
    filenames = {}
    selected = request.get("trace_functions")
    spans, active = [], {}
    calls, omitted = 0, 0
    run_id = request["invocation_id"]

    def profile(frame, event, value):
        nonlocal calls, omitted
        filename = frame.f_code.co_filename
        if filename not in filenames:
            filenames[filename] = paths.get(str(Path(filename).resolve())) if not filename.startswith("<") else None
        path = filenames[filename]
        if path is None or (selected and frame.f_code.co_name not in selected):
            return
        if event == "call":
            calls += 1
            if len(spans) + len(active) >= request["max_spans"]:
                omitted += 1
                return
            parent_frame = frame.f_back
            while parent_frame is not None and id(parent_frame) not in active:
                parent_frame = parent_frame.f_back
            count = frame.f_code.co_argcount + frame.f_code.co_kwonlyargcount
            span = {"run_id": run_id, "span_id": str(calls), "kind": "node", "node": frame.f_code.co_name,
                    "parent_id": active[id(parent_frame)]["span_id"] if parent_frame is not None else "case",
                    "source": {"path": path, "function": frame.f_code.co_name, "line": frame.f_code.co_firstlineno},
                    "input": {name: preview(frame.f_locals[name]) for name in frame.f_code.co_varnames[:count] if name in frame.f_locals},
                    "metadata": {"capture": "python_profile_segment", "source_sha256": request["trace_paths"][path]}}
            active[id(frame)] = span
        elif event == "return" and id(frame) in active:
            span = active.pop(id(frame))
            span["output"] = preview(value)
            spans.append(span)

    case = request["case"]
    actual, available, error = None, False, None
    sys.setprofile(profile)
    try:
        function = importlib.import_module(case["module"])
        for component in case["function"].split("."):
            function = getattr(function, component)
        actual = function(*case["args"], **case["kwargs"])
        if inspect.isawaitable(actual):
            actual = asyncio.run(actual)
        # Actual/expected outcomes are complete; only frame telemetry is sampled.
        encoded = json.dumps(actual, sort_keys=True, allow_nan=False)
        available = True
        passed = encoded == json.dumps(case["expected"], sort_keys=True, allow_nan=False)
    except BaseException as exc:
        actual, passed = None, False
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sys.setprofile(None)
    for span in active.values():
        span["error"] = "Profile segment did not return"
        spans.append(span)
    spans.append({"run_id": run_id, "span_id": "case", "kind": "task", "node": request["case_id"],
                  "input": {k: case[k] for k in ("module", "function", "args", "kwargs")},
                  "output": actual, "error": error,
                  "assertions": [{"name": "frozen visible expected result", "passed": passed,
                                  "actual": actual, "expected": case["expected"]}]})
    result = {"schema_version": 1, "invocation_id": run_id, "request_sha256": request["request_sha256"],
              "candidate_version": request["candidate_version"], "command": request["command"], "case_id": request["case_id"],
              "completed": True, "result": {"status": "error" if error else "passed" if passed else "failed",
                                            "actual_available": available, "actual": actual, "expected": case["expected"], "error": error},
              "trace": {"status": "partial" if omitted or active else "complete", "omitted_calls": omitted,
                        "unfinished_segments": len(active), "spans": spans}}
    print(PREFIX + json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
