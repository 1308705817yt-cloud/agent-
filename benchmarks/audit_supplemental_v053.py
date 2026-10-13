"""Archive all planned denominators and distinguish probes from model results."""
from __future__ import annotations

import json
import re
from pathlib import Path

from agentdoctor.analysis import model_context
from agentdoctor.benchmark import rate, score_diagnosis
from agentdoctor.core import digest, read_json, utc_now, write_json
from agentdoctor.evidence import command_completed, command_passed
from benchmarks.supplemental_eval import ROOT, implementation, inventory


def audit():
    outputs = []
    definitions = [
        ("retry", "artifacts/supplemental-v053", "retry", "codex-retry-v0.5.3-2026-10-13.json"),
        ("long_feedback", "artifacts/supplemental-v053-long-feedback", "retry", "codex-retry-long-feedback-v0.5.3-2026-10-13.json"),
        ("public", "artifacts/supplemental-v053", "public", "codex-public-smolagents-v0.5.3-2026-10-13.json"),
    ]
    for label, folder, group, filename in definitions:
        source = Path(read_json(ROOT / folder / ("last-" + group + ".json"))["report"])
        report = read_json(source, limit=50_000_000)
        assert "finished_at" in report
        plan = report["plan"]
        assert implementation() == plan["implementation_manifest"]
        assert digest((ROOT / "benchmarks/supplemental_eval.py").read_bytes()) == plan["harness_sha256"]
        for name, sha in plan["historical_archives"].items():
            assert digest((ROOT / name).read_bytes()) == sha, name
        cases_by_id = {c["id"]: c for c in plan["cases"]}
        for case_id, manifest in plan["project_manifests"].items():
            assert inventory(Path(cases_by_id[case_id]["source"])) == manifest
        rows, calls, usage = [], 0, {}
        for row in report["cases"]:
            case = cases_by_id[row["id"]]
            engine = row["engine"]
            assert score_diagnosis(engine["diagnosis"]["findings"], case["expected"]) == row["diagnosis"]
            assert row["external_calls"] == sum(r["provenance"] == "external_codex" for r in row["requests"])
            calls += row["external_calls"]
            for item in engine["model"]["usage"]:
                for key, value in item.items():
                    if isinstance(value, (int, float)):
                        usage[key] = usage.get(key, 0) + value
            for request in row["requests"]:
                event, document = request["event"], request["request"]
                assert digest(Path(event["request_file"]).read_bytes()) == event["request_sha256"]
                assert len(json.dumps(document["context"], ensure_ascii=False)) == event["context_chars"] <= row["budget"]
                assert "_heldout.json" not in json.dumps(document, ensure_ascii=False)
                assert "https://github.com/huggingface/smolagents/issues/" not in json.dumps(document, ensure_ascii=False)
            seed = next((c for c in row["candidates"] if c["origin"] == "injected_seed"), None)
            if seed:
                assert seed["failure_stage"] == "visible_validation" and all(command_completed(c) for c in seed["visible"]["commands"])
            overflows = []
            for warning in engine["warnings"]:
                match = re.search(r"max_context_chars \((\d+) > (\d+)\)", warning)
                if match:
                    overflows.append({"required_chars": int(match[1]), "budget": int(match[2]), "warning": warning})
            size_only = None
            if overflows:
                job = Path(engine["workspace"])
                config = read_json(job / "config.frozen.json")
                config["policy"]["max_context_chars"] = 500000
                history = ([{"baseline": engine["baseline"]}] if row["faulty"] else [])
                history += [{k: a.get(k) for k in ("summary", "reason", "validation", "proposal")} for a in engine["attempts"]]
                ctx = model_context(job / "snapshot", read_json(job / "manifest.json"), read_json(job / "traces.redacted.json"),
                                    engine["diagnosis"], config, history)
                sizes = {k: len(json.dumps(v, ensure_ascii=False)) for k, v in ctx.items()}
                size_only = {"purpose": "Read-only size inspection, raised in-memory budget only; NOT sent to a model, NOT a rerun or a pass.",
                             "chars_before_normal_budget_trimming": len(json.dumps(ctx, ensure_ascii=False)),
                             "field_sizes": sizes, "dependency_resolved": len(ctx["dependency_context"]["resolved"]),
                             "dependency_unresolved": len(ctx["dependency_context"]["unresolved"])}
            commands = [c for candidate in row["candidates"] for suite in (candidate.get("visible"), candidate.get("heldout")) if suite for c in suite["commands"]]
            rows.append({"id": row["id"], "faulty": row["faulty"], "diagnosis_valid": row["diagnosis_valid"],
                         "root_diagnosis_success": row["diagnosis_valid"] and row["diagnosis"]["root_cause_correct"],
                         "repair_success": row["repair_success"], "calls": row["external_calls"],
                         "engine_status": engine["status"], "repair_termination": engine["repair_termination"],
                         "seed_failed_as_planned": row.get("seed_failed_as_planned"), "retry_requests": row.get("retry_requests"),
                         "candidate_count": len(row["candidates"]), "candidate_commands": len(commands),
                         "candidate_commands_completed": sum(command_completed(c) for c in commands),
                         "candidate_commands_passed": sum(command_passed(c) for c in commands),
                         "overflows": overflows, "size_only_inspection": size_only,
                         "model_request_chars": [r["event"]["context_chars"] for r in row["requests"] if r["provenance"] == "external_codex"]})
        faulty = [r for r in rows if r["faulty"]]
        summary = {"group": label, "planned_cases": len(rows), "valid_diagnoses": sum(r["diagnosis_valid"] for r in rows),
                   "root_success_over_all_planned": rate(sum(r["root_diagnosis_success"] for r in rows), len(rows)),
                   "root_success_over_planned_faults": rate(sum(r["root_diagnosis_success"] for r in faulty), len(faulty)),
                   "repair_success_over_planned_faults": rate(sum(r["repair_success"] for r in faulty), len(faulty)),
                   "external_calls": calls, "usage": usage, "context_blocked_cases": sum(bool(r["overflows"]) for r in rows),
                   "all_required_model_stages_completed": all(r["diagnosis_valid"] and not r["overflows"] for r in rows),
                   "retry_diagnoses_are_replays": label != "public", "cases": rows}
        archive = ROOT / "benchmarks" / filename
        assert not archive.exists(), "Refuse to overwrite an archived measurement"
        write_json(archive, {"summary": summary, "source_report_sha256": digest(source.read_bytes()), "source_report": str(source),
                             "raw_report": report, "harness_source": (ROOT / "benchmarks/supplemental_eval.py").read_text(encoding="utf-8"),
                             "raw_complete_semantics": "All cases traversed by the harness. Use summary.all_required_model_stages_completed for context/model-stage completion."})
        outputs.append({"archive": str(archive.relative_to(ROOT)), **summary})
    lock = read_json(ROOT / "benchmarks/public-smolagents-v1.lock.json")
    assert inventory(ROOT / "benchmarks/public-smolagents-v1") == lock["files"]
    result = {"created_at": utc_now(), "implementation_unchanged": True, "historical_archives_unchanged": True,
              "public_dataset_hash": lock["dataset_hash"], "external_calls_total": sum(r["external_calls"] for r in outputs),
              "groups": outputs, "scope": "One selected third-party agent-framework component, two public bugs and two controls; not an unbiased or training-unseen sample."}
    target = ROOT / "benchmarks/codex-supplemental-v0.5.3-2026-10-13-audit.json"
    assert not target.exists()
    write_json(target, result)
    print(json.dumps({"audit": str(target), "external_calls": result["external_calls_total"],
                      "groups": [{k: g[k] for k in ("group", "planned_cases", "valid_diagnoses", "root_success_over_all_planned", "repair_success_over_planned_faults", "context_blocked_cases")} for g in outputs]}, ensure_ascii=True))


if __name__ == "__main__":
    audit()
