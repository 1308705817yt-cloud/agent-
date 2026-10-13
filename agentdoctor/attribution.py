"""Evidence validation for causal hypotheses; validation is not causal proof."""
from __future__ import annotations

import copy
from .citations import checked_source
from .core import DoctorError


def checked_cause(cause: dict, observed_evidence: list[str], current: dict,
                  visible: set[str], manifest: dict, sources: list[dict]) -> dict:
    refs = set(cause["evidence"])
    if not refs or not refs <= set(current) & visible or not refs & set(observed_evidence):
        raise DoctorError("Root cause must cite supplied current evidence including the attributed observation")
    observed_runs = {current[ref]["run_id"] for ref in observed_evidence if ref in current}
    if any(current[ref]["run_id"] not in observed_runs for ref in refs):
        raise DoctorError("Root cause cites an unrelated run")
    source, citation = checked_source(cause["source"], manifest, sources)
    return {"status": "hypothesized", "origin": "model", "summary": cause["summary"],
            "evidence": list(dict.fromkeys(cause["evidence"])),
            "source": {"path": source["path"], "line": source["start_line"]},
            "source_evidence": source, "citation": citation,
            "evidence_validation": "trace_and_source_checked; causality_unconfirmed"}


def combine_causes(first: list[dict], second: list[dict]) -> list[dict]:
    result = copy.deepcopy(first)
    for cause in second:
        if cause not in result:
            result.append(copy.deepcopy(cause))
    return result
