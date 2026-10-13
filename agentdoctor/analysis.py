"""Bounded diagnostic context and evidence-checked semantic alert merging."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .contracts import validate
from .context_view import dependency_view, diagnostic_view, history_view
from .attribution import checked_cause, combine_causes
from .core import DoctorError, digest, read_text, redact
from .detectors import finding, merge_findings
from .dependencies import dependency_locations
from .model import ANALYSIS_SCHEMA, CATEGORY_DEFINITIONS
from .project import editable
from .source_context import source_variants
from .traces import evidence_id
from .taxonomy import SYMPTOMS, classification_reason
from .retry import feedback_view


def model_context(project: Path, manifest: dict, spans: list[dict], diagnosis: dict, config: dict,
                  history: list[dict], reference_spans: list[dict] | None = None, *, audit: dict | None = None,
                  patch_basis: dict | None = None, retry_feedback: dict | None = None) -> dict:
    references = reference_spans or []
    hypotheses = sorted(diagnosis.get("hypotheses", []), key=lambda h: h.get("origin") != "model")
    comparisons = diagnosis.get("trace_comparisons", [])
    observations = diagnosis.get("observations", [])
    findings = diagnosis["findings"]
    priority = [ref for c in comparisons for ref in c.get("first_difference", [])]
    priority += [ref for h in hypotheses for ref in h.get("evidence", [])]
    priority += [ref for f in findings for ref in f["evidence"]]
    priority += [ref for f in findings for c in f.get("root_causes", []) for ref in c["evidence"]]
    priority += [ref for o in observations for ref in o["evidence"]]
    needed = set(priority)
    by_id = {evidence_id(s): s for s in spans + references}
    pending = list(needed)
    while pending:
        span = by_id.get(pending.pop(), {})
        parent = f"{span.get('run_id')}/{span.get('parent_id')}" if span.get("parent_id") else None
        if parent in by_id and parent not in needed:
            needed.add(parent)
            priority.append(parent)
            pending.append(parent)
    trace_ids = {evidence_id(s) for s in spans + references}
    # Frozen validation findings carry their own output; these are not trace citations.
    validation = {ref for f in findings if f.get("details") for ref in f["evidence"] if ref.startswith("validation/")}
    trace_only = {ref for c in comparisons for ref in c.get("first_difference", [])}
    trace_only |= {ref for o in observations for ref in o["evidence"]}
    missing = (needed - trace_ids - validation) | (trace_only - trace_ids)
    finding_ids = {f["id"] for f in findings}
    missing_findings = {ref for h in hypotheses for ref in h.get("finding_ids", [])} - finding_ids
    if missing or missing_findings:
        raise DoctorError(f"Incomplete model context: missing evidence {sorted(missing)} or finding dependencies {sorted(missing_findings)}")
    rank = {ref: i for i, ref in reversed(list(enumerate(priority)))}

    def ordered(values):
        # The sampling cap applies only to optional context. Required evidence
        # is retained as a complete dependency set, even above forty spans.
        ordered_spans = sorted(values, key=lambda s: rank.get(evidence_id(s), len(rank)))
        return [{**copy.deepcopy(s), "evidence_id": evidence_id(s)} for i, s in enumerate(ordered_spans)
                if i < 40 or evidence_id(s) in needed]

    current_context = ordered(spans)
    locations = {}

    def locate(source):
        if source.get("path"):
            locations.setdefault(source["path"], []).append(source)

    for item in findings + observations:
        locate(item.get("source", {}))
        for cause in item.get("root_causes", []):
            locate(cause["source_evidence"])
    for hypothesis in hypotheses:
        locate({"path": hypothesis.get("source_path")})
    # Aggregate findings may omit a function/line present in their evidence.
    # Reference locations may belong to a different source revision.
    for span in current_context:
        if span["evidence_id"] in needed or not needed:
            locate(span.get("source", {}))
    # Replay line numbers belong to the candidate. Locate its named functions
    # afresh in ORIGINAL sources instead of applying candidate line offsets.
    for case in (retry_feedback or {}).get("failed_cases", []):
        for span in case["replay"].get("trace", {}).get("spans", []):
            source = span.get("source", {})
            if source.get("path") in manifest:
                locate({"path": source["path"], **({"function": source["function"]}
                       if source.get("function") and source["function"] != "<module>" else {})})
    locations, dependencies = dependency_locations(project, manifest, locations, config)
    if audit is not None:
        audit.update(dependency_context=copy.deepcopy(dependencies), previous_attempts=copy.deepcopy(history))
        if patch_basis is not None:
            audit["patch_base"] = copy.deepcopy(patch_basis)
        if retry_feedback is not None:
            audit["retry_feedback"] = copy.deepcopy(retry_feedback)
    compact_dependencies = dependency_view(dependencies)
    hot_paths = set(locations)
    paths = sorted((p for p in manifest if editable(p, config)), key=lambda p: (p not in hot_paths, p))
    pools = {"spans": current_context, "reference_spans": ordered(references), "findings": copy.deepcopy(findings),
             "hypotheses": copy.deepcopy(hypotheses), "trace_comparisons": copy.deepcopy(comparisons),
             "observations": copy.deepcopy(observations), "previous_attempts": history_view(history[-3:]), "sources": []}
    focused_sources = {}
    omit_replay_frame_values = False
    optional_paths = [p for p in paths if p not in hot_paths][:12]
    for relative in paths:
        if relative not in hot_paths and relative not in optional_paths:
            continue
        try:
            content = read_text(project / relative)
        except UnicodeError as exc:
            if relative in hot_paths:
                raise DoctorError(f"Incomplete model context: located source must be UTF-8: {relative}") from exc
            continue
        full, focused = source_variants(relative, manifest[relative], content, locations.get(relative, []))
        pools["sources"].extend(full)
        focused_sources[relative] = focused

    def assemble():
        context = diagnostic_view(pools, manifest)
        context.update(contracts=config["tools"], invariants=config["checks"], allowed_paths=paths,
                       dependency_context=compact_dependencies,
                       category_definitions=CATEGORY_DEFINITIONS, symptom_categories=sorted(SYMPTOMS),
                       evidence_catalog=[{"id": s["evidence_id"], "trace": role} for key, role in (("spans", "current"), ("reference_spans", "reference")) for s in context[key]])
        if patch_basis is not None:
            context["patch_base"] = copy.deepcopy(patch_basis)
        if retry_feedback is not None:
            context["retry_feedback"] = feedback_view(retry_feedback, omit_frame_values=omit_replay_frame_values)
        for key, total in (("spans", len(spans)), ("reference_spans", len(references)), ("findings", len(findings)),
                           ("hypotheses", len(hypotheses)), ("trace_comparisons", len(comparisons)),
                           ("observations", len(observations)), ("sources", len(paths)), ("previous_attempts", len(history))):
            included = len({s["path"] for s in context[key]}) if key == "sources" else len(context[key])
            context["omitted_" + key] = total - included
        return redact(context)

    while True:
        context = assemble()
        size = len(json.dumps(context, ensure_ascii=False))
        budget = config["policy"]["max_context_chars"]
        if size <= budget:
            return context
        trimmed = False
        for key in ("spans", "reference_spans"):
            unused = next((s for s in reversed(pools[key]) if s["evidence_id"] not in needed), None)
            if unused is not None and (key == "reference_spans" or len(pools[key]) > 1):
                pools[key].remove(unused)
                trimmed = True
                break
        if trimmed:
            continue
        optional = next((s for s in reversed(pools["sources"]) if s["path"] not in hot_paths), None)
        if optional is not None:
            pools["sources"] = [s for s in pools["sources"] if s["path"] != optional["path"]]
            continue
        if len(pools["previous_attempts"]) > 1:
            pools["previous_attempts"].pop(0)
            continue
        # Compact the largest source saving first, keeping every located range.
        savings = []
        for relative, focused in focused_sources.items():
            present = [s for s in pools["sources"] if s["path"] == relative]
            if present and present != focused:
                saved = len(json.dumps(redact(present), ensure_ascii=False)) - len(json.dumps(redact(focused), ensure_ascii=False))
                if saved > 0:
                    savings.append((saved, relative))
        if savings:
            relative = max(savings)[1]
            index = next(i for i, s in enumerate(pools["sources"]) if s["path"] == relative)
            pools["sources"] = pools["sources"][:index] + focused_sources[relative] + [s for s in pools["sources"][index:] if s["path"] != relative]
            continue
        if retry_feedback is not None and not omit_replay_frame_values:
            # Keep the complete case input/outcome, trace identities, call edges,
            # locations and source versions. Incidental frame previews are an
            # optional projection; full captured values remain in the audit.
            omit_replay_frame_values = True
            continue
        raise DoctorError(f"Essential model context exceeds max_context_chars ({size} > {budget}); "
                          "complete diagnostic evidence and located source cannot fit. Reduce the trace/project scope or increase the budget")


def add_model_analysis(diagnosis: dict, result: dict, spans: list[dict], manifest: dict,
                       reference_spans: list[dict] | None = None, visible_evidence_ids: set[str] | None = None,
                       supplied_sources: list[dict] | None = None) -> dict:
    validate(result, ANALYSIS_SCHEMA, "model analysis")
    current = {evidence_id(s): s for s in spans}
    by_ref = {**{evidence_id(s): s for s in (reference_spans or [])}, **current}
    visible = set(by_ref) if visible_evidence_ids is None else visible_evidence_ids
    # Reject atomically, including valid but unsupplied and reference-only citations.
    for kind in ("findings", "hypotheses"):
        for entry in result[kind]:
            refs = set(entry["evidence"])
            allowed = set(current) if kind == "findings" else set(by_ref)
            if not refs or not refs <= allowed & visible or not refs & set(current):
                raise DoctorError("Model analysis cited missing or fabricated evidence, or evidence outside the supplied current context")
            if entry.get("source_path") is not None and entry["source_path"] not in manifest:
                raise DoctorError("Model analysis cited an unknown source file")
    # Validate the entire response before mutating the diagnosis. A valid file
    # in the manifest is insufficient: its cited lines must have been supplied.
    new_causes = []
    for entry in result["findings"]:
        if entry.get("observed_at") is not None and entry["observed_at"] not in entry["evidence"]:
            raise DoctorError("Observed location must reference one of the finding's cited current spans")
        cause = entry.get("root_cause")
        new_causes.append(checked_cause(cause, entry["evidence"], current, visible, manifest, supplied_sources or []) if cause else None)
    existing = {f["id"]: f for f in diagnosis["findings"]}
    attributed = []
    for entry in result.get("attributions", []):
        if entry["finding_id"] not in existing:
            raise DoctorError("Root cause references an unknown finding_id")
        issue = existing[entry["finding_id"]]
        classification = entry.get("classification")
        if classification:
            if issue["category"] not in SYMPTOMS:
                raise DoctorError("Only symptom-level findings can receive a new fault classification; existing fault categories must be retained")
            if not classification["contract"].strip() or not classification["violation"].strip():
                raise DoctorError("Symptom classification requires an explicit contract and violation")
        cause = checked_cause(entry["root_cause"], issue["evidence"], current, visible, manifest, supplied_sources or [])
        attributed.append((issue, cause, classification))
    pending = []
    decisions = []
    for issue, cause, classification in attributed:
        issue["root_causes"] = combine_causes(issue.get("root_causes", []), [cause])
        if classification is None:
            decisions.append({"disposition": "attributed", "finding_id": issue["id"], "causal_status": "hypothesized"})
            continue
        cited = [current[ref] for ref in cause["evidence"]]
        reason = classification_reason(classification["category"], cited, diagnosis, observed_evidence=issue["evidence"])
        if reason:
            pending.append({"summary": classification["violation"], "evidence": cause["evidence"],
                            "source_path": cause["source"]["path"], "assessment_note": reason})
            decisions.append({"disposition": "hypothesis_only", "finding_id": issue["id"],
                              "proposed_classification": copy.deepcopy(classification), "reason": reason})
            continue
        fault = finding(classification["category"], issue["severity"], classification["violation"], cited, source=issue["source"])
        fault.update(origin="model", root_causes=[cause], symptom_ids=[issue["id"]],
                     classification_bases=[{"symptom_id": issue["id"], **copy.deepcopy(classification)}])
        diagnosis["findings"].append(fault)
        decisions.append({"disposition": "classified", "finding_id": fault["id"], "symptom_id": issue["id"]})
    for entry, cause in zip(result["findings"], new_causes):
        cited = [current[ref] for ref in entry["evidence"]]
        observed = current[entry.get("observed_at", entry["evidence"][0])]
        issue = finding(entry["category"], entry["severity"], entry["summary"], cited, source=observed.get("source", {}))
        if cause:
            issue["root_causes"] = [cause]
        issue["origin"] = "model"
        reason = classification_reason(entry["category"], cited, diagnosis, observed_evidence=[evidence_id(observed)])
        if reason:
            pending.append({"summary": entry["summary"], "evidence": entry["evidence"],
                            "source_path": issue["source"].get("path"), "assessment_note": reason})
            decisions.append({"disposition": "hypothesis_only", "proposed_finding": issue, "reason": reason})
        else:
            diagnosis["findings"].append(issue)
            decisions.append({"disposition": "accepted", "finding_id": issue["id"]})
    diagnosis["findings"], aliases = merge_findings(diagnosis["findings"])
    by_id = {f["id"]: f for f in diagnosis["findings"]}
    for fault in diagnosis["findings"]:
        for symptom_id in fault.get("symptom_ids", []):
            symptom = by_id.get(aliases.get(symptom_id, symptom_id))
            if symptom is not None:
                symptom["classified_by"] = list(dict.fromkeys(symptom.get("classified_by", []) + [fault["id"]]))
    for hypothesis in diagnosis["hypotheses"]:
        hypothesis["finding_ids"] = list(dict.fromkeys(aliases.get(ref, ref) for ref in hypothesis["finding_ids"]))
    for decision in decisions:
        ref = decision.get("finding_id")
        if ref in aliases:
            decision.update(disposition="merged", finding_id=aliases[ref], proposed_finding_id=ref)
    for entry in result["hypotheses"] + pending:
        related = [f["id"] for f in diagnosis["findings"] if set(f["evidence"]) & set(entry["evidence"])]
        hypothesis = {"id": "h_" + digest(json.dumps(entry, sort_keys=True))[:12], "status": "hypothesized",
                      **copy.deepcopy(entry), "finding_ids": related, "origin": "model"}
        if hypothesis["id"] not in {h["id"] for h in diagnosis["hypotheses"]}:
            diagnosis["hypotheses"].append(hypothesis)
    diagnosis.setdefault("analysis_decisions", []).extend(decisions)
    diagnosis["coverage"]["semantic_checks"] = "model_assisted; not independently calibrated"
    diagnosis["coverage"]["unclassified_symptoms"] = [f["id"] for f in diagnosis["findings"] if f["category"] in SYMPTOMS and not f.get("classified_by")]
    return {key: sum(d["disposition"] == key for d in decisions) for key in ("accepted", "merged", "hypothesis_only", "attributed", "classified")}
