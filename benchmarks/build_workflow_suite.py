"""Create workflow-v1 once, then freeze it before any real-model evaluation.

Run from the repository root. Refuses to overwrite a previously frozen dataset.
Reference edits and held-out assertions never enter the repair project.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

from agentdoctor.core import digest, write_json

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "agentdoctor/workflow_data"
PAD = "# Unrelated documentation padding keeps dependencies outside prefix samples.\n" * 240
STABLE = "\n\ndef stable(value):\n    return value + 10\n"


def check(kwargs, expected, function="run"):
    return {"function": function, "kwargs": kwargs, "expected": expected}


def evaluator(cases):
    return "CASES = " + repr(cases) + "\n" + textwrap.dedent('''
        import asyncio
        import inspect
        import json
        import os
        import sys

        def main():
            kind = sys.argv[1]
            results = []
            def emit(completed, error=None):
                print("AGENTDOCTOR_RESULT " + json.dumps({"schema_version": 1,
                    "invocation_id": os.environ.get("AGENTDOCTOR_VALIDATION_ID", "standalone"),
                    "completed": completed, "executed_count": len(results), "results": results, "suite_error": error}))
            try:
                import subject
            except BaseException as exc:
                emit(False, f"Import failed: {type(exc).__name__}: {exc}")
                return 1
            for index, case in enumerate(CASES[kind], 1):
                try:
                    actual = getattr(subject, case["function"])(**case["kwargs"])
                    if inspect.isawaitable(actual):
                        actual = asyncio.run(actual)
                    passed = json.dumps(actual, sort_keys=True) == json.dumps(case["expected"], sort_keys=True)
                    results.append({"id": f"{kind}:{index}", "status": "passed" if passed else "failed",
                                    "detail": json.dumps({"actual": actual, "expected": case["expected"]})})
                except BaseException as exc:
                    results.append({"id": f"{kind}:{index}", "status": "error", "detail": f"{type(exc).__name__}: {exc}"})
            emit(True)
            return 0 if results and all(r["status"] == "passed" for r in results) else 1

        if __name__ == "__main__":
            raise SystemExit(main())
    ''')


def build():
    if DATA.exists():
        raise SystemExit("workflow_data already exists; frozen cases must not be overwritten")
    suite = {"version": "workflow-v1", "design": "New synthetic multi-file and async workflows; same-project authored, not external production data.", "cases": []}
    references = {}
    regression = [check({"value": 4}, 14, "stable"), check({"value": -5}, 5, "stable")]
    held_regression = [check({"value": 31}, 41, "stable"), check({"value": -23}, -13, "stable")]

    def binding(module, function, kind="node", tool=None, source_function=None):
        item = {"module": module, "function": function, "kind": kind,
                "source": {"path": module.replace(".", "/") + ".py", "function": source_function or function}}
        if tool:
            item["tool"] = tool
        return item

    def add(case_id, family, files, recording, tools, visible, heldout, expected, edit=None, keys=None):
        project = DATA / "cases" / case_id
        project.mkdir(parents=True)
        files = {"workflow/__init__.py": "", **files}
        for path, text in files.items():
            target = project / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="")
        (project / "evaluator.py").write_text(evaluator({"target": visible, "regression": regression}), encoding="utf-8", newline="")
        write_json(project / "recording.json", recording)
        config = {"schema_version": 1, "project_name": case_id, "source_paths": ["subject.py", "workflow/**"], "tools": tools,
                  "checks": {"loop_threshold": 3, "preserve_state_keys": keys or []},
                  "validation": {"repetitions": 2, "commands": [
                      {"name": kind, "kind": kind, "argv": ["{python}", "-m", "evaluator", kind], "timeout_seconds": 15,
                       "expected_cases": [f"{kind}:{i}" for i in range(1, len(visible if kind == "target" else regression) + 1)]}
                      for kind in ("target", "regression")]},
                  "policy": {"max_candidates": 3, "max_model_calls": 5, "max_context_chars": 24000,
                             "max_changed_files": 3, "max_changed_lines": 120, "protected_paths": ["recording.json", "evaluator.py"]}}
        write_json(project / "doctor.json", config)
        suite["cases"].append({"id": case_id, "family": family, "expected": expected,
                               "heldout": {"target": heldout, "regression": held_regression}})
        if edit:
            references[case_id] = edit

    for faulty in (True, False):
        suffix = "fault" if faulty else "control"
        family = "dependency_contract"
        bad, good = 'return {"quantity": str(quantity)}', 'return {"quantity": quantity}'
        files = {
            "subject.py": "from workflow import dispatch\ndef run(quantity):\n    return dispatch.execute(quantity)\n" + STABLE,
            "workflow/dispatch.py": "from . import codec, service\n\ndef execute(quantity):\n    return service.quote(**codec.build_request(quantity))\n",
            "workflow/codec.py": PAD + 'def build_request(quantity):\n    """Adapt a nonnegative integer quantity for the quote tool."""\n    ' + (bad if faulty else good) + "\n",
            "workflow/service.py": "from .settings import RATE\n" + PAD + "def quote(quantity):\n    if type(quantity) is not int or quantity < 0:\n        raise TypeError('quantity must be a nonnegative integer')\n    return quantity * RATE\n",
            "workflow/settings.py": PAD + "RATE = 7\n"}
        record = {"kwargs": {"quantity": 3}, "bindings": [binding("workflow.codec", "build_request"), binding("workflow.service", "quote", "tool", "quote"), binding("workflow.dispatch", "execute")]}
        tools = {"quote": {"input_schema": {"type": "object", "properties": {"quantity": {"type": "integer", "minimum": 0}}, "required": ["quantity"], "additionalProperties": False}}}
        add(family + "_" + suffix, family, files, record, tools, [check({"quantity": q}, q * 7) for q in (0, 2)],
            [check({"quantity": q}, q * 7) for q in (7, 11, 19)], [{"category": "tool_argument_schema", "path": "workflow/codec.py"}] if faulty else [],
            {"path": "workflow/codec.py", "before": bad, "after": good} if faulty else None)

        family = "async_state_merge"
        bad, good = "return update", "return {**state, **update}"
        files = {
            "subject.py": "from workflow import graph\nasync def run(state):\n    return await graph.execute(state)\n" + STABLE,
            "workflow/graph.py": "from . import ledger\n" + PAD + 'async def execute(state):\n    """Merge the async balance update while retaining all existing request state."""\n    update = await ledger.hydrate(state["units"])\n    ' + (bad if faulty else good) + "\n",
            "workflow/ledger.py": "import asyncio\nfrom .settings import UNIT_VALUE\n\nasync def hydrate(units):\n    await asyncio.sleep(0)\n    return {'balance': units * UNIT_VALUE}\n",
            "workflow/settings.py": PAD + "UNIT_VALUE = 9\n"}
        def state_case(units, request, **extra):
            state = {"request_id": request, "units": units, **extra}
            return check({"state": state}, {**state, "balance": units * 9})
        record = {"kwargs": {"state": {"request_id": "recorded", "units": 3, "messages": ["keep"]}}, "capture_state": True,
                  "state_source": {"path": "workflow/graph.py", "function": "execute"},
                  "bindings": [binding("workflow.graph", "execute"), binding("workflow.ledger", "hydrate")]}
        add(family + "_" + suffix, family, files, record, {}, [state_case(2, "visible"), state_case(0, "zero")],
            [state_case(7, "held", messages=["a", "b"]), state_case(12, "second", extra=True)],
            [{"category": "state_invariant", "path": "workflow/graph.py"}] if faulty else [],
            {"path": "workflow/graph.py", "before": bad, "after": good} if faulty else None, keys=["request_id"])

        family = "async_fanout"
        bad, good = 'return list({row["item"]: row for row in rows}.values())', "return rows"
        files = {
            "subject.py": "from workflow import graph\nasync def run(items):\n    return await graph.execute(items)\n" + STABLE,
            "workflow/graph.py": "import asyncio\nfrom . import worker\n" + PAD + 'async def execute(items):\n    """Return one row per requested item, retaining duplicates and original input order."""\n    rows = await asyncio.gather(*(worker.fetch(item) for item in items))\n    ' + (bad if faulty else good) + "\n",
            "workflow/worker.py": "import asyncio\nfrom .settings import MULTIPLIER\n\nasync def fetch(item):\n    await asyncio.sleep((item % 3) * 0.001)\n    return {'item': item, 'value': item * MULTIPLIER}\n",
            "workflow/settings.py": PAD + "MULTIPLIER = 2\n"}
        def fanout(items):
            return check({"items": items}, [{"item": i, "value": i * 2} for i in items])
        record = {"kwargs": {"items": [3, 3, 1]}, "output_count_from": "items", "state_source": {"path": "workflow/graph.py", "function": "execute"},
                  "bindings": [binding("workflow.graph", "execute"), binding("workflow.worker", "fetch", "tool", "fetch")]}
        tools = {"fetch": {"input_schema": {"type": "object", "properties": {"item": {"type": "integer"}}, "required": ["item"]}}}
        add(family + "_" + suffix, family, files, record, tools, [fanout([2, 2, 1]), fanout([])],
            [fanout([7, 1, 7, 2, 1]), fanout([-3, -3, 0]), fanout([11])],
            [{"category": "other", "path": "workflow/graph.py"}] if faulty else [],
            {"path": "workflow/graph.py", "before": bad, "after": good} if faulty else None)

        family = "nested_retry"
        bad = "        status = await step(ready_after)\n"
        good = bad + '        if status["done"]:\n            break\n'
        files = {
            "subject.py": "from workflow import graph\nasync def run(ready_after):\n    return await graph.execute(ready_after)\n" + STABLE,
            "workflow/graph.py": "from . import service\nfrom .settings import MAX_ATTEMPTS\n" + PAD + 'async def step(ready_after):\n    return await service.status(ready_after)\n\nasync def execute(ready_after):\n    """Stop querying as soon as done; pending tasks exhaust at most MAX_ATTEMPTS."""\n    service.reset()\n    for attempt in range(MAX_ATTEMPTS):\n' + (bad if faulty else good) + '    return {"done": status["done"], "calls": service.calls}\n',
            "workflow/service.py": "import asyncio\n\ncalls = 0\n\ndef reset():\n    global calls\n    calls = 0\n\nasync def status(ready_after):\n    global calls\n    await asyncio.sleep(0)\n    calls += 1\n    return {'done': calls >= ready_after}\n",
            "workflow/settings.py": PAD + "MAX_ATTEMPTS = 5\n"}
        def retry(ready):
            return check({"ready_after": ready}, {"done": ready <= 5, "calls": min(max(1, ready), 5)})
        record = {"kwargs": {"ready_after": 1 if faulty else 99},
                  "bindings": [binding("workflow.graph", "execute"), binding("workflow.graph", "step"),
                               {**binding("workflow.service", "status", "tool", "status"), "source": {"path": "workflow/graph.py", "function": "step"}}]}
        tools = {"status": {"input_schema": {"type": "object", "properties": {"ready_after": {"type": "integer"}}, "required": ["ready_after"]}}}
        add(family + "_" + suffix, family, files, record, tools, [retry(1), retry(99)], [retry(2), retry(4), retry(5), retry(20)],
            [{"category": "possible_loop", "path": "workflow/graph.py"}] if faulty else [],
            {"path": "workflow/graph.py", "before": bad, "after": good} if faulty else None)

    write_json(DATA / "suite.json", suite)
    write_json(ROOT / "benchmarks/workflow-v1.reference-patches.json", references)
    files = {p.relative_to(DATA).as_posix(): digest(p.read_bytes()) for p in sorted(DATA.rglob("*")) if p.is_file()}
    write_json(ROOT / "benchmarks/workflow-v1.lock.json", {"suite_version": suite["version"], "dataset_hash": digest(json.dumps(files, sort_keys=True)),
               "case_ids": [c["id"] for c in suite["cases"]], "files": files})
    print(f"Frozen {len(suite['cases'])} cases, {len(files)} files; dataset hash {digest(json.dumps(files, sort_keys=True))}")


if __name__ == "__main__":
    build()
