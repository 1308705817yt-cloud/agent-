"""0.5.4 remeasurement of exactly the prior frozen plans and acceptance cases."""
from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

from agentdoctor.benchmark import _heldout, _suite_pass, score_diagnosis
from agentdoctor.codex_cli import CodexCLIClient
from agentdoctor.core import DoctorError, digest, read_json, utc_now, write_json
from agentdoctor.engine import run
from agentdoctor.evidence import command_completed, command_passed
from agentdoctor.project import _project_files, snapshot
from agentdoctor.runner import ValidationRunner, execute

ROOT = Path(__file__).resolve().parents[1]


def inventory(root):
    return {name: digest(path.read_bytes()) for name, path in _project_files(root)}


def implementation():
    return {p.name: digest(p.read_bytes()) for p in sorted((ROOT / "agentdoctor").glob("*.py"))}


class RetryProvider:
    """Replay an actual old diagnosis, inject one known-wrong patch, then call Codex.

    calls/usage count only external calls. Every synthetic/replayed response is
    distinguished in provenance and in the self-contained benchmark archive.
    """
    provider = "codex-with-explicit-retry-seed"
    name = "codex-default"

    def __init__(self, diagnosis, edit, metadata):
        self.client = CodexCLIClient(metadata=metadata)
        self.diagnosis, self.edit = diagnosis, edit
        self.metadata = {**metadata, "mode": "archived_diagnosis_then_injected_bad_patch_then_real_codex"}
        self.provenance = []

    @property
    def calls(self):
        return self.client.calls

    @property
    def usage(self):
        return self.client.usage

    @property
    def max_calls(self):
        return self.client.max_calls

    @max_calls.setter
    def max_calls(self, value):
        self.client.max_calls = value

    def complete(self, instruction, context, schema):
        if not self.provenance:
            self.provenance.append("archived_semantic_response")
            return copy.deepcopy(self.diagnosis)
        if len(self.provenance) == 1:
            self.provenance.append("injected_known_wrong_candidate")
            return {"summary": "Injected retry probe: intentionally incomplete change, not a model output",
                    "finding_ids": [f["id"] for f in context["findings"] if f["category"] != "validation_failure"],
                    "edits": [{**self.edit, "reason": "Predeclared faulty seed to exercise validation-feedback retry"}]}
        self.provenance.append("external_codex")
        return self.client.complete(instruction, context, schema)


def retry_cases():
    cases = []
    definitions = [("dependency-v053", "0.5.3", "dependency_contract_fault"),
                   ("dependency-near-budget-v052", "0.5.2", "dependency_contract_fault"),
                   ("fanout-v053", "0.5.3", "async_fanout_fault")]
    suite = read_json(ROOT / "agentdoctor/workflow_data/suite.json")
    for name, version, case_id in definitions:
        archive_path = ROOT / f"benchmarks/codex-workflow-v1-v{version}-2026-10-13.json"
        archive = read_json(archive_path, limit=50_000_000)
        old = next(c for c in archive["cases"] if c["case_id"] == case_id)
        spec = copy.deepcopy(next(c for c in suite["cases"] if c["id"] == case_id))
        edit = ({"path": "workflow/codec.py", "before": 'return {"quantity": str(quantity)}',
                 "after": 'return {"quantity": quantity + 1}'} if case_id.startswith("dependency") else
                {"path": "workflow/graph.py", "before": 'return list({row["item"]: row for row in rows}.values())',
                 "after": "return rows[:-1]"})
        spec.update(id=name, source=ROOT / "agentdoctor/workflow_data/cases" / case_id, edit=edit,
                    traces=old["traces"], semantic=old["engine"]["model_events"][0]["output"],
                    provenance={"archive": str(archive_path.relative_to(ROOT)), "sha256": digest(archive_path.read_bytes()),
                                "case": case_id, "diagnosis": "replayed", "first_candidate": "injected"})
        cases.append(spec)
    return cases


def run_group(group, output):
    output.mkdir(parents=True, exist_ok=True)
    destination = output / (group + "-" + uuid.uuid4().hex[:12])
    destination.mkdir()
    imp = implementation()
    assert digest(json.dumps(imp, sort_keys=True)) == read_json(ROOT / "artifacts/v054-frozen-implementation.json")["implementation_hash"]
    prior_names = {"retry": "codex-retry-v0.5.3-2026-10-13.json",
                   "long-feedback": "codex-retry-long-feedback-v0.5.3-2026-10-13.json",
                   "public": "codex-public-smolagents-v0.5.3-2026-10-13.json"}
    prior = read_json(ROOT / "benchmarks" / prior_names[group], limit=50_000_000)
    cases = copy.deepcopy(prior["raw_report"]["plan"]["cases"])
    for case in cases:
        case["source"] = Path(case["source"])
        assert inventory(case["source"]) == prior["raw_report"]["plan"]["project_manifests"][case["id"]]
    is_retry = group != "public"
    plan = {"started_at": utc_now(), "group": group, "prior_archive": prior_names[group], "prior_archive_sha256": digest((ROOT / "benchmarks" / prior_names[group]).read_bytes()), "implementation_manifest": imp,
            "harness_sha256": digest(Path(__file__).read_bytes()),
            "historical_archives": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in (ROOT / "benchmarks").glob("*.json")},
            "cases": [{k: str(v) if isinstance(v, Path) else v for k, v in c.items()} for c in cases],
            "project_manifests": {c["id"]: inventory(c["source"]) for c in cases}}
    write_json(destination / "plan.json", plan)
    client = CodexCLIClient()
    metadata = client.preflight()
    result = {"group": group, "agentdoctor_version": "0.5.4", "plan": plan, "provider_metadata": metadata,
              "model": "codex-default; exact revision unpinned", "cases": [], "complete": False,
              "limitations": ["One run per case, no post-result tuning or retry replacement.",
                              "Public issue tests are component-level and may be present in model training.",
                              "Injected retry seeds and replayed diagnoses are not external model results."]}
    runner = ValidationRunner("local")
    report_path = destination / "report.json"
    write_json(output / ("last-" + group + ".json"), {"report": str(report_path)})
    for case in cases:
        print("START " + case["id"], flush=True)
        area = destination / case["id"]
        project = area / "project"
        snapshot(case["source"], project)
        row = {"id": case["id"], "faulty": bool(case["expected"]), "status": "incomplete",
               "repair_success": False, "diagnosis_valid": False, "candidates": [], "external_calls": 0}
        result["cases"].append(row)
        try:
            baseline = _heldout(project, area / "baseline-oracle", case["heldout"], runner)
            row["heldout_baseline"] = baseline
            assert all(command_completed(c) for c in baseline["commands"]) and not baseline["integrity_changes"]
            assert _suite_pass(baseline, "regression") and _suite_pass(baseline, "target") != row["faulty"], "Invalid fixture baseline"
            trace_path = area / "trace.jsonl"
            if is_retry:
                trace_path.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in case["traces"]), encoding="utf-8")
                model = RetryProvider(case["semantic"], case["edit"], metadata.copy())
            else:
                collected = execute([sys.executable, str(ROOT / "benchmarks/collect_public.py"), str(project), str(trace_path)], project, 30)
                row["trace_collection"] = collected
                assert collected["returncode"] == 0 and not collected["timed_out"], "Trace collection failed"
                model = CodexCLIClient(metadata=metadata.copy())
            engine = run(project, trace_path, mode="repair" if row["faulty"] else "diagnose", output=area / "runs",
                         executor="local", model=model, progress=lambda phase: print(case["id"] + ": " + phase, flush=True))
            row.update(engine=engine, external_calls=model.calls, diagnosis_valid=engine["diagnosis"].get("coverage", {}).get("semantic_checks") != "failed",
                       diagnosis=score_diagnosis(engine["diagnosis"]["findings"], case["expected"]), traces=read_json(Path(engine["workspace"]) / "traces.redacted.json"),
                       budget=read_json(project / "doctor.json")["policy"]["max_context_chars"])
            row["requests"] = []
            for i, event in enumerate(engine["model_events"]):
                path = Path(event["request_file"])
                assert digest(path.read_bytes()) == event["request_sha256"]
                request = read_json(path, limit=20_000_000)
                assert len(json.dumps(request["context"], ensure_ascii=False)) == event["context_chars"] <= row["budget"]
                provenance = model.provenance[i] if is_retry else "external_codex"
                row["requests"].append({"provenance": provenance, "event": event, "request": request, "context_audit": read_json(Path(event["context_audit_file"]), limit=20_000_000)})
            for attempt in engine["attempts"]:
                candidate = {"number": attempt["number"], "origin": "injected_seed" if is_retry and attempt["number"] == 1 else attempt["origin"],
                             "status": attempt["status"], "failure_stage": attempt["failure_stage"], "reason": attempt.get("reason"),
                             "visible": attempt.get("validation"), "heldout_passed": False,
                             "patch": Path(attempt["patch"]).read_text(encoding="utf-8") if attempt.get("patch") else None}
                row["candidates"].append(candidate)
                if attempt.get("validation") and not attempt["validation"]["integrity_changes"]:
                    heldout = _heldout(Path(engine["workspace"]) / f"candidate-{attempt['number']}", area / f"oracle-{attempt['number']}", case["heldout"], runner)
                    candidate.update(heldout=heldout, heldout_passed=_suite_pass(heldout, "target") and _suite_pass(heldout, "regression"))
                    if attempt["status"] == "verified":
                        row["repair_success"] = candidate["heldout_passed"]
            if is_retry:
                first = row["candidates"][0] if row["candidates"] else {}
                row["seed_failed_as_planned"] = first.get("failure_stage") == "visible_validation"
                row["retry_requests"] = [
                    {"chars": r["event"]["context_chars"], "budget": row["budget"],
                     "history_chars": len(json.dumps(r["request"]["context"]["previous_attempts"], ensure_ascii=False)),
                     "history_count": len(r["request"]["context"]["previous_attempts"]),
                     "omitted_history": r["request"]["context"]["omitted_previous_attempts"],
                     "contains_failed_candidate": any(h.get("proposal") == engine["attempts"][0]["proposal"] and h.get("validation") for h in r["request"]["context"]["previous_attempts"])}
                    for r in row["requests"] if r["provenance"] == "external_codex"]
            row["status"] = "measured"
        except (DoctorError, OSError, AssertionError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        write_json(report_path, result)
        print(json.dumps({k: row.get(k) for k in ("id", "status", "repair_success", "external_calls", "retry_requests", "error")}), flush=True)
    result.update(finished_at=utc_now(), complete=all(c["status"] == "measured" and c["diagnosis_valid"] and c["engine"].get("repair_termination") != "model_error" for c in result["cases"]),
                  implementation_unchanged=implementation() == imp)
    assert result["implementation_unchanged"]
    for name, sha in plan["historical_archives"].items():
        assert digest((ROOT / name).read_bytes()) == sha, name
    for case in cases:
        assert inventory(case["source"]) == plan["project_manifests"][case["id"]]
    write_json(report_path, result)
    print("REPORT " + str(report_path), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("group", choices=["retry", "long-feedback", "public"])
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/supplemental-v054")
    args = parser.parse_args()
    run_group(args.group, args.output.resolve())
