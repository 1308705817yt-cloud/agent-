from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable

from .analysis import add_model_analysis, model_context
from .core import DoctorError, digest, redact, utc_now, write_json
from .detectors import detect, diagnose, finding
from .model import ANALYSIS_INSTRUCTION, PATCH_INSTRUCTION, SYSTEM, ModelProvider, analysis_schema, patch_schema
from .patches import alias_proposals, apply_proposal
from .project import check_manifest, load_config, snapshot
from .report import write_report
from .runner import ValidationRunner, baseline_gate, candidate_gate
from .traces import evidence_id, load_traces
from .retry import failure_feedback, patch_base, snapshot_version


def run(project: str | Path, traces: str | Path, *, mode: str = "diagnose", config_file: str = "doctor.json",
        output: str | Path | None = None, executor: str = "docker", model: ModelProvider | None = None,
        reference: str | Path | None = None, progress: Callable[[str], None] | None = None) -> dict:
    if mode not in ("diagnose", "repair"):
        raise DoctorError("Mode must be diagnose or repair")
    project = Path(project).resolve()
    config = load_config(project, config_file)
    spans = load_traces(Path(traces).resolve())
    reference_spans = load_traces(Path(reference).resolve()) if reference else []
    if {evidence_id(s) for s in spans} & {evidence_id(s) for s in reference_spans}:
        raise DoctorError("Current and reference traces must use distinct run_id/span_id evidence IDs")
    output_root = Path(output).resolve() if output else project / ".agentdoctor"
    if project == output_root or project.is_relative_to(output_root):
        raise DoctorError("Output must not be the project directory or one of its ancestors")
    job = output_root / ("run-" + uuid.uuid4().hex[:12])
    job.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "run_id": job.name, "mode": mode, "status": "running", "reason": "",
              "started_at": utc_now(), "project": str(project), "workspace": str(job), "executor": executor if mode == "repair" else "none",
              "diagnosis": {}, "baseline": None, "attempts": [], "verified_patch": None, "warnings": [],
              "repair_termination": "not_started", "model_events": [],
              "model": {"provider": model.provider, "name": model.name, "metadata": getattr(model, "metadata", {})} if model else {"provider": "none"},
              "scope": "Verification applies only to the configured checks and repetitions; it does not prove universal correctness."}
    manifest = {}

    def stage(name: str):
        report["stage"] = name
        write_json(job / "state.json", redact(report))
        if progress:
            progress(name)

    def request_model(operation: str, instruction: str, context: dict, schema: dict):
        event = {"operation": operation, "started_at": utc_now(), "calls_before": model.calls, "status": "running"}
        report["model_events"].append(event)
        request_path = job / "model-requests" / f"{len(report['model_events']):02}-{operation}.json"
        write_json(request_path, redact({"operation": operation, "system": SYSTEM, "instruction": instruction,
                                         "context": context, "schema": schema}))
        event.update(request_file=str(request_path), request_sha256=digest(request_path.read_bytes()),
                     context_chars=len(json.dumps(context, ensure_ascii=False)))
        audit_path = job / "model-context-audits" / f"{len(report['model_events']):02}-{operation}.json"
        if audit_path.exists():
            event.update(context_audit_file=str(audit_path), context_audit_sha256=digest(audit_path.read_bytes()))
        previous_usage = len(model.usage)
        try:
            result = model.complete(instruction, context, schema)
            event.update(status="completed", output=redact(result))
            return result
        except DoctorError as exc:
            event.update(status="failed", error=str(exc))
            raise
        finally:
            event.update(finished_at=utc_now(), calls_after=model.calls, usage=model.usage[previous_usage:])

    def context_for(operation: str, history: list[dict]) -> dict:
        audit = {}
        try:
            feedback = None
            basis = None
            if operation == "patch_proposal":
                if report["attempts"]:
                    latest = report["attempts"][-1]
                    if latest.get("validation") is not None:
                        if "retry_feedback" not in latest:
                            stage(f"collect_visible_failure_evidence_{latest['number']}")
                            latest["retry_feedback"] = failure_feedback(
                                job / "snapshot", job / f"candidate-{latest['number']}", job / f"retry-evidence-{latest['number']}",
                                manifest, {**manifest, **latest["changed_hashes"]}, latest, report["baseline"], config, runner)
                        feedback = latest["retry_feedback"]
                basis = patch_base(manifest, report["attempts"])
            return model_context(job / "snapshot", manifest, spans, report["diagnosis"], config, history, reference_spans,
                                 audit=audit, patch_basis=basis, retry_feedback=feedback)
        finally:
            # Full discovery and validation output remain available even when
            # the compact request is still too large to send.
            path = job / "model-context-audits" / f"{len(report['model_events']) + 1:02}-{operation}.json"
            write_json(path, redact(audit))

    def finish(status: str, reason: str) -> dict:
        report.update(status=status, reason=reason, finished_at=utc_now(), stage="finished")
        if manifest:
            changes = check_manifest(project, manifest, excluded=[output_root])
            if changes:
                report["warnings"].append("Original project changed during analysis: " + ", ".join(changes))
                if status == "verified":
                    report.update(status="inconclusive", reason="Original project changed; re-run against the current revision", verified_patch=None)
        if model:
            report["model"].update(calls=model.calls, usage=model.usage)
        write_json(job / "diagnosis.json", redact(report["diagnosis"]))
        write_json(job / "model-events.json", redact(report["model_events"]))
        write_json(job / "verification.json", redact({k: report[k] for k in ("status", "reason", "baseline", "attempts", "verified_patch", "scope")}))
        write_json(job / "state.json", redact(report))
        write_report(job, redact(report))
        return report

    try:
        stage("snapshot")
        manifest = snapshot(project, job / "snapshot", excluded=[output_root])
        report["snapshot_id"] = snapshot_version(manifest)
        if load_config(job / "snapshot", config_file) != config:
            raise DoctorError("Project configuration changed while creating the snapshot; re-run with a stable revision")
        write_json(job / "manifest.json", manifest)
        write_json(job / "config.frozen.json", config)
        write_json(job / "traces.redacted.json", redact(spans))
        write_json(job / "reference-traces.redacted.json", redact(reference_spans))
        stage("detect_and_diagnose")
        findings = detect(spans, config, job / "snapshot", manifest)
        report["diagnosis"] = diagnose(findings, spans, reference_spans)
        if model:
            model.max_calls = min(model.max_calls, config["policy"]["max_model_calls"])
            stage("semantic_analysis")
            try:
                context = context_for("semantic_analysis", [])
                result = request_model("semantic_analysis", ANALYSIS_INSTRUCTION, context, analysis_schema(context))
                decisions = add_model_analysis(report["diagnosis"], result, spans, manifest, reference_spans,
                                               {e["id"] for e in context["evidence_catalog"]}, context["sources"])
                report["model_events"][-1].update(semantic_validation="accepted", alert_decisions=decisions)
            except DoctorError as exc:
                report["warnings"].append(str(exc))
                report["diagnosis"]["coverage"]["semantic_checks"] = "failed"
                if report["model_events"]:
                    report["model_events"][-1].update(semantic_validation="rejected", validation_error=str(exc))
        if mode == "diagnose":
            if report["diagnosis"]["coverage"]["semantic_checks"] == "failed":
                return finish("inconclusive", "Semantic diagnosis could not be validated; rule findings remain available")
            if not report["diagnosis"]["findings"]:
                return finish("inconclusive", "No issues detected in the available evidence; this is not a health certification")
            return finish("diagnosed", "Findings and root-cause hypotheses recorded; no target code was executed")
        runner = ValidationRunner(executor)
        ready, description = runner.available()
        report["executor_description"] = description
        if not ready:
            return finish("inconclusive", description)
        kinds = {c["kind"] for c in config["validation"]["commands"]}
        if not {"target", "regression"}.issubset(kinds):
            return finish("inconclusive", "Configure at least one target and one regression command before repair")
        if any(not c.get("expected_cases") for c in config["validation"]["commands"]):
            return finish("inconclusive", "Configure nonempty expected_cases and completion evidence for every validation command; exit codes alone are insufficient")
        stage("baseline_validation")
        snapshot(job / "snapshot", job / "baseline")
        report["baseline"] = runner.run_suite(job / "baseline", config, manifest)
        ok, reason = baseline_gate(report["baseline"])
        if not ok:
            return finish("inconclusive", reason)
        for row in report["baseline"]["commands"]:
            if row["kind"] == "target" and row["returncode"] != 0 and row["repetition"] == 1:
                issue = finding("validation_failure", "high", "Frozen target check failed: " + row["name"], [])
                issue["evidence"] = ["validation/" + row["name"]]
                issue["details"] = {"stdout": row["stdout"], "stderr": row["stderr"]}
                report["diagnosis"]["findings"].append(issue)
        candidates = alias_proposals(job / "snapshot", spans, report["diagnosis"]["findings"], config)
        report["repair_termination"] = "candidate_budget"
        seen = set()
        for number in range(1, config["policy"]["max_candidates"] + 1):
            stage(f"propose_patch_{number}")
            if candidates:
                proposal = candidates.pop(0)
                origin = "declared_alias"
            elif model:
                try:
                    history = [{k: a.get(k) for k in ("number", "base_snapshot_id", "candidate_version", "summary", "reason", "validation", "proposal")} for a in report["attempts"]]
                    history.insert(0, {"baseline": report["baseline"]})
                    context = context_for("patch_proposal", history)
                    proposal = request_model("patch_proposal", PATCH_INSTRUCTION, context, patch_schema(report["snapshot_id"]))
                    origin = "model"
                except DoctorError as exc:
                    report["warnings"].append(str(exc))
                    report["repair_termination"] = "model_error"
                    break
                if not proposal["edits"]:
                    report["warnings"].append(proposal["summary"])
                    report["repair_termination"] = "no_patch"
                    break
            else:
                report["repair_termination"] = "no_patch"
                break
            fingerprint = digest(json.dumps(proposal["edits"], sort_keys=True))
            if fingerprint in seen:
                report["warnings"].append("Repeated patch proposal; stopped to avoid a repair loop")
                report["repair_termination"] = "duplicate_proposal"
                break
            seen.add(fingerprint)
            attempt = {"number": number, "origin": origin, "summary": proposal["summary"], "proposal": proposal,
                       "status": "proposed", "application_status": "not_applied", "failure_stage": None,
                       "base_snapshot_id": report["snapshot_id"]}
            report["attempts"].append(attempt)
            candidate_dir = job / f"candidate-{number}"
            snapshot(job / "snapshot", candidate_dir)
            try:
                patch = apply_proposal(candidate_dir, proposal, config, {f["id"] for f in report["diagnosis"]["findings"]}, manifest)
                attempt["application_status"] = "applied"
                patch_file = job / f"candidate-{number}.patch"
                patch_file.write_text(patch["diff"], encoding="utf-8")
                attempt.update(patch=str(patch_file), changed_files=patch["changed_files"], changed_lines=patch["changed_lines"])
                candidate_manifest = {**manifest, **patch["hashes"]}
                attempt.update(candidate_version=snapshot_version(candidate_manifest), changed_hashes=patch["hashes"])
                stage(f"verify_patch_{number}")
                attempt["validation"] = runner.run_suite(candidate_dir, config, candidate_manifest)
                accepted, reason = candidate_gate(report["baseline"], attempt["validation"])
                attempt.update(status="verified" if accepted else "rejected", reason=reason,
                               failure_stage=None if accepted else "visible_validation")
                if accepted:
                    report["repair_termination"] = "verified"
                    report["verified_patch"] = str(patch_file)
                    report["candidate_project"] = str(candidate_dir)
                    for hypothesis in report["diagnosis"]["hypotheses"]:
                        if set(hypothesis["finding_ids"]) & set(proposal["finding_ids"]):
                            hypothesis["status"] = "supported_by_configured_checks"
                    return finish("verified", reason)
            except (DoctorError, UnicodeError) as exc:
                phase = "policy" if attempt["application_status"] == "not_applied" else "validation_error"
                attempt.update(status="rejected", reason=str(exc), failure_stage=phase)
                if phase == "policy":
                    attempt["application_status"] = "rejected"
        if report["attempts"]:
            return finish("rejected", "No candidate passed the configured validation within the repair budget")
        return finish("inconclusive", "No justified patch available; configure a model or supply a narrower reproduction and source mapping")
    except (DoctorError, OSError, UnicodeError) as exc:
        return finish("inconclusive", str(exc))
