"""Exercise the public recorder, including failures before a tool body runs."""
import inspect
from pathlib import Path

import agent
from agentdoctor.traces import TraceRecorder


if __name__ == "__main__":
    trace = Path("trace.jsonl")
    if trace.exists():
        raise SystemExit("trace.jsonl already exists; use a fresh demo project")
    recorder = TraceRecorder(trace, "demo-lookup")
    source = {"path": "agent.py", "function": "lookup_customer", "line": inspect.getsourcelines(agent.lookup_customer)[1]}
    agent.tool_lookup = recorder.wrap("lookup_customer", agent.tool_lookup, kind="tool", tool="lookup_customer", source=source)
    observed_agent = recorder.wrap("customer_agent", agent.lookup_customer)
    output, error = None, None
    try:
        output = observed_agent("C001")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    passed = output is not None and output.get("name") == "Ada"
    recorder.record({"span_id": "task-1", "node": "customer_agent", "kind": "task",
                  "input": {"task": "Find customer C001 and return the name"}, "output": output, "error": error,
                  "task_status": "completed" if passed else "failed",
                  "assertions": [{"name": "correct_customer_name", "passed": passed, "expected": "Ada", "actual": output}],
                  "metadata": {"task_id": "find-customer-C001"}})
