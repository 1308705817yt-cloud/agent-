CASES = {'target': [{'function': 'run', 'kwargs': {'items': [2, 2, 1]}, 'expected': [{'item': 2, 'value': 4}, {'item': 2, 'value': 4}, {'item': 1, 'value': 2}]}, {'function': 'run', 'kwargs': {'items': []}, 'expected': []}], 'regression': [{'function': 'stable', 'kwargs': {'value': 4}, 'expected': 14}, {'function': 'stable', 'kwargs': {'value': -5}, 'expected': 5}]}

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
