"""Frozen visible checks with explicit completion evidence."""
import json
import os
import sys

CASES = json.loads("{\"target\":[{\"function\":\"run\",\"kwargs\":{\"value\":2},\"expected\":{\"answer\":2}},{\"function\":\"run\",\"kwargs\":{\"value\":0},\"expected\":{\"answer\":0}}],\"regression\":[{\"function\":\"stable\",\"kwargs\":{\"value\":5},\"expected\":15},{\"function\":\"stable\",\"kwargs\":{\"value\":-2},\"expected\":8}]}")


def main():
    invocation = os.environ.get("AGENTDOCTOR_VALIDATION_ID", "standalone")
    kind = sys.argv[1]
    checks = CASES[kind]
    outcomes = []
    def emit(completed, error=None):
        print("AGENTDOCTOR_RESULT " + json.dumps({"schema_version": 1, "invocation_id": invocation,
              "completed": completed, "executed_count": len(outcomes), "results": outcomes, "suite_error": error}))
    try:
        import subject
    except BaseException as exc:
        emit(False, f"Import failed: {type(exc).__name__}: {exc}")
        return 1
    for index, check in enumerate(checks, 1):
        try:
            actual = getattr(subject, check["function"])(**check["kwargs"])
            passed = json.dumps(actual, sort_keys=True) == json.dumps(check["expected"], sort_keys=True)
            outcomes.append({"id": f"{kind}:{index}", "status": "passed" if passed else "failed",
                             "detail": json.dumps({"actual": actual, "expected": check["expected"]})})
        except BaseException as exc:
            outcomes.append({"id": f"{kind}:{index}", "status": "error", "detail": f"{type(exc).__name__}: {exc}"})
    emit(True)
    return 0 if outcomes and all(item["status"] == "passed" for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
