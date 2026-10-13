from __future__ import annotations

import functools
import inspect
import json
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

from .contracts import TRACE_SCHEMA, check_schema, validate
from .core import DoctorError, parse_json, redact, utc_now


def evidence_id(span: dict) -> str:
    return f"{span['run_id']}/{span['span_id']}"


def load_traces(path: Path) -> list[dict]:
    if path.stat().st_size > 20_000_000:
        raise DoctorError("Trace input exceeds 20 MB; split it into smaller batches")
    text = path.read_text(encoding="utf-8-sig")
    if text.lstrip().startswith("["):
        spans = parse_json(text)
    else:
        spans = [parse_json(line) for line in text.splitlines() if line.strip()]
    if not isinstance(spans, list) or len(spans) > 10000:
        raise DoctorError("Trace input must contain at most 10000 spans")
    seen = set()
    for index, span in enumerate(spans):
        validate(span, TRACE_SCHEMA, f"trace[{index}]")
        if "/" in span["run_id"] or "/" in span["span_id"]:
            raise DoctorError("run_id and span_id cannot contain '/' characters")
        key = evidence_id(span)
        if key in seen:
            raise DoctorError(f"Duplicate span identifier: {key}")
        seen.add(key)
        if span["kind"] == "tool" and "tool" not in span:
            raise DoctorError(f"Tool span requires a tool name: {key}")
        for field in ("input_schema", "output_schema"):
            if field in span:
                check_schema(span[field])
    # Validate cycles among available parents; partial traces are allowed.
    parents = {evidence_id(s): f"{s['run_id']}/{s['parent_id']}" for s in spans if s.get("parent_id")}
    for key in parents:
        chain = set()
        while key in parents:
            if key in chain:
                raise DoctorError("Trace parent links contain a cycle")
            chain.add(key)
            key = parents[key]
    return spans


class TraceRecorder:
    """JSONL recorder with synchronous and asynchronous node wrappers.

    Only JSON-serializable inputs/outputs are supported. No hidden reasoning is recorded.
    Write failures are intentionally surfaced: callers should decide their telemetry policy.
    """

    def __init__(self, path: str | Path, run_id: str | None = None):
        self.path = Path(path)
        self.run_id = run_id or uuid.uuid4().hex
        self._lock = threading.Lock()
        self._parent = ContextVar(f"agentdoctor_parent_{id(self)}", default=None)

    def record(self, span: dict) -> None:
        value = {"run_id": self.run_id, "span_id": uuid.uuid4().hex, **span}
        validate(value, TRACE_SCHEMA, "recorded span")
        line = json.dumps(redact(value), ensure_ascii=False, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def wrap(self, name: str, function, *, kind: str = "node", tool: str | None = None, source: dict | None = None):
        def begin():
            span = {"span_id": uuid.uuid4().hex, "parent_id": self._parent.get(), "node": name,
                    "kind": kind, "timestamp": utc_now(), "metadata": {}}
            if tool:
                span["tool"] = tool
            if source:
                span["source"] = source
            return span, time.monotonic(), self._parent.set(span["span_id"])

        def capture(span, args, kwargs):
            # Establish the span before capture/binding so parameter errors cannot
            # disappear. Keep the exact call separate from normalized arguments.
            raw = parse_json(json.dumps({"args": args, "kwargs": kwargs}, ensure_ascii=False, allow_nan=False))
            span["metadata"]["call"] = raw
            span["input"] = raw["kwargs"] if not args else raw
            span["metadata"]["input_encoding"] = "raw_keywords" if not args else "raw_call"
            try:
                signature = inspect.signature(function)
                # Raw positional arguments need the same named-secret redaction
                # as bound arguments, including when full binding will fail.
                position = 0
                for parameter in signature.parameters.values():
                    if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
                        if position < len(raw["args"]):
                            raw["args"][position] = redact({parameter.name: raw["args"][position]})[parameter.name]
                        position += 1
                    elif parameter.kind == parameter.VAR_POSITIONAL:
                        for index in range(position, len(raw["args"])):
                            raw["args"][index] = redact({parameter.name: raw["args"][index]})[parameter.name]
                bound = signature.bind(*args, **kwargs)
            except (TypeError, ValueError) as exc:
                span["metadata"]["binding_error"] = f"{type(exc).__name__}: {exc}"
                # Invoke the actual callable to preserve its TypeError (or valid
                # behavior when introspection is unavailable), including async.
            else:
                span["input"] = parse_json(json.dumps(bound.arguments, ensure_ascii=False, allow_nan=False))
                span["metadata"]["input_encoding"] = "bound_arguments"

        def finish(span, started, token, output=None, error=None):
            self._parent.reset(token)
            span.update(output=output, error=error, duration_ms=round((time.monotonic() - started) * 1000, 3))
            self.record(span)

        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                span, started, token = begin()
                try:
                    capture(span, args, kwargs)
                    output = await function(*args, **kwargs)
                except BaseException as exc:
                    finish(span, started, token, error=f"{type(exc).__name__}: {exc}")
                    raise
                finish(span, started, token, output=output)
                return output
            return asynchronous

        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            span, started, token = begin()
            try:
                capture(span, args, kwargs)
                output = function(*args, **kwargs)
            except BaseException as exc:
                finish(span, started, token, error=f"{type(exc).__name__}: {exc}")
                raise
            finish(span, started, token, output=output)
            return output
        return synchronous
