"""Record the actual exception and arguments produced by the broken demo."""
import inspect
import json
from pathlib import Path

import agent

spans = []
original_tool = agent.tool_lookup


def recorded_tool(**arguments):
    span = {"run_id": "demo-lookup", "span_id": "tool-1", "parent_id": "task-1", "node": "lookup_customer",
            "kind": "tool", "tool": "lookup_customer", "input": arguments,
            "source": {"path": "agent.py", "function": "lookup_customer", "line": inspect.currentframe().f_back.f_lineno},
            "metadata": {"task_id": "find-customer-C001"}}
    try:
        result = original_tool(**arguments)
        span["output"] = result
        return result
    except Exception as exc:
        span["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        spans.append(span)


if __name__ == "__main__":
    agent.tool_lookup = recorded_tool
    output, error = None, None
    try:
        output = agent.lookup_customer("C001")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    passed = output is not None and output.get("name") == "Ada"
    spans.append({"run_id": "demo-lookup", "span_id": "task-1", "node": "customer_agent", "kind": "task",
                  "input": {"task": "Find customer C001 and return the name"}, "output": output, "error": error,
                  "task_status": "completed" if passed else "failed",
                  "assertions": [{"name": "correct_customer_name", "passed": passed, "expected": "Ada", "actual": output}],
                  "metadata": {"task_id": "find-customer-C001"}})
    Path("trace.jsonl").write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in spans), encoding="utf-8")
