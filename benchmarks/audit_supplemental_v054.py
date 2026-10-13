"""Audit frozen-plan equivalence, compact views, requests and independent checks."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from agentdoctor.benchmark import rate, score_diagnosis
from agentdoctor.context_view import history_view
from agentdoctor.core import digest, read_json, utc_now, write_json
from agentdoctor.evidence import command_completed, command_passed
from benchmarks.supplemental_eval_v054 import ROOT, implementation, inventory


def verify_dependencies(compact, raw):
    resolved = Counter((g["from_path"], r["name"], g["path"], *bounds)
                       for g in compact["resolved"] for r in g["references"] for bounds in r["ranges"])
    unresolved = Counter((g["path"], r["name"], line, g["reason"])
                         for g in compact["unresolved"] for r in g["references"] for line in r["lines"])
    assert resolved == Counter((r["from_path"], r["reference"], r["path"], r["start_line"], r["end_line"]) for r in raw["resolved"])
    assert unresolved == Counter((r["path"], r["reference"], r["line"], r["reason"]) for r in raw["unresolved"])
    assert compact["record_counts"] == {key: len(raw[key]) for key in ("resolved", "unresolved")}


def candidate_outcome(candidate):
    if candidate["failure_stage"] == "policy":
        return "policy_rejected"
    visible = candidate.get("visible")
    if not visible:
        return "unassessed"
    if visible["integrity_changes"] or not all(command_passed(c) for c in visible["commands"]):
        return "visible_test_failed"
    if candidate.get("heldout_passed"):
        return "repaired"
    return "independent_acceptance_failed" if candidate.get("heldout") else "unassessed"


def audit():
    old = read_json(ROOT / "artifacts/v054-preflight.json")
    for filename, sha in old["archives"].items():
        assert digest((ROOT / filename).read_bytes()) == sha, filename
    output = {"created_at": utc_now(), "agentdoctor_version": "0.5.4", "historical_archives_unchanged": True,
              "scope": "Same eight supplemental scenarios, budgets, seed proposals, archived diagnoses and oracle inputs as v0.5.3; no post-result tuning.",
              "groups": []}
    for group, archive_name in (("retry", "codex-retry-v0.5.4-2026-10-13.json"),
                                ("long-feedback", "codex-retry-long-feedback-v0.5.4-2026-10-13.json"),
                                ("public", "codex-public-smolagents-v0.5.4-2026-10-13.json")):
        source = Path(read_json(ROOT / "artifacts/supplemental-v054" / ("last-" + group + ".json"))["report"])
        report = read_json(source, limit=50_000_000)
        plan = report["plan"]
        prior_path = ROOT / "benchmarks" / plan["prior_archive"]
        prior = read_json(prior_path, limit=50_000_000)["raw_report"]
        assert digest(prior_path.read_bytes()) == plan["prior_archive_sha256"]
        assert plan["cases"] == prior["plan"]["cases"], "Frozen evaluation plan changed"
        assert plan["project_manifests"] == prior["plan"]["project_manifests"]
        assert implementation() == plan["implementation_manifest"]
        assert digest((ROOT / "benchmarks/supplemental_eval_v054.py").read_bytes()) == plan["harness_sha256"]
        for case in plan["cases"]:
            assert inventory(Path(case["source"])) == plan["project_manifests"][case["id"]]
        assert "finished_at" in report
        cases = {c["id"]: c for c in plan["cases"]}
        calls, rows, usage = 0, [], {}
        for row in report["cases"]:
            engine = row["engine"]
            assert score_diagnosis(engine["diagnosis"]["findings"], cases[row["id"]]["expected"]) == row["diagnosis"]
            calls += row["external_calls"]
            assert row["external_calls"] == sum(r["provenance"] == "external_codex" for r in row["requests"])
            for u in engine["model"]["usage"]:
                for key, value in u.items():
                    if isinstance(value, (int, float)):
                        usage[key] = usage.get(key, 0) + value
            context_sizes = []
            for request in row["requests"]:
                event = request["event"]
                context, full = request["request"]["context"], request["context_audit"]
                assert digest(Path(event["request_file"]).read_bytes()) == event["request_sha256"]
                assert digest(Path(event["context_audit_file"]).read_bytes()) == event["context_audit_sha256"]
                assert read_json(Path(event["context_audit_file"]), limit=20_000_000) == full
                assert len(json.dumps(context, ensure_ascii=False)) == event["context_chars"] <= row["budget"]
                assert "_heldout.json" not in json.dumps(request["request"], ensure_ascii=False)
                assert "https://github.com/huggingface/smolagents/issues/" not in json.dumps(request["request"], ensure_ascii=False)
                verify_dependencies(context["dependency_context"], full["dependency_context"])
                count = len(context["previous_attempts"])
                assert context["previous_attempts"] == history_view(full["previous_attempts"][-count:] if count else [])
                context_sizes.append({"operation": event["operation"], "provenance": request["provenance"], "chars": event["context_chars"],
                                      "budget": row["budget"], "dependency_chars": len(json.dumps(context["dependency_context"], ensure_ascii=False)),
                                      "history_chars": len(json.dumps(context["previous_attempts"], ensure_ascii=False)),
                                      "omitted_history": context["omitted_previous_attempts"]})
            context_audits = Path(engine["workspace"]) / "model-context-audits"
            row["all_context_audits"] = {p.name: {"sha256": digest(p.read_bytes()), "data": read_json(p, limit=20_000_000)} for p in context_audits.glob("*.json")}
            candidate_stats = []
            for candidate in row["candidates"]:
                commands = [c for s in (candidate.get("visible"), candidate.get("heldout")) if s for c in s["commands"]]
                candidate_stats.append({"number": candidate["number"], "origin": candidate["origin"], "outcome": candidate_outcome(candidate),
                                        "commands": len(commands), "completed": sum(command_completed(c) for c in commands),
                                        "passed": sum(command_passed(c) for c in commands)})
                if candidate["origin"] == "injected_seed":
                    assert candidate["failure_stage"] == "visible_validation"
                    assert all(command_completed(c) for c in candidate["visible"]["commands"])
            overflows = [w for w in engine["warnings"] if re.search(r"max_context_chars \(\d+ > \d+\)", w)]
            rows.append({"id": row["id"], "faulty": row["faulty"], "diagnosis_valid": row["diagnosis_valid"],
                         "root_correct": row["diagnosis_valid"] and row["diagnosis"]["root_cause_correct"],
                         "repair_success": row["repair_success"], "engine_status": engine["status"],
                         "repair_termination": engine["repair_termination"], "warnings": engine["warnings"],
                         "context_blocked": bool(overflows), "calls": row["external_calls"],
                         "empty_patch_responses": sum(r["provenance"] == "external_codex" and r["event"]["operation"] == "patch_proposal"
                                                      and r["event"].get("output", {}).get("edits") == [] for r in row["requests"]),
                         "request_sizes": context_sizes, "candidate_results": candidate_stats})
        faults = [r for r in rows if r["faulty"]]
        controls = [r for r in rows if not r["faulty"]]
        model_candidates = [c for r in rows for c in r["candidate_results"] if c["origin"] == "model"]
        summary = {"group": group, "archive": archive_name, "implementation_hash": digest(json.dumps(implementation(), sort_keys=True)),
                   "complete": report["complete"], "cases_and_acceptance_unchanged": True,
                   "planned_cases": len(rows), "diagnosis_valid": sum(r["diagnosis_valid"] for r in rows),
                   "diagnoses_are_archived_replays": group != "public",
                   "root_success_over_all_planned": rate(sum(r["root_correct"] for r in rows), len(rows)),
                   "root_success_over_planned_faults": rate(sum(r["root_correct"] for r in faults), len(faults)),
                   "controls_passed": rate(sum(r["root_correct"] for r in controls), len(controls)),
                   "repair_success_over_planned_faults": rate(sum(r["repair_success"] for r in faults), len(faults)),
                   "context_blocked_cases": sum(r["context_blocked"] for r in rows), "external_calls": calls, "usage": usage,
                   "model_candidate_outcomes": dict(Counter(c["outcome"] for c in model_candidates)),
                   "no_patch_generated_cases": sum(r["repair_termination"] == "no_patch" and not r["candidate_results"] for r in faults),
                   "empty_patch_responses": sum(r["empty_patch_responses"] for r in rows),
                   "failed_case_outcomes": dict(Counter(
                       next((kind for kind in ("independent_acceptance_failed", "visible_test_failed", "policy_rejected")
                             if any(c["outcome"] == kind for c in r["candidate_results"])),
                            "context_or_model_error" if r["repair_termination"] == "model_error" else "no_patch_generated")
                       for r in faults if not r["repair_success"])),
                   "false_verified": sum(r["engine_status"] == "verified" and not r["repair_success"] for r in faults),
                   "cases": rows}
        target = ROOT / "benchmarks" / archive_name
        assert not target.exists()
        write_json(target, {"summary": summary, "source_report": str(source), "source_report_sha256": digest(source.read_bytes()),
                            "raw_report": report, "harness_source": (ROOT / "benchmarks/supplemental_eval_v054.py").read_text(encoding="utf-8")})
        output["groups"].append(summary)
    output["external_calls_total"] = sum(g["external_calls"] for g in output["groups"])
    target = ROOT / "benchmarks/codex-supplemental-v0.5.4-2026-10-13-audit.json"
    assert not target.exists()
    write_json(target, output)
    print(json.dumps({"audit": str(target), "external_calls": output["external_calls_total"],
                      "groups": [{k: g[k] for k in ("group", "complete", "root_success_over_all_planned", "repair_success_over_planned_faults", "model_candidate_outcomes", "context_blocked_cases")} for g in output["groups"]]}, ensure_ascii=True))


if __name__ == "__main__":
    audit()
