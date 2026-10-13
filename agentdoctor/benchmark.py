"""Versioned independent fixtures, held-out verification, and explicit denominators."""
from __future__ import annotations

import csv
import json
import sys
import uuid
from pathlib import Path

from . import __version__
from .core import DoctorError, digest, read_json, utc_now, write_json
from .engine import run
from .evidence import command_completed, command_passed
from .project import snapshot
from .runner import ValidationRunner, execute
from .taxonomy import SYMPTOMS


# Symptom-level failures are not accepted as a correct fault-family diagnosis.
# Unknown labels are retained as wrong predictions rather than silently dropped.

REPAIR_OUTCOMES = ("no_patch_generated", "policy_rejected", "visible_test_failed", "independent_acceptance_failed",
                   "repaired", "model_error", "environment_error", "not_evaluated")


def candidate_outcome(candidate: dict) -> str:
    if candidate.get("failure_stage") == "policy":
        return "policy_rejected"
    visible = candidate.get("visible_outcomes", {})
    if "failed" in visible.values():
        return "visible_test_failed"
    if candidate.get("failure_stage") == "validation_error" or not visible or any(v != "passed" for v in visible.values()):
        return "environment_error"
    independent = candidate.get("heldout_outcomes", {})
    if "failed" in independent.values():
        return "independent_acceptance_failed"
    if candidate.get("heldout_passed") and candidate.get("status") == "verified":
        return "repaired"
    return "environment_error"


def repair_outcome(row: dict) -> str:
    if not row["faulty"]:
        return "not_applicable"
    if row.get("repair_success"):
        return "repaired"
    if row.get("repair_termination") == "model_error":
        return "model_error"
    if row.get("repair_status") == "incomplete":
        return "environment_error" if row.get("engine_status") else "not_evaluated"
    candidates = row.get("candidates", [])
    if not candidates:
        return "no_patch_generated" if row.get("repair_termination") == "no_patch" else "not_evaluated"
    outcomes = {candidate_outcome(c) for c in candidates}
    # Case totals are exclusive: report the furthest gate reached across retries.
    for outcome in ("independent_acceptance_failed", "visible_test_failed", "policy_rejected"):
        if outcome in outcomes:
            return outcome
    return "environment_error"


def repair_breakdown(rows: list[dict]) -> dict:
    faulty = [r for r in rows if r["faulty"]]
    candidates = [c for r in faulty for c in r.get("candidates", [])]
    return {"faulty_cases": len(faulty),
            "case_outcomes": {key: sum(repair_outcome(r) == key for r in faulty) for key in REPAIR_OUTCOMES},
            "candidate_attempts": len(candidates),
            "candidate_outcomes": {key: sum(candidate_outcome(c) == key for c in candidates) for key in REPAIR_OUTCOMES if key != "no_patch_generated"},
            "candidate_origins": {key: sum(c.get("origin", "unknown") == key for c in candidates) for key in ("model", "declared_alias", "unknown")}}


def score_diagnosis(findings: list[dict], expected: list[dict]) -> dict:
    predicted = sorted({f["category"] for f in findings if f["category"] not in SYMPTOMS})
    expected_categories = sorted({item["category"] for item in expected})
    localized = all(any(f["category"] == item["category"] and f.get("source", {}).get("path") == item["path"] for f in findings) for item in expected)
    # Keep the historical observation-location metric unchanged. Root-cause
    # localization is an additive metric against the very same frozen labels.
    root_localized = all(any(f["category"] == item["category"] and any(
        c.get("source", {}).get("path") == item["path"] for c in f.get("root_causes", []))
        for f in findings) for item in expected)
    predicted_roots = {(f["category"], c.get("source", {}).get("path")) for f in findings
                       if f["category"] not in SYMPTOMS for c in f.get("root_causes", [])}
    expected_roots = {(item["category"], item["path"]) for item in expected}
    correct = predicted == expected_categories and localized
    positive, alarm = bool(expected), bool(predicted)
    return {"predicted_categories": predicted, "expected_categories": expected_categories,
            "correct": correct, "localized": localized if positive else None,
            "root_cause_localized": root_localized if positive else None,
            "root_cause_correct": predicted == expected_categories and predicted_roots == expected_roots,
            "binary_outcome": "tp" if positive and alarm else "fn" if positive else "fp" if alarm else "tn"}


def rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator, "value": numerator / denominator if denominator else None}


def summarize(rows: list[dict]) -> dict:
    valid = [r for r in rows if r["status"] == "measured"]
    diagnosed = [r for r in rows if r.get("diagnosis_status", r["status"]) == "measured"]
    faulty = [r for r in rows if r["faulty"] and r.get("repair_status", r["status"]) == "measured"]
    # Attempts and deliveries are facts, including when later infrastructure
    # prevents assessment. Never erase them by filtering the entire case row.
    candidate_rows = [c for r in rows for c in r.get("candidates", [])]
    regressions = [c for c in candidate_rows if c["regression_evaluated"]]
    delivered = [r for r in rows if r["faulty"] and r.get("delivered")]
    assessed_deliveries = [r for r in delivered if r.get("delivery_evaluated", r["status"] == "measured")]
    confusion = {key: sum(r["diagnosis"]["binary_outcome"] == key for r in diagnosed) for key in ("tp", "tn", "fp", "fn")}
    return {
        "scheduled_cases": len(rows), "measured_cases": len(valid), "invalid_or_incomplete_cases": len(rows) - len(valid),
        "diagnosed_cases": len(diagnosed), "repair_measured_cases": len(faulty),
        "diagnosis_accuracy": rate(sum(r["diagnosis"]["correct"] for r in diagnosed), len(diagnosed)),
        "root_cause_diagnosis_accuracy": rate(sum(r["diagnosis"]["root_cause_correct"] for r in diagnosed if "root_cause_correct" in r["diagnosis"]),
                                             sum("root_cause_correct" in r["diagnosis"] for r in diagnosed)),
        "binary_detection_accuracy": rate(confusion["tp"] + confusion["tn"], len(diagnosed)),
        "detection_precision": rate(confusion["tp"], confusion["tp"] + confusion["fp"]),
        "detection_recall": rate(confusion["tp"], confusion["tp"] + confusion["fn"]),
        "repair_success_rate": rate(sum(r.get("repair_success", False) for r in faulty), len(faulty)),
        "candidate_regression_failure_rate": rate(sum(c["regression_failed"] for c in regressions), len(regressions)),
        "candidate_count": len(candidate_rows),
        "incomplete_candidates": sum(c.get("assessment_status") == "incomplete" for c in candidate_rows),
        "delivered_count": len(delivered), "unassessed_deliveries": len(delivered) - len(assessed_deliveries),
        "false_verified_rate": rate(sum(not r.get("repair_success", False) for r in assessed_deliveries), len(assessed_deliveries)),
        "confusion": confusion,
    }


def _suite_pass(suite: dict, kind: str) -> bool:
    rows = [r for r in suite["commands"] if r["kind"] == kind]
    return bool(rows) and not suite["integrity_changes"] and all(command_passed(r) for r in rows)


def _infrastructure_ok(suite: dict) -> bool:
    # Strict readiness check for the *baseline*. Candidate budget exhaustion and
    # integrity violations are outcomes, classified separately below.
    return not suite["integrity_changes"] and len(suite["commands"]) == 2 and all(not r["error"] and not r["timed_out"] and r["returncode"] is not None for r in suite["commands"])


def _candidate_outcome(suite: dict, kind: str) -> str:
    if suite["integrity_changes"]:
        return "failed"
    rows = [r for r in suite["commands"] if r["kind"] == kind]
    if not rows:
        return "not_run"
    outcomes = []
    for row in rows:
        if row["timed_out"]:
            outcomes.append("failed")
        elif row["error"] or row["returncode"] is None:
            outcomes.append("environment_error")
        else:
            outcomes.append("passed" if command_passed(row) else "failed")
    # A known failed check is sufficient to reject a candidate, even if another
    # check is unavailable. Missing results after process exit are failures too.
    return "failed" if "failed" in outcomes else "environment_error" if "environment_error" in outcomes else "passed"


def _heldout(project: Path, destination: Path, spec: dict, runner: ValidationRunner) -> dict:
    manifest = snapshot(project, destination)
    oracle = Path(__file__).with_name("benchmark_oracle.py").read_bytes()
    (destination / "_heldout_runner.py").write_bytes(oracle)
    write_json(destination / "_heldout.json", spec)
    manifest.update({p: digest((destination / p).read_bytes()) for p in ("_heldout_runner.py", "_heldout.json")})
    config = {"validation": {"repetitions": 1, "docker_image": "python:3.11-slim", "commands": [
        {"name": "heldout_" + kind, "kind": kind, "argv": ["{python}", "_heldout_runner.py", kind], "timeout_seconds": 15,
         "expected_cases": [f"{kind}:{index}" for index in range(1, len(spec[kind]) + 1)]}
        for kind in ("target", "regression")
    ]}}
    return runner.run_suite(destination, config, manifest)


def run_benchmark(output: Path, *, provider: str = "none", model_name: str | None = None,
                  executor: str = "local", model_factory=None, repetitions: int = 1, progress=None,
                  dataset_lock: Path | None = None, provider_metadata: dict | None = None,
                  suite_name: str = "independent-v2") -> dict:
    if not 1 <= repetitions <= 20:
        raise DoctorError("Benchmark repetitions must be between 1 and 20")
    suites = {"independent-v2": "benchmark_data", "workflow-v1": "workflow_data"}
    if suite_name not in suites:
        raise DoctorError("Unknown benchmark suite")
    source_data = Path(__file__).with_name(suites[suite_name])
    job = output.resolve() / ("benchmark-" + uuid.uuid4().hex[:12])
    job.mkdir(parents=True, exist_ok=False)
    data = job / "dataset"
    frozen_files = snapshot(source_data, data)
    dataset_hash = digest(json.dumps(frozen_files, sort_keys=True))
    suite = read_json(data / "suite.json")
    if dataset_lock:
        lock = read_json(Path(dataset_lock))
        if lock.get("files") != frozen_files or lock.get("dataset_hash") != dataset_hash:
            raise DoctorError("Dataset differs from the fixed lock; no model calls were scheduled")
    write_json(job / "dataset-manifest.json", {"suite_version": suite["version"], "dataset_hash": dataset_hash, "files": frozen_files})
    report = {"schema_version": 2, "suite_version": suite["version"], "agentdoctor_version": __version__,
              "started_at": utc_now(), "provider": provider, "model": model_name, "executor": executor,
              "repetitions": repetitions, "workspace": str(job), "cases": [], "complete": False,
              "planned_cases": len(suite["cases"]) * repetitions,
              "limitations": ["Synthetic integration cases, not a production accuracy estimate.",
                              "Prompt cases use a deterministic response simulator, not a live target LLM.",
                              "Labels and held-out cases are never passed to the repair model.",
                              "A low candidate regression rate is not evidence of broad repair coverage.",
                              "Completion evidence assumes trusted evaluators, not hostile in-process report forgery."]}
    report["dataset_hash"] = dataset_hash
    report["dataset_lock_verified"] = dataset_lock is not None
    report["repair_strategy"] = "declared_alias_then_model"
    report["provider_metadata"] = provider_metadata or {}
    implementation = {p.name: digest(p.read_bytes()) for p in sorted(Path(__file__).parent.glob("*.py"))}
    report["implementation_hash"] = digest(json.dumps(implementation, sort_keys=True))
    (job / "implementation").mkdir()
    for name in implementation:
        (job / "implementation" / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    write_json(job / "implementation-manifest.json", implementation)
    runner = ValidationRunner(executor)
    ready, reason = runner.available()
    if not ready:
        raise DoctorError(reason)

    def save():
        for row in report["cases"]:
            row["repair_outcome"] = repair_outcome(row)
            for candidate in row["candidates"]:
                candidate["outcome"] = candidate_outcome(candidate)
        report["metrics"] = summarize(report["cases"])
        report["repair_breakdown"] = repair_breakdown(report["cases"])
        families = sorted({r["family"] for r in report["cases"]})
        report["by_family"] = {family: summarize([r for r in report["cases"] if r["family"] == family]) for family in families}
        write_json(job / "benchmark.json", report)

    for repetition in range(1, repetitions + 1):
        for case in suite["cases"]:
            if progress:
                progress(f"benchmark {case['id']} ({repetition}/{repetitions})")
            case_dir = job / f"{case['id']}-{repetition}"
            project = case_dir / "project"
            snapshot(data / "cases" / case["id"], project)
            row = {"case_id": case["id"], "family": case["family"], "faulty": bool(case["expected"]),
                   "repetition": repetition, "status": "incomplete", "diagnosis_status": "incomplete",
                   "repair_status": "incomplete" if case["expected"] else "not_applicable",
                   "candidates": [], "repair_success": False, "delivery_evaluated": False}
            report["cases"].append(row)
            try:
                traces = case_dir / "trace.jsonl"
                collected = execute([sys.executable, "-m", "agentdoctor.benchmark_collect", str(project), str(traces)], project, 15)
                if collected["returncode"] != 0 or collected["timed_out"]:
                    raise DoctorError("Fixture trace collection failed: " + collected["stderr"])
                baseline = _heldout(project, case_dir / "oracle-baseline", case["heldout"], runner)
                row["heldout_baseline"] = baseline
                if not _infrastructure_ok(baseline) or not all(command_completed(r) for r in baseline["commands"]) or not _suite_pass(baseline, "regression") or _suite_pass(baseline, "target") == row["faulty"]:
                    raise DoctorError("Fixture does not satisfy its independently checked baseline contract")
                # Every sample gets a fresh model client and the same budget.
                model = model_factory() if model_factory else None
                result = run(project, traces, mode="repair" if row["faulty"] else "diagnose", output=case_dir / "runs", executor=executor, model=model,
                             progress=(lambda phase: progress(f"{case['id']}: {phase}")) if progress else None)
                row.update(diagnosis_status="measured", repair_status="measured" if row["faulty"] else "not_applicable",
                           diagnosis=score_diagnosis(result["diagnosis"]["findings"], case["expected"]),
                           engine_status=result["status"], engine_report=str(Path(result["workspace"]) / "report.json"),
                           delivered=result["status"] == "verified", model=result["model"],
                           repair_termination=result.get("repair_termination", "unknown"),
                           model_events=result.get("model_events", []))
                if row["repair_termination"] == "model_error":
                    row["repair_status"] = "incomplete"
                # Semantic API failure is incomplete measurement, not a clean bill of health.
                if model and result["diagnosis"].get("coverage", {}).get("semantic_checks") == "failed":
                    row.update(diagnosis_status="incomplete", error="Semantic analysis failed; inspect the engine report")
                for attempt in result["attempts"]:
                    candidate = {"number": attempt["number"], "status": attempt["status"], "regression_evaluated": False,
                                 "regression_failed": False, "heldout_passed": False, "heldout_evaluated": False,
                                 "assessment_status": "not_run", "origin": attempt.get("origin", "unknown"),
                                 "failure_stage": attempt.get("failure_stage"), "reason": attempt.get("reason")}
                    row["candidates"].append(candidate)
                    validation = attempt.get("validation")
                    if validation:
                        candidate["visible_outcomes"] = {kind: _candidate_outcome(validation, kind) for kind in ("target", "regression")}
                        development_regression = _candidate_outcome(validation, "regression")
                        candidate["regression_evaluated"] = development_regression == "failed"
                        candidate["regression_failed"] = development_regression == "failed"
                        candidate["assessment_status"] = "measured"
                        if not validation["integrity_changes"]:
                            try:
                                independent = _heldout(Path(result["workspace"]) / f"candidate-{attempt['number']}", case_dir / f"oracle-candidate-{attempt['number']}", case["heldout"], runner)
                                candidate["heldout"] = independent
                                outcomes = {kind: _candidate_outcome(independent, kind) for kind in ("target", "regression")}
                            except (DoctorError, OSError) as exc:
                                outcomes = {kind: "environment_error" for kind in ("target", "regression")}
                                candidate["assessment_error"] = str(exc)
                            candidate["heldout_outcomes"] = outcomes
                            candidate["regression_evaluated"] |= outcomes["regression"] in ("passed", "failed")
                            candidate["regression_failed"] |= outcomes["regression"] == "failed"
                            candidate["heldout_passed"] = all(value == "passed" for value in outcomes.values())
                            candidate["heldout_evaluated"] = candidate["heldout_passed"] or "failed" in outcomes.values()
                            if any(value in ("environment_error", "not_run") for value in outcomes.values()):
                                candidate["assessment_status"] = "incomplete"
                                candidate.setdefault("assessment_error", "Independent checks unavailable; inspect heldout_outcomes")
                            if not candidate["heldout_evaluated"]:
                                row["repair_status"] = "incomplete"
                            if attempt["status"] == "verified":
                                row["delivery_evaluated"] = candidate["heldout_evaluated"]
                                row["repair_status"] = "measured" if candidate["heldout_evaluated"] else "incomplete"
                            if attempt["status"] == "verified" and candidate["heldout_passed"]:
                                row["repair_success"] = True
                if row["diagnosis_status"] == "measured" and row["repair_status"] != "incomplete" and not any(c["assessment_status"] == "incomplete" for c in row["candidates"]):
                    row["status"] = "measured"
            except (DoctorError, OSError) as exc:
                row.update(status="incomplete", error=str(exc))
            save()
    report.update(finished_at=utc_now(), complete=all(r["status"] == "measured" for r in report["cases"]))
    report["implementation_unchanged"] = implementation == {p.name: digest(p.read_bytes()) for p in sorted(Path(__file__).parent.glob("*.py"))}
    if not report["implementation_unchanged"]:
        report["complete"] = False
        report["limitations"].append("Implementation changed during measurement; results are not one frozen implementation.")
    save()
    with (job / "cases.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["case_id", "family", "repetition", "status", "diagnosis_status", "repair_status", "faulty", "diagnosis_correct", "engine_status", "repair_success", "repair_outcome", "delivery_evaluated", "candidate_count"])
        writer.writeheader()
        for row in report["cases"]:
            writer.writerow({**{k: row.get(k) for k in writer.fieldnames if k in row}, "diagnosis_correct": row.get("diagnosis", {}).get("correct"), "candidate_count": len(row["candidates"])})
    with (job / "candidates.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["case_id", "repetition", "number", "origin", "status", "outcome", "reason", "heldout_passed"])
        writer.writeheader()
        for row in report["cases"]:
            for candidate in row["candidates"]:
                writer.writerow({"case_id": row["case_id"], "repetition": row["repetition"], **{key: candidate.get(key) for key in writer.fieldnames if key not in ("case_id", "repetition")}})
    write_json(output / "last-benchmark.json", {"report": str(job / "benchmark.json"), "cases": str(job / "cases.csv"), "complete": report["complete"]})
    return report
