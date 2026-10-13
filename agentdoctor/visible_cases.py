"""Explicit frozen visible-case bindings; never discover benchmark/oracle inputs."""
from __future__ import annotations

from pathlib import Path

from .contracts import STRING, obj, validate
from .core import DoctorError, contained_path, read_json


CASE = obj({"id": STRING, "function": STRING, "args": {"type": "array"},
            "kwargs": {"type": "object"}, "expected": {}}, ["function", "expected"])


def visible_cases(project: Path, command: dict) -> dict[str, dict]:
    """Bind only the configured command's expected IDs, without importing code.

    Explicit IDs may be in any order. Legacy ordered case lists bind to the
    frozen expected_cases order; mixed IDs and ambiguous counts are rejected.
    """
    spec = command.get("replay")
    if not spec:
        return {}
    if not all(part.isidentifier() for part in spec["module"].split(".")):
        raise DoctorError("Replay module must be a dotted Python module name")
    document = read_json(contained_path(project, spec["cases_file"]))
    if "case_group" in spec:
        if not isinstance(document, dict) or spec["case_group"] not in document:
            raise DoctorError("Replay case_group is missing from the frozen visible cases file")
        document = document[spec["case_group"]]
    expected = command.get("expected_cases", [])
    if not isinstance(document, list) or not expected or len(document) != len(expected):
        raise DoctorError("Replay cases must match the nonempty frozen expected_cases list")
    for case in document:
        validate(case, CASE, "visible replay case")
        if not all(part.isidentifier() for part in case["function"].split(".")):
            raise DoctorError("Replay function must be a dotted attribute name")
    explicit = sum("id" in case for case in document)
    if explicit not in (0, len(document)):
        raise DoctorError("Replay cases must either all have IDs or all follow expected_cases order")
    ids = [case["id"] for case in document] if explicit else expected
    if len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise DoctorError("Replay case IDs differ from frozen expected_cases")
    return {case_id: {"module": spec["module"], "function": case["function"],
                      "args": case.get("args", []), "kwargs": case.get("kwargs", {}), "expected": case["expected"]}
            for case_id, case in zip(ids, document)}
