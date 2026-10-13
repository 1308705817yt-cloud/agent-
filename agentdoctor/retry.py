"""Versioned visible failure evidence and explicit original-snapshot patch bases."""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from .contracts import STRING, TRACE_SCHEMA, obj, validate
from .core import DoctorError, digest, parse_json, read_json, redact, write_json
from .evidence import command_completed
from .project import check_manifest, editable, snapshot
from .traces import evidence_id, load_traces
from .visible_cases import visible_cases


REPLAY_PREFIX = "AGENTDOCTOR_REPLAY "
REPLAY_SCHEMA = obj({
    "schema_version": {"const": 1}, "invocation_id": STRING, "request_sha256": STRING,
    "candidate_version": STRING, "command": STRING, "case_id": STRING, "completed": {"const": True},
    "result": obj({"status": {"enum": ["passed", "failed", "error"]}, "actual_available": {"type": "boolean"},
                   "actual": {}, "expected": {}, "error": {"type": ["string", "null"]}}),
    "trace": obj({"status": {"enum": ["complete", "partial"]}, "omitted_calls": {"type": "integer", "minimum": 0},
                  "unfinished_segments": {"type": "integer", "minimum": 0}, "spans": {"type": "array", "items": TRACE_SCHEMA, "minItems": 1}}),
})


def snapshot_version(manifest: dict) -> str:
    return digest(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def patch_base(manifest: dict, attempts: list[dict]) -> dict:
    preserved = {}
    for attempt in attempts:
        for case in attempt.get("retry_feedback", {}).get("newly_passing_cases", []):
            preserved[(case["command"], case["case_id"])] = case
    return {"kind": "original_snapshot", "snapshot_id": snapshot_version(manifest),
            "application": "complete_replacement_not_incremental", "sources_revision": "original_snapshot",
            "candidate_traces_revision": "explicit_candidate_version_not_original_source_line_numbers",
            "preserve_observed_improvements": list(preserved.values())}


def _completed_cases(suite: dict) -> tuple[dict, list[str]]:
    cases, unavailable = {}, []
    for row in suite["commands"]:
        if not command_completed(row):
            unavailable.append(row["name"] + ":" + str(row["repetition"]))
            continue
        for result in row["completion"]["results"]:
            cases.setdefault((row["name"], result["id"]), []).append((row["repetition"], result))
    return cases, unavailable


def _observed_result(result: dict) -> dict:
    # Legacy evaluators serialize actual/expected in detail. Only parse explicit
    # JSON fields; do not infer inputs or expected results from prose/exceptions.
    detail = {}
    try:
        parsed = parse_json(result.get("detail", ""))
        if isinstance(parsed, dict):
            detail = parsed
    except DoctorError:
        pass
    fields = {**detail, **result}
    return {"status": result["status"], **{key: fields[key] for key in ("input", "actual", "expected") if key in fields},
            "actual_available": "actual" in fields, "expected_available": "expected" in fields}


def _replay(candidate: Path, directory: Path, manifest: dict, command: dict, case_id: str,
            case: dict, config: dict, runner) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    worker = Path(__file__).with_name("replay_worker.py")
    request = {"invocation_id": uuid.uuid4().hex, "candidate_version": snapshot_version(manifest),
               "command": command["name"], "case_id": case_id, "case": case,
               "trace_paths": {name: sha for name, sha in manifest.items() if editable(name, config) and name.endswith(".py")},
               "trace_functions": command["replay"].get("trace_functions"), "max_spans": command["replay"].get("max_spans", 64)}
    request["request_sha256"] = digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
    write_json(directory / "request.json", request)
    write_json(directory / "candidate-manifest.json", manifest)
    copied = snapshot(candidate, directory / "project")
    if copied != manifest:
        raise DoctorError("Candidate changed before replay snapshot")
    local = runner.backend == "local"
    argv = ["{python}", str(worker) if local else "/agentdoctor-replay.py",
            str(directory / "request.json") if local else "/agentdoctor-replay-request.json"]
    try:
        execution = runner.run_process(directory / "project", argv, command["timeout_seconds"], config,
                                       mounts=[(worker, "/agentdoctor-replay.py"), (directory / "request.json", "/agentdoctor-replay-request.json")])
    finally:
        # Execution needs original values; persisted request/audit views redact
        # them. The original request digest is retained for invocation binding.
        write_json(directory / "request.json", redact(request))
    changes = check_manifest(directory / "project", manifest)
    raw = {"execution": execution, "integrity_changes": changes, "request_sha256": request["request_sha256"],
           "worker_sha256": digest(worker.read_bytes()), "candidate_version": request["candidate_version"]}
    result = {"status": "incomplete", "reason": "No validated replay evidence"}
    try:
        if changes or execution["error"] or execution["timed_out"] or execution.get("stdout_truncated"):
            raise DoctorError("Replay incomplete: timeout, infrastructure, truncated output or integrity change")
        lines = [line[len(REPLAY_PREFIX):] for line in execution["stdout"].splitlines() if line.startswith(REPLAY_PREFIX)]
        if len(lines) != 1:
            raise DoctorError("Replay requires exactly one completion record")
        record = parse_json(lines[0])
        validate(record, REPLAY_SCHEMA, "visible replay")
        record = redact(record)
        for key in ("invocation_id", "request_sha256", "candidate_version", "command", "case_id"):
            if record[key] != request[key]:
                raise DoctorError("Replay completion does not match its frozen request: " + key)
        if record["result"]["expected"] != redact(case["expected"]):
            raise DoctorError("Replay expected result differs from the frozen visible case")
        if execution["returncode"] != (0 if record["result"]["status"] == "passed" else 1):
            raise DoctorError("Replay process exit disagrees with its result")
        trace_file = directory / "traces.json"
        write_json(trace_file, record["trace"]["spans"])
        spans = load_traces(trace_file)
        ids = {evidence_id(s) for s in spans}
        if any(s["run_id"] != request["invocation_id"] or
               (s.get("parent_id") and f"{s['run_id']}/{s['parent_id']}" not in ids) for s in spans):
            raise DoctorError("Replay traces have inconsistent run or parent IDs")
        task = next((s for s in spans if s["span_id"] == "case"), None)
        if task is None or task.get("node") != case_id or task.get("input") != redact({k: case[k] for k in ("module", "function", "args", "kwargs")}):
            raise DoctorError("Replay traces are not linked to the requested case input")
        outcome = record["result"]
        expected_assertion = {"name": "frozen visible expected result", "passed": outcome["status"] == "passed",
                              "actual": outcome["actual"], "expected": outcome["expected"]}
        if task["kind"] != "task" or task.get("output") != outcome["actual"] or task.get("error") != outcome["error"] or task.get("assertions") != [expected_assertion]:
            raise DoctorError("Replay case trace disagrees with its result")
        if len(spans) > request["max_spans"] + 1:
            raise DoctorError("Replay trace exceeds its declared capture limit")
        for span in spans:
            source = span.get("source")
            if source and (source["path"] not in request["trace_paths"] or
                           span.get("metadata", {}).get("source_sha256") != request["trace_paths"][source["path"]]):
                raise DoctorError("Replay trace source does not match candidate revision")
        raw["record"] = record
        result = {"status": "completed", "result": record["result"], "trace": record["trace"],
                  "case_evidence_id": evidence_id(task)}
    except (DoctorError, OSError, UnicodeError) as exc:
        result["reason"] = str(exc)
    audit_path = directory / "audit.json"
    safe_lines = []
    for line in execution["stdout"].splitlines(keepends=True):
        if line.startswith(REPLAY_PREFIX):
            try:
                line = REPLAY_PREFIX + json.dumps(redact(parse_json(line[len(REPLAY_PREFIX):])), ensure_ascii=False) + "\n"
            except DoctorError:
                line = REPLAY_PREFIX + "[incomplete record; original output bound by hash]\n"
        safe_lines.append(line)
    raw["execution"] = {**execution, "stdout": "".join(safe_lines), "stdout_sha256_before_structural_redaction": digest(execution["stdout"])}
    write_json(audit_path, redact(raw))
    result.update(audit_file=str(audit_path), audit_sha256=digest(audit_path.read_bytes()))
    return redact(result)


def failure_feedback(original: Path, candidate: Path, output: Path, base_manifest: dict, candidate_manifest: dict,
                     attempt: dict, baseline: dict, config: dict, runner) -> dict:
    """Associate per-case visible failures with fresh candidate traces, once/case.

    Baseline/candidate suites remain unchanged and are the only acceptance input.
    Replay data always names its candidate revision and never enters diagnosis
    traces, which still refer to the original snapshot.
    """
    version = snapshot_version(candidate_manifest)
    feedback = {"candidate": {"number": attempt["number"], "version": version,
                              "base_snapshot_id": snapshot_version(base_manifest),
                              "changed_files": [{"path": name, "sha256": sha} for name, sha in candidate_manifest.items() if sha != base_manifest.get(name)]},
                "scope": "visible_only_diagnostic_not_acceptance", "newly_passing_cases": [], "failed_cases": []}
    cases, unavailable = _completed_cases(attempt["validation"])
    baseline_cases, _ = _completed_cases(baseline)
    feedback["unavailable_commands"] = unavailable
    commands = {c["name"]: c for c in config["validation"]["commands"]}
    integrity = sorted(set(attempt["validation"]["integrity_changes"] + check_manifest(candidate, candidate_manifest)
                           + check_manifest(original, base_manifest)))
    feedback["integrity_changes"] = integrity
    for (name, case_id), observations in cases.items():
        ref = {"command": name, "case_id": case_id}
        failed = [(rep, result) for rep, result in observations if result["status"] in ("failed", "error")]
        if not failed:
            if not integrity and len(observations) == config["validation"]["repetitions"] and any(
                    result["status"] != "passed" for _, result in baseline_cases.get((name, case_id), [])):
                feedback["newly_passing_cases"].append({**ref, "candidate_version": version})
            continue
        groups = {}
        for repetition, result in failed:
            observed = _observed_result(result)
            key = json.dumps(observed, sort_keys=True, ensure_ascii=False)
            groups.setdefault(key, {**observed, "repetitions": []})["repetitions"].append(repetition)
        entry = {**ref, "validation_results": list(groups.values()), "input_available": False,
                 "replay": {"status": "unavailable", "reason": "No frozen visible replay binding configured"}}
        feedback["failed_cases"].append(entry)
        command = commands[name]
        if integrity:
            entry["replay"]["reason"] = "Integrity changes prevent replay"
            continue
        if "replay" not in command:
            continue
        try:
            case = visible_cases(original, command)[case_id]
            entry.update(input_available=True, input={k: case[k] for k in ("module", "function", "args", "kwargs")}, expected=case["expected"])
            if any(r["expected_available"] and r["expected"] != case["expected"] for r in groups.values()):
                raise DoctorError("Visible replay binding disagrees with validation expected result")
            entry["replay"] = _replay(candidate, output / f"case-{len(feedback['failed_cases'])}", candidate_manifest,
                                      command, case_id, case, config, runner)
            replay = entry["replay"]
            if replay["status"] == "completed":
                actual = replay["result"]
                comparable = [r for r in groups.values() if r["actual_available"] and actual["actual_available"]]
                entry["reproduces_validation"] = (any(r["status"] == actual["status"] and r["actual"] == actual["actual"]
                                                      for r in comparable) if comparable else None)
        except (DoctorError, OSError, UnicodeError) as exc:
            entry["replay"] = {"status": "unavailable", "reason": str(exc)}
    write_json(output / "feedback.json", redact(feedback))
    return redact(feedback)


def feedback_view(feedback: dict, *, omit_frame_values: bool = False) -> dict:
    view = copy.deepcopy(feedback)
    for index, case in enumerate(view["failed_cases"]):
        replay = case["replay"]
        replay.pop("audit_file", None)
        replay.pop("audit_sha256", None)
        for span in replay.get("trace", {}).get("spans", []):
            span["evidence_id"] = evidence_id(span)
            if span["span_id"] == "case" and span.get("input") == case.get("input"):
                span.pop("input", None)
                span["input_ref"] = f"/retry_feedback/failed_cases/{index}/input"
            elif omit_frame_values:
                span.pop("input", None)
                span.pop("output", None)
        if omit_frame_values and "trace" in replay:
            replay["trace"]["frame_values"] = "omitted_from_request; captured previews retained in replay audit"
    return view
