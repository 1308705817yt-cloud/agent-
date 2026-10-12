"""Example: instrument a LangGraph node without depending on LangGraph internally.

Assumes AgentDoctor is installed in the target project's environment. Wrap each
tool separately to record tool arguments, and use JSON-serializable state.
"""
from agentdoctor.traces import TraceRecorder


def add_observed_node(builder, node_function, trace_path="runs/trace.jsonl", run_id=None):
    recorder = TraceRecorder(trace_path, run_id=run_id)
    wrapped = recorder.wrap("planner", node_function, source={"path": "src/planner.py", "function": node_function.__name__})
    builder.add_node("planner", wrapped)
    return recorder


def record_task_result(recorder, correct: bool, expected, actual):
    recorder.record({"node": "task", "kind": "task", "task_status": "completed" if correct else "failed",
                     "assertions": [{"name": "business_outcome", "passed": correct, "expected": expected, "actual": actual}]})
