from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable

from .contracts import validate
from .core import DoctorError, digest, read_text, redact, utc_now, write_json
from .detectors import detect, diagnose, finding
from .model import ANALYSIS_SCHEMA, PATCH_SCHEMA, ModelClient
from .patches import alias_proposals, apply_proposal
from .project import check_manifest, editable, load_config, snapshot
from .report import write_report
from .runner import ValidationRunner, baseline_gate, candidate_gate
from .traces import evidence_id, load_traces


def model_context(project: Path, manifest: dict, spans: list[dict], diagnosis: dict, config: dict, history: list[dict]) -> dict:
    limit = config["policy"]["max_context_chars"]
    relevant = {ref for issue in diagnosis["findings"] for ref in issue["evidence"]}
    ordered = sorted(spans, key=lambda s: evidence_id(s) not in relevant)
    hot_paths = {issue["source"].get("path") for issue in diagnosis["findings"]}
    paths = sorted((p for p in manifest if editable(p, config)), key=lambda p: (p not in hot_paths, p))
    context = {"contracts": config["tools"], "invariants": config["checks"], "allowed_paths": paths,
               "findings": diagnosis["findings"][:40], "spans": ordered[:40], "sources": [],
               "previous_attempts": history[-3:], "omitted_spans": max(0, len(spans) - 40), "omitted_sources": 0}
    for relative in paths[:12]:
        try:
            content = read_text(project / relative)
        except UnicodeError:
            continue
        context["sources"].append({"path": relative, "sha256": manifest[relative], "text": content[:12000], "truncated": len(content) > 12000})
    context["omitted_sources"] = len(paths) - len(context["sources"])
    context = redact(context)
    while len(json.dumps(context, ensure_ascii=False)) > limit:
        if len(context["spans"]) > 1:
            context["spans"].pop()
            context["omitted_spans"] += 1
        elif len(context["sources"]) > 1:
            context["sources"].pop()
            context["omitted_sources"] += 1
        elif context["previous_attempts"]:
            context["previous_attempts"].pop(0)
        else:
            raise DoctorError("Essential model context exceeds max_context_chars; reduce the trace/project scope or increase the budget")
    return context


def add_model_analysis(diagnosis: dict, result: dict, spans: list[dict], manifest: dict) -> None:
    validate(result, ANALYSIS_SCHEMA, "model analysis")
    by_ref = {evidence_id(s): s for s in spans}
    # Validate the entire response before merging any part of it.
    for entry in result["findings"] + result["hypotheses"]:
        if not entry["evidence"] or not set(entry["evidence"]).issubset(by_ref):
            raise DoctorError("Model analysis cited missing or fabricated evidence")
        if entry.get("source_path") is not None and entry["source_path"] not in manifest:
            raise DoctorError("Model analysis cited an unknown source file")
    for entry in result["findings"]:
        issue = finding(entry["category"], entry["severity"], entry["summary"], [by_ref[r] for r in entry["evidence"]])
        issue["origin"] = "model"
        if issue["id"] not in {f["id"] for f in diagnosis["findings"]}:
            diagnosis["findings"].append(issue)
    for entry in result["hypotheses"]:
        related = [f["id"] for f in diagnosis["findings"] if set(f["evidence"]) & set(entry["evidence"])]
        diagnosis["hypotheses"].append({"id": "h_" + digest(json.dumps(entry, sort_keys=True))[:12], "status": "hypothesized",
                                       "summary": entry["summary"], "finding_ids": related, "source_path": entry["source_path"],
                                       "evidence": entry["evidence"], "origin": "model"})
    diagnosis["coverage"]["semantic_checks"] = "model_assisted; not independently calibrated"


def run(project: str | Path, traces: str | Path, *, mode: str = "diagnose", config_file: str = "doctor.json",
        output: str | Path | None = None, executor: str = "docker", model: ModelClient | None = None,
        reference: str | Path | None = None, progress: Callable[[str], None] | None = None) -> dict:
    if mode not in ("diagnose", "repair"):
        raise DoctorError("Mode must be diagnose or repair")
    project = Path(project).resolve()
    config = load_config(project, config_file)
    spans = load_traces(Path(traces).resolve())
    reference_spans = load_traces(Path(reference).resolve()) if reference else []
    output_root = Path(output).resolve() if output else project / ".agentdoctor"
    if project == output_root or project.is_relative_to(output_root):
        raise DoctorError("Output must not be the project directory or one of its ancestors")
    job = output_root / ("run-" + uuid.uuid4().hex[:12])
    job.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "run_id": job.name, "mode": mode, "status": "running", "reason": "",
              "started_at": utc_now(), "project": str(project), "workspace": str(job), "executor": executor if mode == "repair" else "none",
              "diagnosis": {}, "baseline": None, "attempts": [], "verified_patch": None, "warnings": [],
              "model": {"provider": model.provider, "name": model.name} if model else {"provider": "none"},
              "scope": "Verification applies only to the configured checks and repetitions; it does not prove universal correctness."}
    manifest = {}

    def stage(name: str):
        report["stage"] = name
        write_json(job / "state.json", redact(report))
        if progress:
            progress(name)

    def finish(status: str, reason: str) -> dict:
        report.update(status=status, reason=reason, finished_at=utc_now(), stage="finished")
        if manifest:
            changes = check_manifest(project, manifest)
            if changes:
                report["warnings"].append("Original project changed during analysis: " + ", ".join(changes))
                if status == "verified":
                    report.update(status="inconclusive", reason="Original project changed; re-run against the current revision", verified_patch=None)
        if model:
            report["model"].update(calls=model.calls, usage=model.usage)
        write_json(job / "diagnosis.json", redact(report["diagnosis"]))
        write_json(job / "verification.json", redact({k: report[k] for k in ("status", "reason", "baseline", "attempts", "verified_patch", "scope")}))
        write_json(job / "state.json", redact(report))
        write_report(job, redact(report))
        return report

    try:
        stage("snapshot")
        manifest = snapshot(project, job / "snapshot", excluded=[output_root])
        if load_config(job / "snapshot", config_file) != config:
            raise DoctorError("Project configuration changed while creating the snapshot; re-run with a stable revision")
        write_json(job / "manifest.json", manifest)
        write_json(job / "config.frozen.json", config)
        write_json(job / "traces.redacted.json", redact(spans))
        stage("detect_and_diagnose")
        findings = detect(spans, config, job / "snapshot", manifest)
        report["diagnosis"] = diagnose(findings, spans, reference_spans)
        if model:
            model.max_calls = min(model.max_calls, config["policy"]["max_model_calls"])
            stage("semantic_analysis")
            try:
                context = model_context(job / "snapshot", manifest, spans, report["diagnosis"], config, [])
                result = model.complete("Identify prompt contradictions, plan/action mismatches, and causal hypotheses supported by the evidence. Do not treat benign exception handling or polling as failures.", context, ANALYSIS_SCHEMA)
                add_model_analysis(report["diagnosis"], result, spans, manifest)
            except DoctorError as exc:
                report["warnings"].append(str(exc))
                report["diagnosis"]["coverage"]["semantic_checks"] = "failed"
        if mode == "diagnose":
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
        seen = set()
        for number in range(1, config["policy"]["max_candidates"] + 1):
            stage(f"propose_patch_{number}")
            if candidates:
                proposal = candidates.pop(0)
                origin = "declared_alias"
            elif model:
                try:
                    history = [{"summary": a.get("summary"), "reason": a.get("reason"), "validation": a.get("validation"), "proposal": a.get("proposal")} for a in report["attempts"]]
                    history.insert(0, {"baseline": report["baseline"]})
                    context = model_context(job / "snapshot", manifest, spans, report["diagnosis"], config, history)
                    proposal = model.complete("Propose ONE minimal patch to the original snapshot. Use exact, unique before/after source fragments. Cite existing finding_ids. Never edit protected tests, tool contracts, permissions or validation criteria. Account for previous failed candidates. If no justified patch exists, return edits=[].", context, PATCH_SCHEMA)
                    origin = "model"
                except DoctorError as exc:
                    report["warnings"].append(str(exc))
                    break
                if not proposal["edits"]:
                    report["warnings"].append(proposal["summary"])
                    break
            else:
                break
            fingerprint = digest(json.dumps(proposal["edits"], sort_keys=True))
            if fingerprint in seen:
                report["warnings"].append("Repeated patch proposal; stopped to avoid a repair loop")
                break
            seen.add(fingerprint)
            attempt = {"number": number, "origin": origin, "summary": proposal["summary"], "proposal": proposal, "status": "proposed"}
            report["attempts"].append(attempt)
            candidate_dir = job / f"candidate-{number}"
            snapshot(job / "snapshot", candidate_dir)
            try:
                patch = apply_proposal(candidate_dir, proposal, config, {f["id"] for f in report["diagnosis"]["findings"]}, manifest)
                patch_file = job / f"candidate-{number}.patch"
                patch_file.write_text(patch["diff"], encoding="utf-8")
                attempt.update(patch=str(patch_file), changed_files=patch["changed_files"], changed_lines=patch["changed_lines"])
                candidate_manifest = {**manifest, **patch["hashes"]}
                stage(f"verify_patch_{number}")
                attempt["validation"] = runner.run_suite(candidate_dir, config, candidate_manifest)
                accepted, reason = candidate_gate(report["baseline"], attempt["validation"])
                attempt.update(status="verified" if accepted else "rejected", reason=reason)
                if accepted:
                    report["verified_patch"] = str(patch_file)
                    report["candidate_project"] = str(candidate_dir)
                    for hypothesis in report["diagnosis"]["hypotheses"]:
                        if set(hypothesis["finding_ids"]) & set(proposal["finding_ids"]):
                            hypothesis["status"] = "supported_by_configured_checks"
                    return finish("verified", reason)
            except (DoctorError, UnicodeError) as exc:
                attempt.update(status="rejected", reason=str(exc))
        if report["attempts"]:
            return finish("rejected", "No candidate passed the configured validation within the repair budget")
        return finish("inconclusive", "No justified patch available; configure a model or supply a narrower reproduction and source mapping")
    except (DoctorError, OSError, UnicodeError) as exc:
        return finish("inconclusive", str(exc))
