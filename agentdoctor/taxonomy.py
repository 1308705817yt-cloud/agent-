"""Shared symptom/defect distinction for diagnosis, model context and scoring."""

# Keep these labels identical to the historical benchmark exclusions.
SYMPTOMS = frozenset({"execution_error", "task_failed", "task_assertion", "validation_failure",
                      "input_schema", "output_schema", "invalid_json", "unverified_completion"})

CATEGORY_DEFINITIONS = {
    "tool_argument_schema": "Registered tool argument contract violated (name, type, shape).",
    "state_invariant": "Configured state invariant lost or changed during an update.",
    "possible_loop": "Repeated actions without progress or after completion. Exclude documented bounded pending polling; repetition alone is insufficient.",
    "prompt_conflict": "Mutually incompatible prompt instructions; invalid output alone does not establish a conflict.",
    "plan_action_mismatch": "Different explicit metadata.plan and metadata.action on the same cited span; never compare steps or duplicate another fault.",
    "unknown_tool": "An observed tool call has no registered tool contract.",
    "python_syntax": "Source cannot be parsed as Python.",
    "other": "Explicit business/assertion/schema contract violation outside named categories; a symptom alone is insufficient.",
}


def classification_level(category: str) -> str:
    return "symptom" if category in SYMPTOMS else "fault" if category in CATEGORY_DEFINITIONS else "observation" if category == "bounded_polling" else "unknown"


def classification_reason(category: str, cited: list[dict], diagnosis: dict, *, observed_evidence: list[str]) -> str | None:
    """One gate for new findings and classifications attached to symptoms.

    Contract evidence must belong to the attributed/selected observation, not
    merely another cited step. Schema failures come from recorded rule findings,
    not from the model's proposed category or free-form contract description.
    """
    if category == "plan_action_mismatch":
        steps = [s.get("metadata", {}) for s in cited]
        if not any(s.get("plan") and s.get("action") and s["plan"] != s["action"] for s in steps):
            return "Plan/action classification requires an explicit recorded plan and distinct observed action on the same span; unlinked steps and inferred intent are insufficient"
    if category == "possible_loop":
        refs = {f"{s['run_id']}/{s['span_id']}" for s in cited}
        if any(refs <= set(o["evidence"]) for o in diagnosis.get("observations", []) if o["category"] == "bounded_polling"):
            return "Cited calls are pending responses within an observed, exhausted finite polling budget"
    if category == "other":
        observed = [s for s in cited if f"{s['run_id']}/{s['span_id']}" in observed_evidence]
        refs = {f"{s['run_id']}/{s['span_id']}" for s in observed}
        has_contract = any(a.get("passed") is False for s in observed for a in s.get("assertions", []))
        for issue in diagnosis["findings"]:
            if not set(issue["evidence"]) & refs:
                continue
            if issue["category"] in {"input_schema", "output_schema"} and issue.get("details", {}).get("violations"):
                has_contract = True
            if issue["category"] == "invalid_json":
                has_contract |= any(s.get("output_schema") is not None for s in observed
                                    if f"{s['run_id']}/{s['span_id']}" in issue["evidence"])
        if not has_contract:
            return "An other classification needs a recorded failed assertion or schema violation at the attributed observation; an error or another step's contract is insufficient"
    return None
