"""Freeze untouched upstream code with separately authored acceptance cases."""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from agentdoctor.core import digest, write_json
from agentdoctor.project import _project_files, snapshot

ROOT = Path(__file__).resolve().parents[1]
SHA = "30bb1161095dbae2271e6bc3cc4c219cc3897a57"
UPSTREAM = ROOT / "artifacts/public-source" / ("smolagents-" + SHA)
DATA = ROOT / "benchmarks/public-smolagents-v1"

SUBJECT = '''
from smolagents import local_python_executor as executor


def run(codes):
    """Execute known small agent code steps in one session.

    Each step returns its own print output, result and error flag. Earlier user
    variables remain available. Valid Python loops must retain Python semantics.
    """
    state = {}
    rows = []
    for code in codes:
        value, failed = None, False
        try:
            value, _ = executor.evaluate_python_code(
                code, static_tools=dict(executor.BASE_PYTHON_TOOLS), state=state, timeout_seconds=None
            )
        except executor.InterpreterError:
            failed = True
        rows.append({"value": value, "logs": str(state.get("_print_outputs", "")), "failed": failed})
    return rows
'''


def step(value=None, logs="", failed=False):
    return {"value": value, "logs": logs, "failed": failed}


def test(codes, outcomes):
    return {"function": "run", "kwargs": {"codes": codes}, "expected": outcomes}


def build():
    if DATA.exists():
        raise SystemExit("Refusing to replace frozen public cases")
    suite = {"version": "public-smolagents-v1", "provenance": {"repository": "https://github.com/huggingface/smolagents",
             "commit": SHA, "upstream_zip_sha256": digest((UPSTREAM / "upstream.zip").read_bytes()),
             "scope": "Unmodified upstream package, local Python executor used by CodeAgent; no live target LLM. Public issue suggestions and hidden tests excluded from repair context."}, "cases": []}
    loop_for = "x = 0\nfor i in range(3):\n    x += i\nelse:\n    x += 100\nx"
    loop_while = "x = 0\nwhile x < 3:\n    x += 1\nelse:\n    x += 100\nx"
    break_for = "x = 0\nfor i in range(4):\n    if i == 2:\n        break\n    x += i\nelse:\n    x += 100\nx"
    break_while = "x = 0\nwhile x < 4:\n    x += 1\n    if x == 2:\n        break\nelse:\n    x += 100\nx"
    loops_target = [test([loop_for], [step(103)]), test([loop_while], [step(103)])]
    loops_regression = [test([break_for], [step(1)]), test([break_while], [step(2)])]
    loops_hidden = [test(["x = 7\nfor i in []:\n    x = 0\nelse:\n    x += 11\nx"], [step(18)]),
                    test(["x = 8\nwhile False:\n    x = 0\nelse:\n    x += 13\nx"], [step(21)]),
                    test(["x = 0\nfor i in range(5):\n    if i == 2:\n        continue\n    x += i\nelse:\n    x += 20\nx"], [step(28)]),
                    test(["x = 0\nwhile x < 3:\n    x += 1\n    continue\nelse:\n    x += 40\nx"], [step(43)])]
    logs_target = [test(["print('first step')", "if True print('invalid')"], [step(logs="first step\n"), step(failed=True)]),
                   test(["value = 4\nprint(value)", "def broken(", "value + 1"], [step(logs="4\n"), step(failed=True), step(5)])]
    logs_regression = [test(["print('one')", "print('two')"], [step(logs="one\n"), step(logs="two\n")]),
                       test(["2 + 3"], [step(5)])]
    logs_hidden = [test(["print('heldout-old')", "for x in :\n    pass", "print('heldout-new')"],
                        [step(logs="heldout-old\n"), step(failed=True), step(logs="heldout-new\n")]),
                   test(["n = 17\nprint(n)", "x = (", "n * 2"], [step(logs="17\n"), step(failed=True), step(34)]),
                   test(["x = (", "y = ["], [step(failed=True), step(failed=True)])]
    extra_regression = [test(["x = 9", "x + 2"], [step(9), step(11)])]
    configurations = [
        ("loop_else_fault", 2782, True, loops_target, loops_regression, loops_hidden, loops_regression + extra_regression),
        ("print_reset_fault", 1998, True, logs_target, logs_regression, logs_hidden, logs_regression + extra_regression),
        ("loop_control", 2782, False, loops_regression, extra_regression, loops_regression, extra_regression),
        ("print_control", 1998, False, logs_regression, extra_regression, logs_regression, extra_regression),
    ]
    oracle = (ROOT / "agentdoctor/benchmark_oracle.py").read_text(encoding="utf-8")
    for name, issue, faulty, visible, regression, hidden, hidden_regression in configurations:
        project = DATA / "cases" / name
        project.mkdir(parents=True)
        upstream_manifest = snapshot(UPSTREAM / "src/smolagents", project / "smolagents")
        (project / "LICENSE.smolagents").write_bytes((UPSTREAM / "LICENSE").read_bytes())
        (project / "subject.py").write_text(dedent(SUBJECT).lstrip("\n"), encoding="utf-8", newline="")
        (project / "evaluator.py").write_text(oracle.replace('Path("_heldout.json")', 'Path("visible.json")'), encoding="utf-8", newline="")
        write_json(project / "visible.json", {"target": visible, "regression": regression})
        write_json(project / "recording.json", {"kwargs": visible[0]["kwargs"], "expected": visible[0]["expected"],
                  "contract": "Each code step has its own output; valid Python loop semantics and persistent user variables must be preserved."})
        config = {"schema_version": 1, "project_name": name, "source_paths": ["smolagents/local_python_executor.py"], "tools": {},
                  "checks": {"loop_threshold": 3, "preserve_state_keys": []},
                  "validation": {"repetitions": 2, "commands": [
                      {"name": kind, "kind": kind, "argv": ["{python}", "-m", "evaluator", kind], "timeout_seconds": 30,
                       "expected_cases": [f"{kind}:{i}" for i in range(1, len(rows) + 1)]}
                      for kind, rows in (("target", visible), ("regression", regression))]},
                  "policy": {"max_candidates": 3, "max_model_calls": 5, "max_context_chars": 96000,
                             "max_changed_files": 3, "max_changed_lines": 120,
                             "protected_paths": ["subject.py", "evaluator.py", "visible.json", "recording.json", "LICENSE.smolagents"]}}
        write_json(project / "doctor.json", config)
        suite["cases"].append({"id": name, "family": "public_smolagents", "issue": f"https://github.com/huggingface/smolagents/issues/{issue}",
                               "expected": [{"category": "other", "path": "smolagents/local_python_executor.py"}] if faulty else [],
                               "upstream_manifest": upstream_manifest, "heldout": {"target": hidden, "regression": hidden_regression}})
    write_json(DATA / "suite.json", suite)
    files = {name: digest(path.read_bytes()) for name, path in _project_files(DATA)}
    write_json(ROOT / "benchmarks/public-smolagents-v1.lock.json", {"files": files, "dataset_hash": digest(json.dumps(files, sort_keys=True))})
    print(json.dumps({"cases": len(suite["cases"]), "files": len(files), "dataset_hash": digest(json.dumps(files, sort_keys=True))}))


if __name__ == "__main__":
    build()
