"""Independent, stdlib-only held-out evaluator; copied AFTER repair finishes.

This deliberately does not import the project's evaluator.py or AgentDoctor's
diagnostic/patch code. Inputs and expected outcomes live outside repair projects.
"""
import importlib.util
import asyncio
import inspect
import json
import os
import sys
from pathlib import Path


def main():
    invocation = os.environ.get("AGENTDOCTOR_VALIDATION_ID", "standalone")
    kind = sys.argv[1]
    checks = json.loads(Path("_heldout.json").read_text(encoding="utf-8"))[kind]
    outcomes = []
    def emit(completed, error=None):
        print("AGENTDOCTOR_RESULT " + json.dumps({"schema_version": 1, "invocation_id": invocation,
              "completed": completed, "executed_count": len(outcomes), "results": outcomes, "suite_error": error}))
    try:
        spec = importlib.util.spec_from_file_location("subject", Path("subject.py"))
        subject = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(subject)
    except BaseException as exc:
        emit(False, f"Import failed: {type(exc).__name__}: {exc}")
        return 1
    for index, check in enumerate(checks, 1):
        try:
            actual = getattr(subject, check["function"])(**check["kwargs"])
            if inspect.isawaitable(actual):
                actual = asyncio.run(actual)
            passed = json.dumps(actual, sort_keys=True) == json.dumps(check["expected"], sort_keys=True)
            outcomes.append({"id": f"{kind}:{index}", "status": "passed" if passed else "failed",
                             "detail": json.dumps({"actual": actual, "expected": check["expected"]}, ensure_ascii=False)})
        except BaseException as exc:
            outcomes.append({"id": f"{kind}:{index}", "status": "error", "detail": f"{type(exc).__name__}: {exc}"})
    emit(True)
    return 0 if checks and all(item["status"] == "passed" for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
