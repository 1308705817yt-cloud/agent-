from __future__ import annotations

import ast
import difflib
from pathlib import Path

from .contracts import validate, violations
from .core import DoctorError, contained_path, digest, read_text
from .model import PATCH_SCHEMA
from .project import editable
from .traces import evidence_id


def _byte_position(text: str, lineno: int, offset: int) -> int:
    return sum(len(line.encode("utf-8")) for line in text.splitlines(keepends=True)[:lineno - 1]) + offset


def alias_proposals(project: Path, spans: list[dict], findings: list[dict], config: dict) -> list[dict]:
    """Narrow, deterministic repair based on an explicit, user-declared alias.

    No semantic equivalence is guessed. Only unique dict literals in the recorded
    source function can be rewritten, and the corrected observed input must validate.
    """
    proposals = []
    seen = set()
    for span in spans:
        source = span.get("source", {})
        relative = source.get("path")
        tool = config["tools"].get(span.get("tool"), {})
        inputs = span.get("input")
        related = [f["id"] for f in findings if f["category"] == "tool_argument_schema" and evidence_id(span) in f["evidence"]]
        if not related or not relative or not source.get("function") or not isinstance(inputs, dict) or not editable(relative, config):
            continue
        path = contained_path(project, relative)
        if not path.is_file():
            continue
        content = read_text(path)
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == source["function"]]
        if len(functions) != 1:
            continue
        for old, new in tool.get("aliases", {}).items():
            if old not in inputs or new in inputs:
                continue
            corrected = {new if k == old else k: v for k, v in inputs.items()}
            if violations(corrected, tool["input_schema"]):
                continue
            nodes = []
            for node in ast.walk(functions[0]):
                if not isinstance(node, ast.Dict):
                    continue
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
                if len(keys) == len(node.keys) and set(keys) == set(inputs) and keys.count(old) == 1 and new not in keys:
                    key_node = next(key for key in node.keys if key.value == old)
                    nodes.append((node, key_node))
            if len(nodes) != 1:
                continue
            node, key_node = nodes[0]
            before = ast.get_source_segment(content, node)
            start = _byte_position(content, node.lineno, node.col_offset)
            key_start = _byte_position(content, key_node.lineno, key_node.col_offset) - start
            key_end = _byte_position(content, key_node.end_lineno, key_node.end_col_offset) - start
            raw = before.encode("utf-8")
            after = (raw[:key_start] + repr(new).encode("utf-8") + raw[key_end:]).decode("utf-8")
            identity = (relative, before, after)
            if identity in seen:
                continue
            seen.add(identity)
            proposals.append({"summary": f"Use declared tool argument alias {old} -> {new}", "finding_ids": related,
                              "edits": [{"path": relative, "before": before, "after": after, "reason": "Explicit alias in the frozen tool contract; corrected arguments satisfy the schema"}]})
    return proposals


def apply_proposal(project: Path, proposal: dict, config: dict, known_findings: set[str], manifest: dict[str, str]) -> dict:
    validate(proposal, PATCH_SCHEMA, "patch proposal")
    if not proposal["finding_ids"] or not set(proposal["finding_ids"]).issubset(known_findings):
        raise DoctorError("Patch must cite existing finding IDs")
    if not proposal["edits"] or len(proposal["edits"]) > 50:
        raise DoctorError("Patch must contain 1-50 edits")
    originals, updated = {}, {}
    for edit in proposal["edits"]:
        relative = edit["path"]
        if not editable(relative, config):
            raise DoctorError(f"Patch target is outside the allowlist or protected: {relative}")
        path = contained_path(project, relative)
        if not path.is_file() or relative not in manifest:
            raise DoctorError("Patches may only modify existing snapshot files")
        if relative not in originals:
            if digest(path.read_bytes()) != manifest[relative]:
                raise DoctorError(f"Source changed after snapshot: {relative}")
            originals[relative] = updated[relative] = read_text(path)
        before, after = edit["before"], edit["after"]
        if before == after or updated[relative].count(before) != 1:
            raise DoctorError(f"Edit must match exactly one distinct source fragment: {relative}")
        updated[relative] = updated[relative].replace(before, after, 1)
    if len(updated) > config["policy"]["max_changed_files"]:
        raise DoctorError("Patch exceeds changed-file limit")
    diff = []
    changed_lines = 0
    for relative, content in updated.items():
        if len(content.encode("utf-8")) > 2_000_000:
            raise DoctorError("Patched source exceeds size limit")
        if relative.endswith(".py"):
            try:
                ast.parse(content, filename=relative)
            except SyntaxError as exc:
                raise DoctorError(f"Patch introduces invalid Python: {relative}:{exc.lineno}") from exc
        lines = list(difflib.unified_diff(originals[relative].splitlines(keepends=True), content.splitlines(keepends=True), fromfile="a/" + relative, tofile="b/" + relative))
        changed_lines += sum(1 for line in lines if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        diff.extend(lines)
    if not changed_lines or changed_lines > config["policy"]["max_changed_lines"]:
        raise DoctorError("Patch is empty or exceeds changed-line limit")
    # All policy checks complete before any mutation; this directory is a disposable copy.
    for relative, content in updated.items():
        with contained_path(project, relative).open("w", encoding="utf-8", newline="") as stream:
            stream.write(content)
    return {"diff": "".join(diff), "changed_files": list(updated), "changed_lines": changed_lines,
            "hashes": {p: digest(contained_path(project, p).read_bytes()) for p in updated}}
