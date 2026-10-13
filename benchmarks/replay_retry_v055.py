"""Offline public failure replay. NO external model or new accuracy measurement.

Replay the archived diagnosis and failing first patch, then submit a predetermined
complete for+while patch. Keep original cases, budgets and acceptance unchanged;
add only explicit visible diagnostic replay bindings in the disposable copy.
"""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from agentdoctor.benchmark import _heldout, _suite_pass
from agentdoctor.core import digest, read_json, write_json
from agentdoctor.engine import run
from agentdoctor.project import check_manifest, snapshot
from agentdoctor.runner import ValidationRunner

ROOT = Path(__file__).resolve().parents[1]


def main():
    archive_path = ROOT / "benchmarks/codex-public-smolagents-v0.5.4-2026-10-13.json"
    archive = read_json(archive_path, limit=50_000_000)
    old = next(c for c in archive["raw_report"]["cases"] if c["id"] == "loop_else_fault")
    source = ROOT / "benchmarks/public-smolagents-v1/cases/loop_else_fault"
    output = ROOT / "artifacts" / ("retry-v055-public-" + uuid.uuid4().hex[:12])
    output.mkdir()
    project = output / "project"
    original_manifest = snapshot(source, project)
    config = read_json(project / "doctor.json")
    original_config = copy.deepcopy(config)
    for command in config["validation"]["commands"]:
        command["replay"] = {"module": "subject", "cases_file": "visible.json", "case_group": command["kind"],
                             "trace_functions": ["evaluate_python_code", "evaluate_for", "evaluate_while"]}
    write_json(project / "doctor.json", config)
    write_json(output / "traces.json", old["traces"])

    class OfflineProvider:
        provider, name, calls, max_calls = "offline-preset", "no-external-model", 0, 3
        metadata = {"external_model_calls": 0, "scope": "archived diagnosis and first patch; predetermined complete retry"}

        def __init__(self):
            self.usage, self.retry_context = [], None

        def complete(self, instruction, context, schema):
            self.calls += 1
            if self.calls == 1:
                return copy.deepcopy(old["engine"]["model_events"][0]["output"])
            proposal = copy.deepcopy(old["engine"]["attempts"][0]["proposal"])
            proposal["base_snapshot_id"] = context["patch_base"]["snapshot_id"]
            if self.calls == 3:
                self.retry_context = context
                failure = context["retry_feedback"]["failed_cases"][0]
                assert failure["case_id"] == "target:2" and failure["reproduces_validation"]
                assert failure["replay"]["status"] == "completed"
                assert any(s["node"] == "evaluate_while" for s in failure["replay"]["trace"]["spans"])
                assert context["patch_base"]["preserve_observed_improvements"][0]["case_id"] == "target:1"
                assert len(json.dumps(context, ensure_ascii=False)) <= original_config["policy"]["max_context_chars"]
                assert "_heldout.json" not in json.dumps(context) and "https://github.com/huggingface/smolagents/issues/" not in json.dumps(context)
                before = ('            raise InterpreterError(f"Maximum number of {MAX_WHILE_ITERATIONS} iterations in While loop exceeded")\n'
                          '    return None')
                proposal["edits"].append({"path": "smolagents/local_python_executor.py", "before": before,
                    "after": before.replace("    return None", "    for node in while_loop.orelse:\n        evaluate_ast(node, state, static_tools, custom_tools, authorized_imports)\n    return None"),
                    "reason": "Predetermined test patch: evaluate while else after normal termination"})
                proposal["summary"] = "Offline predetermined COMPLETE for+while patch against original snapshot"
            return proposal

    provider = OfflineProvider()
    report = run(project, output / "traces.json", mode="repair", executor="local", output=output / "runs", model=provider)
    assert report["status"] == "verified", (report["reason"], report["warnings"])
    assert len(report["attempts"]) == 2
    assert report["attempts"][0]["failure_stage"] == "visible_validation"
    # Held-out data is read only after all model-facing requests have finished.
    spec = next(c for c in archive["raw_report"]["plan"]["cases"] if c["id"] == old["id"])["heldout"]
    heldout = _heldout(Path(report["candidate_project"]), output / "oracle", spec, ValidationRunner("local"))
    assert _suite_pass(heldout, "target") and _suite_pass(heldout, "regression")
    assert check_manifest(source, original_manifest) == []
    current_config = read_json(project / "doctor.json")
    for command in current_config["validation"]["commands"]:
        command.pop("replay")
    assert current_config == original_config
    context = provider.retry_context
    result = {"scope": "offline integration test, NOT external model accuracy", "external_model_calls": 0,
              "prior_archive": str(archive_path), "prior_archive_sha256": digest(archive_path.read_bytes()),
              "cases_budgets_acceptance_unchanged": True, "original_fixture_unchanged": True,
              "only_config_addition": "visible replay binding in disposable copy", "budget": original_config["policy"]["max_context_chars"],
              "retry_context_chars": len(json.dumps(context, ensure_ascii=False)), "complete_patch_verified": True,
              "hidden_checks_passed": True, "retry_feedback": report["attempts"][0]["retry_feedback"],
              "report": report, "heldout": heldout}
    write_json(output / "result.json", result)
    print(json.dumps({"result": str(output / "result.json"), "external_model_calls": 0,
                      "retry_context_chars": result["retry_context_chars"], "status": report["status"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
