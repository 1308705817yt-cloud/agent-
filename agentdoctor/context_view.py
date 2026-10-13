"""Model-facing diagnostic view; reports keep full citation and audit records."""
from __future__ import annotations

import copy
import json

from .citations import checked_source
from .core import DoctorError, digest, redact
from .evidence import PREFIX, command_completed, parse_completion


def dependency_view(report: dict) -> dict:
    """Lossless grouping of static edges/limits, without changing discovery."""
    view = {k: copy.deepcopy(v) for k, v in report.items() if k not in ("resolved", "unresolved")}
    resolved, unresolved = {}, {}
    for edge in report["resolved"]:
        group = resolved.setdefault((edge["from_path"], edge["path"]), {})
        group.setdefault(edge["reference"], []).append([edge["start_line"], edge["end_line"]])
    for edge in report["unresolved"]:
        group = unresolved.setdefault((edge["path"], edge["reason"]), {})
        group.setdefault(edge["reference"], []).append(edge["line"])
    # Names are values, not object keys: redaction must not mistake a referenced
    # symbol such as api_key for a credential and erase its source ranges.
    view.update(format="grouped_references_v1",
                resolved=[{"from_path": origin, "path": target,
                           "references": [{"name": name, "ranges": ranges} for name, ranges in refs.items()]}
                          for (origin, target), refs in resolved.items()],
                unresolved=[{"path": path, "reason": reason,
                             "references": [{"name": name, "lines": lines} for name, lines in refs.items()]}
                            for (path, reason), refs in unresolved.items()],
                record_counts={key: len(report[key]) for key in ("resolved", "unresolved")})
    return view


def _log_excerpt(text: str, limit: int = 1024) -> dict:
    """Bound optional stdout, with an explicit gap and a hash of visible text."""
    result = {"chars": len(text), "sha256": digest(text), "omitted_chars": max(0, len(text) - limit)}
    if len(text) <= limit:
        result["text"] = text
    else:
        result.update(head=text[:limit // 2], tail=text[-(limit - limit // 2):])
    return result


def validation_view(suite: dict) -> dict:
    """Keep every case outcome/detail, compact only ancillary transport data.

    Never manufacture completion evidence: gates always consume the raw suite.
    Invalid/missing completion, timeout, stderr and case details stay verbatim.
    Only matched, valid stdout protocol records can be represented once through
    completion, allowing additional diagnostic stdout to be explicitly sampled.
    """
    view = redact(copy.deepcopy(suite))
    groups = {}
    for original in view.get("commands", []):
        row = copy.deepcopy(original)
        matched = False
        if command_completed(row):
            try:
                matched = parse_completion(row["stdout"]) == row["completion"]
            except DoctorError:
                pass
        if matched:
            extra = "".join(line for line in row["stdout"].splitlines(keepends=True) if not line.startswith(PREFIX))
            row["stdout"] = {"protocol_record": "completion", "additional_output": _log_excerpt(extra)}
            row["completion"].pop("invocation_id", None)
            row.pop("invocation_id", None)
            row.pop("duration_seconds", None)
        # Repetitions with different outcomes, error details or log hashes never
        # merge, even when the names and return codes happen to be identical.
        repetition = row.pop("repetition", None)
        key = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if key not in groups:
            groups[key] = {**row, "repetitions": []}
        groups[key]["repetitions"].append(repetition)
    view.update(format="validation_feedback_v1", command_count=len(view.get("commands", [])), commands=list(groups.values()))
    return view


def history_view(history: list[dict]) -> list[dict]:
    view = copy.deepcopy(history)
    for attempt in view:
        if isinstance(attempt.get("proposal"), dict) and attempt.get("summary") == attempt["proposal"].get("summary"):
            # The complete proposal already owns this exact summary.
            attempt.pop("summary", None)
        for key in ("baseline", "validation"):
            if isinstance(attempt.get(key), dict):
                attempt[key] = validation_view(attempt[key])
    return view


def diagnostic_view(pools: dict, manifest: dict) -> dict:
    """Resolve root excerpts into the *current* supplied source list.

    Trace evidence already uses IDs into spans/reference_spans. Source evidence
    similarly uses source_id and an absolute line range, so its text lives only
    in sources. Rebuild after every source trim: earlier request IDs may cover
    a different range. Audit-only data is retained in the original diagnosis.
    """
    context = copy.deepcopy(pools)
    # Stored citations refer to what the model actually saw, including redaction.
    # Compare against the same visible text; hashes still bind the raw revision.
    visible_sources = redact(context["sources"])
    for issue in context["findings"] + context["observations"]:
        for cause in issue.get("root_causes", []):
            excerpt = cause["source_evidence"]
            if excerpt["sha256"] != manifest.get(excerpt["path"]):
                raise DoctorError("Incomplete model context: root source evidence has a stale file hash")
            # Recheck the stored canonical range, never relocate repeated text.
            quote = {key: excerpt[key] for key in ("path", "start_line", "end_line", "text")}
            canonical, _ = checked_source(redact(quote), manifest, visible_sources)
            cause["source_evidence"] = {key: canonical[key] for key in ("source_id", "start_line", "end_line")}
            cause.pop("citation", None)
    return context
