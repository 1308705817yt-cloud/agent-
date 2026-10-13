"""Verbatim source ranges for model context; target code is never executed."""
from __future__ import annotations

import ast
import re

from .citations import source_id


def _functions(tree: ast.AST) -> list[tuple[str, int, int]]:
    functions = []

    def visit(node, parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parents = (*parents, node.name)
            if not isinstance(node, ast.ClassDef):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                functions.append((".".join(parents), start, node.end_lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree, ())
    return functions


def _located_ranges(relative: str, content: str, line_count: int, locations: list[dict]) -> list[tuple[int, int]]:
    whole = [(1, line_count)]
    if any(loc.get("whole_file") for loc in locations):
        return whole
    explicit = [(loc["start_line"], loc["end_line"]) for loc in locations if "start_line" in loc and "end_line" in loc]
    precise = {(loc.get("function") or "", loc.get("line")) for loc in locations
               if loc.get("function") or loc.get("line")}
    if not precise and not explicit:
        return whole
    functions = []
    if relative.endswith(".py"):
        try:
            functions = _functions(ast.parse(content, filename=relative))
        except (SyntaxError, RecursionError):
            pass
    ranges = explicit[:]
    for name, line in precise:
        if line is not None and not (1 <= line <= line_count):
            return whole
        if name:
            name = name.replace(".<locals>.", ".")
            if "." not in name:
                candidates = [f for f in functions if name == f[0].rsplit(".", 1)[-1]]
            else:
                candidates = [f for f in functions if name == f[0]]
                if not candidates:
                    module = relative.removesuffix(".py").replace("/", ".")
                    prefixes = {module, module.rsplit(".", 1)[-1]}
                    local_names = {name[len(prefix) + 1:] for prefix in prefixes if name.startswith(prefix + ".")}
                    candidates = [f for f in functions if f[0] in local_names]
            matches = [(start, end) for _, start, end in candidates]
            if line is not None:
                matches = [(start, end) for start, end in matches if start <= line <= end]
            # An unresolved/stale locator must not degrade to the file prefix.
            if not matches:
                return whole
            ranges.extend(matches)
        else:
            containing = [(start, end) for _, start, end in functions if start <= line <= end]
            if containing:
                ranges.append(min(containing, key=lambda pair: pair[1] - pair[0]))
            else:
                # Syntax errors, module-level statements, and non-Python text.
                ranges.append((max(1, line - 20), min(line_count, line + 20)))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged or whole


def source_variants(relative: str, sha256: str, content: str, locations: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return whole-file and located views; each entry is a contiguous exact slice.

    Use Python physical line endings, not Unicode separators inside literals.
    Keeping whole lines preserves CRLF, indentation and non-ASCII patch text.
    """
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", content)
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)

    def excerpts(ranges):
        truncated = sum(end - start + 1 for start, end in ranges) < total
        return [{"source_id": source_id(relative, sha256, start, end),
                 "path": relative, "sha256": sha256, "text": "".join(lines[start - 1:end]),
                 "start_line": start, "end_line": end, "total_lines": total, "truncated": truncated}
                for start, end in ranges]

    full = excerpts([(1, total)])
    focused = excerpts(_located_ranges(relative, content, total, locations))
    return full, focused
