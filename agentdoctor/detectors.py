from __future__ import annotations

import ast
import copy
import json
from collections import defaultdict
from pathlib import Path

from .contracts import violations
from .attribution import combine_causes
from .core import DoctorError, digest, parse_json, read_text
from .project import editable
from .polling import bounded_pending_chain
from .traces import evidence_id
from .taxonomy import SYMPTOMS, classification_level


def finding(category: str, severity: str, summary: str, spans: list[dict], *, details: dict | None = None, source: dict | None = None) -> dict:
    evidence = [evidence_id(s) for s in spans]
    identity = json.dumps([category, evidence, summary], sort_keys=True)
    observed = copy.deepcopy(source if source is not None else (spans[0].get("source", {}) if spans else {}))
    return {"id": "f_" + digest(identity)[:12], "category": category, "severity": severity,
            "summary": summary, "evidence": evidence, "source": observed,
            "observed_source": copy.deepcopy(observed), "root_causes": [],
            "classification_level": classification_level(category),
            "details": details or {}, "origin": "rule", "causal_status": "unconfirmed"}


def merge_findings(findings: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Merge overlapping reports of one category/location, retaining their evidence."""
    merged, aliases = [], {}
    priorities = {"low": 0, "medium": 1, "high": 2}
    for original in findings:
        issue = copy.deepcopy(original)
        target = next((f for f in merged if f["category"] == issue["category"] and
                       f["source"].get("path") == issue["source"].get("path") and
                       f["source"].get("function") == issue["source"].get("function") and
                       set(f["evidence"]) & set(issue["evidence"])), None)
        if target is None:
            merged.append(issue)
            continue
        if issue["id"] != target["id"]:
            aliases[issue["id"]] = target["id"]
        support = target.setdefault("supporting_findings", [{k:copy.deepcopy(target[k]) for k in ("id", "summary", "evidence", "details", "origin")}])
        support.extend(issue.get("supporting_findings", [{k:copy.deepcopy(issue[k]) for k in ("id", "summary", "evidence", "details", "origin")}]))
        target["evidence"] = list(dict.fromkeys(target["evidence"] + issue["evidence"]))
        target["root_causes"] = combine_causes(target.get("root_causes", []), issue.get("root_causes", []))
        for key in ("symptom_ids", "classified_by"):
            if key in issue:
                target[key] = list(dict.fromkeys(target.get(key, []) + issue[key]))
        if "classification_bases" in issue:
            target["classification_bases"] = combine_causes(target.get("classification_bases", []), issue["classification_bases"])
        target["severity"] = max((target["severity"], issue["severity"]), key=priorities.get)
        target["origins"] = sorted(set(target.get("origins", [target["origin"]]) + issue.get("origins", [issue["origin"]])))
        target["occurrences"] = len(support)
    return merged, aliases


def detect(spans: list[dict], config: dict, project: Path, manifest: dict[str, str]) -> list[dict]:
    results = []
    loop_groups = defaultdict(list)
    for span in spans:
        for direction in ("input", "output"):
            schema = span.get(direction + "_schema")
            tool = config["tools"].get(span.get("tool")) if span["kind"] == "tool" else None
            if direction == "input" and tool is not None:
                schema = tool["input_schema"]
            if schema is None:
                continue
            value = span.get(direction)
            if direction == "output" and isinstance(value, str) and isinstance(schema, dict) and schema.get("type") in ("object", "array"):
                try:
                    value = parse_json(value)
                except DoctorError:
                    results.append(finding("invalid_json", "high", "Output is not valid JSON required by its contract", [span]))
                    continue
            errors = violations(value, schema)
            if errors:
                category = "tool_argument_schema" if tool is not None and direction == "input" else direction + "_schema"
                results.append(finding(category, "high", f"{direction} violates its declared schema", [span], details={"violations": errors, "tool": span.get("tool")}))
        if span["kind"] == "tool" and span["tool"] not in config["tools"]:
            results.append(finding("unknown_tool", "high", "Tool call has no registered contract", [span], details={"tool": span["tool"]}))
        if span.get("error"):
            results.append(finding("execution_error", "high", "Execution returned an error; cause requires investigation", [span], details={"error": span["error"]}))
        if span.get("task_status") == "failed":
            results.append(finding("task_failed", "high", "Task was recorded as failed", [span]))
        for assertion in span.get("assertions", []):
            if not assertion["passed"]:
                results.append(finding("task_assertion", "high", f"Task assertion failed: {assertion['name']}", [span], details={"assertion": assertion}))
        if span.get("task_status") == "completed" and not span.get("assertions"):
            results.append(finding("unverified_completion", "medium", "Completion was claimed without a recorded task assertion", [span]))
        before, after = span.get("state_before"), span.get("state_after")
        if before is not None and after is not None:
            for key in config["checks"]["preserve_state_keys"]:
                if key in before and (key not in after or before[key] != after[key]):
                    results.append(finding("state_invariant", "high", f"Configured invariant changed or disappeared: {key}", [span], details={"key": key, "before": before[key], "after": after.get(key)}))
        if span["kind"] == "tool":
            # A graph can revisit a node under a fresh parent on every turn.
            # Parent IDs delimit local polling budgets, not loop detection.
            loop_groups[(span["run_id"], span["node"], span["tool"])].append(span)
    threshold = config["checks"]["loop_threshold"]
    for group in loop_groups.values():
        streak = []
        last = None
        for span in group:
            signature = json.dumps([span.get("input"), span.get("output"), span.get("error"), span.get("state_after")], sort_keys=True)
            progressing = "state_before" in span and "state_after" in span and span["state_before"] != span["state_after"]
            streak = streak + [span] if signature == last and not progressing else [span]
            last = signature
            if len(streak) == threshold:
                proof = bounded_pending_chain(group, spans, project, manifest)
                if proof:
                    observation = finding("bounded_polling", "low", "Pending responses exhausted finite budgets across the recorded call chain", proof["spans"], details={"budget": len(group), "basis": "static source and observed counts at every call-chain edge; not a general termination proof", "call_chain": proof["budgets"]})
                    observation["disposition"] = "observation"
                    results.append(observation)
                    break
                else:
                    results.append(finding("possible_loop", "medium", "Repeated identical tool outcomes suggest a loop; intentional polling is also possible", streak))
    for relative in manifest:
        if relative.endswith(".py") and editable(relative, config):
            try:
                ast.parse(read_text(project / relative), filename=relative)
            except SyntaxError as exc:
                linked = [s for s in spans if s.get("source", {}).get("path") == relative]
                results.append(finding("python_syntax", "high", f"Python syntax error: {exc.msg}", linked, source={"path": relative, "line": exc.lineno or 1}))
            except UnicodeError as exc:
                raise DoctorError(f"Source must be UTF-8: {relative}") from exc
    return results


def diagnose(findings: list[dict], spans: list[dict], reference: list[dict] | None = None) -> dict:
    observations = [f for f in findings if f.get("disposition") == "observation"]
    findings, _ = merge_findings([f for f in findings if f.get("disposition") != "observation"])
    groups = defaultdict(list)
    for issue in findings:
        groups[(issue["category"], issue["source"].get("path"), issue["details"].get("tool"))].append(issue)
    hypotheses = []
    recommendations = {
        "tool_argument_schema": "Compare the caller's argument construction with the tool contract; inspect declared aliases before renaming fields.",
        "execution_error": "Reproduce the error and check upstream inputs and external dependencies before changing code.",
        "task_assertion": "Trace the failed business assertion back to the step that first produced an incorrect value.",
        "state_invariant": "Inspect the node's state update and merge behavior for the configured invariant.",
        "possible_loop": "Check termination, retry counters, and whether state makes progress; polling alone is not proof of a defect.",
        "python_syntax": "Fix the syntax in the indicated source location and rerun the frozen task checks.",
    }
    for (category, path, tool), issues in groups.items():
        hypotheses.append({"id": "h_" + digest(json.dumps([category, path, tool]))[:12], "status": "hypothesized",
                           "summary": recommendations.get(category, "Compare the observed behavior with the explicit task and node contracts."),
                           "finding_ids": [i["id"] for i in issues], "source_path": path,
                           "evidence": list(dict.fromkeys(ref for i in issues for ref in i["evidence"])),
                           "occurrences": sum(i.get("occurrences", 1) for i in issues), "origin": "rule"})
    hypotheses.sort(key=lambda h: (not any(i["severity"] == "high" for i in findings if i["id"] in h["finding_ids"]), -h["occurrences"]))
    comparisons = []
    if reference:
        # Comparisons require explicit task identity; do not pair arbitrary traces.
        task_ids = {s.get("metadata", {}).get("task_id") for s in spans} - {None}
        for task_id in sorted(task_ids):
            failed = [s for s in spans if s.get("metadata", {}).get("task_id") == task_id]
            successful = [s for s in reference if s.get("metadata", {}).get("task_id") == task_id]
            if not successful or not any(s.get("task_status") == "completed" and s.get("assertions") and all(a["passed"] for a in s["assertions"]) for s in successful):
                continue
            def signature(s):
                return {"node": s["node"], "tool": s.get("tool"), "input": s.get("input"), "error": s.get("error")}
            pairs = zip(failed, successful)
            first = next(((a, b) for a, b in pairs if signature(a) != signature(b)), None)
            comparisons.append({"task_id": task_id, "method": "sequence comparison, not causal proof",
                                "failed_steps": len(failed), "reference_steps": len(successful),
                                "first_difference": [evidence_id(s) for s in first] if first else [],
                                "note": "Different schedules or model outputs may cause harmless sequence differences."})
    return {"findings": findings, "hypotheses": hypotheses, "trace_comparisons": comparisons, "observations": observations,
            "coverage": {"spans": len(spans), "semantic_checks": "not_run", "source_checks": "Python AST only",
                         "unclassified_symptoms": [f["id"] for f in findings if f["category"] in SYMPTOMS and not f.get("classified_by")]}}
