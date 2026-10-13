"""Strict JSON contracts. External schema references are deliberately disabled."""
from __future__ import annotations

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import NoSuchResource

from .core import DoctorError


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": STRING}
SOURCE = obj({"path": STRING, "function": {"type": "string"}, "line": {"type": "integer", "minimum": 1}}, ["path"])
ASSERTION = obj({"name": STRING, "passed": {"type": "boolean"}, "expected": {}, "actual": {}}, ["name", "passed"])
TRACE_SCHEMA = obj({
    "run_id": STRING, "span_id": STRING, "parent_id": {"type": ["string", "null"]},
    "node": STRING, "kind": {"enum": ["node", "tool", "llm", "task"]},
    "input": {}, "output": {}, "error": {"type": ["string", "null"]},
    "tool": STRING, "source": SOURCE, "state_before": {"type": "object"}, "state_after": {"type": "object"},
    "input_schema": {"type": ["object", "boolean"]}, "output_schema": {"type": ["object", "boolean"]},
    "prompt": {"type": "string"}, "assertions": {"type": "array", "items": ASSERTION},
    "task_status": {"enum": ["completed", "failed", "unknown"]},
    "timestamp": {"type": "string"}, "duration_ms": {"type": "number", "minimum": 0},
    "metadata": {"type": "object"},
}, ["run_id", "span_id", "node", "kind"])

COMMAND = obj({
    "name": STRING, "kind": {"enum": ["target", "regression"]},
    "argv": {"type": "array", "items": STRING, "minItems": 1},
    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
    "expected_cases": {"type": "array", "items": STRING, "minItems": 1, "uniqueItems": True},
    "replay": obj({
        "module": STRING, "cases_file": STRING, "case_group": STRING,
        "trace_functions": {"type": "array", "items": STRING, "minItems": 1, "uniqueItems": True},
        "max_spans": {"type": "integer", "minimum": 1, "maximum": 1000},
    }, ["module", "cases_file"]),
}, ["name", "kind", "argv"])

CONFIG_SCHEMA = obj({
    "schema_version": {"const": 1}, "project_name": STRING, "source_paths": {"type": "array", "items": STRING, "minItems": 1},
    "tools": {"type": "object", "additionalProperties": obj({
        "input_schema": {"type": ["object", "boolean"]},
        "aliases": {"type": "object", "additionalProperties": STRING},
    }, ["input_schema"])},
    "checks": obj({"loop_threshold": {"type": "integer", "minimum": 2}, "preserve_state_keys": STRINGS}, []),
    "validation": obj({
        "commands": {"type": "array", "items": COMMAND},
        "repetitions": {"type": "integer", "minimum": 1, "maximum": 10},
        "docker_image": STRING,
    }, ["commands"]),
    "policy": obj({
        "protected_paths": STRINGS,
        "max_candidates": {"type": "integer", "minimum": 1, "maximum": 10},
        "max_changed_files": {"type": "integer", "minimum": 1, "maximum": 20},
        "max_changed_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
        "max_model_calls": {"type": "integer", "minimum": 1, "maximum": 50},
        "max_context_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
    }, []),
}, ["schema_version", "project_name", "source_paths", "tools", "validation"])


def _no_retrieve(uri: str):
    raise NoSuchResource(ref=uri)


def check_schema(schema) -> None:
    def check_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("$ref", "$dynamicRef") and (not isinstance(child, str) or not child.startswith("#")):
                    raise DoctorError("Only in-document JSON Schema references are allowed")
                if key == "$schema" and child not in ("https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"):
                    raise DoctorError("Only JSON Schema Draft 2020-12 is supported")
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)
    check_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise DoctorError(f"Invalid JSON Schema: {exc.message}") from exc


def violations(instance, schema) -> list[dict]:
    check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker(), registry=Registry(retrieve=_no_retrieve))
    try:
        errors = sorted(validator.iter_errors(instance), key=lambda e: str(list(e.path)))
        return [{"path": "/" + "/".join(str(p) for p in e.path), "rule": e.validator, "message": e.message} for e in errors]
    except Exception as exc:
        raise DoctorError(f"Cannot resolve or evaluate schema: {type(exc).__name__}") from exc


def validate(instance, schema, label: str) -> None:
    errors = violations(instance, schema)
    if errors:
        raise DoctorError(f"{label}: {errors[0]['path']} {errors[0]['message']}")
