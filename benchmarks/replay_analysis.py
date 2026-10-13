"""Replay archived semantic outputs unchanged; this does not measure a new model."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from agentdoctor import __version__
from agentdoctor.analysis import add_model_analysis
from agentdoctor.core import DoctorError, digest, read_json, utc_now, write_json
from agentdoctor.taxonomy import SYMPTOMS


def replay_row(row: dict) -> dict:
    requests = [r for r in row["model_requests"] if r["operation"] == "semantic_analysis"]
    events = [e for e in row["model_events"] if e["operation"] == "semantic_analysis"]
    if len(requests) != 1 or len(events) != 1 or events[0]["status"] != "completed":
        raise DoctorError("Replay requires exactly one completed, archived semantic response per case")
    request, event = requests[0], events[0]
    context = request["context"]
    # Reconstruct the pre-model diagnosis, not the final enriched report.
    diagnosis = {k: copy.deepcopy(context[k]) for k in
                 ("findings", "hypotheses", "observations", "trace_comparisons")}
    diagnosis["coverage"] = {}
    sources = context["sources"]
    manifest = {s["path"]: s["sha256"] for s in sources}
    # This portable replay trusts only the archived, supplied sources. Do not
    # invent hashes for files whose original manifests are not in the archive.
    if any(h.get("source_path") and h["source_path"] not in manifest for h in event["output"]["hypotheses"]):
        raise DoctorError("Replay needs the original manifest for unsupplied hypothesis files")
    result = {"case_id": row["case_id"], "historical_validation": event["semantic_validation"],
              "original_request_sha256": event["request_sha256"],
              "response_content_sha256": digest(json.dumps(event["output"], sort_keys=True, ensure_ascii=False)),
              "response_modified": False, "validation": "accepted"}
    try:
        result["decisions"] = add_model_analysis(diagnosis, copy.deepcopy(event["output"]), row["traces"], manifest,
                                                context["reference_spans"], {e["id"] for e in context["evidence_catalog"]}, sources)
    except DoctorError as exc:
        result.update(validation="rejected", error=str(exc))
    result["fault_categories"] = sorted({f["category"] for f in diagnosis["findings"] if f["category"] not in SYMPTOMS})
    result["unclassified_symptoms"] = [{"id": f["id"], "category": f["category"], "has_root_cause": bool(f.get("root_causes"))}
                                        for f in diagnosis["findings"] if f["category"] in SYMPTOMS and not f.get("classified_by")]
    result["citations"] = [{"finding_id": f["id"], "canonical": c["source_evidence"], "audit": c["citation"]}
                           for f in diagnosis["findings"] for c in f.get("root_causes", [])]
    return result


def replay_archives(paths: list[Path]) -> dict:
    report = {"kind": "offline_semantic_response_replay", "version": __version__, "created_at": utc_now(),
              "external_model_calls": 0, "is_new_model_accuracy_measurement": False,
              "limitations": ["Original prompts, contexts and responses are unchanged; new prompt effectiveness is not measured.",
                              "Citation acceptance does not prove classification accuracy or causality; no patches or tests are rerun."],
              "archives": []}
    for path in paths:
        before = digest(path.read_bytes())
        archive = read_json(path, limit=50_000_000)
        rows = [replay_row(row) for row in archive["cases"]]
        if digest(path.read_bytes()) != before:
            raise DoctorError("Historical archive changed during replay")
        report["archives"].append({"path": path.as_posix(), "sha256": before, "unchanged": True,
                                   "suite_version": archive["suite_version"], "cases": rows})
    rows = [r for a in report["archives"] for r in a["cases"]]
    report["summary"] = {"responses": len(rows),
                         "previously_accepted": sum(r["historical_validation"] == "accepted" for r in rows),
                         "accepted_now": sum(r["validation"] == "accepted" for r in rows),
                         "recovered": [r["case_id"] for r in rows if r["historical_validation"] == "rejected" and r["validation"] == "accepted"],
                         "still_rejected": [r["case_id"] for r in rows if r["validation"] == "rejected"],
                         "newly_rejected": [r["case_id"] for r in rows if r["historical_validation"] == "accepted" and r["validation"] == "rejected"]}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite an existing replay or measurement")
    report = replay_archives(args.archives)
    write_json(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False))
