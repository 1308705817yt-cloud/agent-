"""Completion protocol for frozen evaluators; process exit is only one signal.

The invocation ID prevents accidental reuse of an earlier report. This protocol
assumes a trusted evaluator; it is not attestation against hostile in-process code.
"""
from __future__ import annotations

from .contracts import STRING, obj, validate
from .core import DoctorError, parse_json

PREFIX = "AGENTDOCTOR_RESULT "
RESULT_SCHEMA = obj({
    "schema_version": {"const": 1}, "invocation_id": STRING,
    "completed": {"type": "boolean"},
    "executed_count": {"type": "integer", "minimum": 0},
    "results": {"type": "array", "items": obj({
        "id": STRING, "status": {"enum": ["passed", "failed", "error", "skipped"]},
        "detail": {"type": "string"},
        "input": {}, "actual": {}, "expected": {},
    }, ["id", "status"])},
    "suite_error": {"type": ["string", "null"]},
}, ["schema_version", "invocation_id", "completed", "executed_count", "results"])


def parse_completion(stdout: str) -> dict:
    lines = [line[len(PREFIX):] for line in stdout.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        raise DoctorError("Expected exactly one completion record on stdout")
    document = parse_json(lines[0])
    validate(document, RESULT_SCHEMA, "completion evidence")
    return document


def completion_error(row: dict) -> str | None:
    expected = row.get("expected_cases")
    if not isinstance(expected, list) or not expected or any(not isinstance(c, str) or not c for c in expected):
        return "Configure a nonempty expected_cases list for every validation command"
    if len(set(expected)) != len(expected):
        return "Expected case IDs must be unique"
    document = row.get("completion")
    try:
        validate(document, RESULT_SCHEMA, "completion evidence")
    except DoctorError as exc:
        return str(exc)
    if not row.get("invocation_id") or document["invocation_id"] != row["invocation_id"]:
        return "Completion record does not belong to this invocation"
    if row.get("stdout_truncated"):
        return "Validation stdout was truncated; completion evidence is incomplete"
    if not document["completed"] or document.get("suite_error"):
        return "Evaluator did not finish its suite: " + (document.get("suite_error") or "incomplete")
    results = document["results"]
    ids = [item["id"] for item in results]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        return "Executed case IDs differ from the frozen expected_cases list"
    if document["executed_count"] != len(results) or len(results) != len(expected):
        return "Executed case count differs from the frozen expectation"
    if any(item["status"] == "skipped" for item in results):
        return "Skipped cases cannot satisfy required validation coverage"
    passed = all(item["status"] == "passed" for item in results)
    if row.get("returncode") is None or (row["returncode"] == 0) != passed:
        return "Process exit code disagrees with individual case results"
    return None


def command_completed(row: dict) -> bool:
    return not row.get("timed_out", True) and not row.get("error") and completion_error(row) is None


def command_passed(row: dict) -> bool:
    return command_completed(row) and row["returncode"] == 0
