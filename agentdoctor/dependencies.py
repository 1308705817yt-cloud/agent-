"""Bounded, non-executing dependency discovery for a supplied source location.

Only module-level bindings and statically named local imports are resolved.
Dynamic receivers, import side effects and third-party packages are not modeled.
"""
from __future__ import annotations

import ast
import builtins
import copy
from collections import deque
from pathlib import Path

from .core import read_text
from .project import editable
from .source_context import source_variants


def _range(node):
    start = node.lineno
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = min([start] + [d.lineno for d in node.decorator_list])
    return start, node.end_lineno


def _bindings(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name) for a in node.names]
    return []


def _references(node):
    """Collect lexical loads, ignoring the outer function's local bindings."""
    local = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        local = {a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
        local.update(a.arg for a in (node.args.vararg, node.args.kwarg) if a)

        class Locals(ast.NodeVisitor):
            def visit_Name(self, value):
                if isinstance(value.ctx, ast.Store):
                    local.add(value.id)

            def visit_FunctionDef(self, value):
                local.add(value.name)

            visit_AsyncFunctionDef = visit_FunctionDef
            visit_ClassDef = visit_FunctionDef

        for statement in node.body:
            Locals().visit(statement)
        local -= {name for n in ast.walk(node) if isinstance(n, ast.Global) for name in n.names}
    refs, dynamic = [], []

    class Loads(ast.NodeVisitor):
        def visit_Attribute(self, value):
            parts, base = [value.attr], value.value
            while isinstance(base, ast.Attribute):
                parts.insert(0, base.attr)
                base = base.value
            if isinstance(base, ast.Name):
                if base.id not in local:
                    refs.append((base.id, parts, value.lineno))
            else:
                self.generic_visit(value)

        def visit_Name(self, value):
            if isinstance(value.ctx, ast.Load) and value.id not in local:
                refs.append((value.id, [], value.lineno))

        def visit_Call(self, value):
            if isinstance(value.func, ast.Name):
                name = value.func.id
                if name in {"getattr", "eval", "exec", "__import__"} or name in local:
                    dynamic.append((name, value.lineno, "runtime_binding"))
            elif isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Name):
                base = value.func.value.id
                if base in local or value.func.attr == "import_module":
                    dynamic.append((base + "." + value.func.attr, value.lineno, "runtime_receiver"))
            self.generic_visit(value)

    Loads().visit(node)
    return refs, dynamic


def dependency_locations(project: Path, manifest: dict, locations: dict, config: dict,
                         *, max_files: int = 32, max_definitions: int = 256) -> tuple[dict, dict]:
    expanded = copy.deepcopy(locations)
    modules, queue, seen = {}, deque(), set()
    edges, unresolved = [], []
    edge_keys, unresolved_keys, imports_seen = set(), set(), set()

    def missing(path, reference, line, reason):
        key = (path, reference, line, reason)
        if key not in unresolved_keys:
            unresolved_keys.add(key)
            unresolved.append({"path": path, "reference": reference, "line": line, "reason": reason})

    def module(path):
        if path in modules:
            return modules[path]
        if path not in manifest or not editable(path, config):
            missing(path, path, None, "outside_allowed_sources")
            return None
        if len(modules) >= max_files:
            missing(path, path, None, "file_limit")
            return None
        try:
            text = read_text(project / path)
            tree = ast.parse(text, filename=path)
        except (UnicodeError, SyntaxError, RecursionError):
            missing(path, path, None, "unparseable_source")
            return None
        definitions = {}
        for node in tree.body:
            for name in _bindings(node):
                definitions.setdefault(name, []).append(node)
        modules[path] = (text, tree, definitions)
        return modules[path]

    def resolve_module(name, origin, line):
        stem = name.replace(".", "/")
        matches = [p for prefix in ("", "src/") for p in (prefix + stem + ".py", prefix + stem + "/__init__.py") if p in manifest]
        if len(matches) != 1:
            missing(origin, name, line, "ambiguous_module" if matches else "external_or_missing_module")
            return None
        return matches[0]

    def enqueue(path, node, origin, reference):
        start, end = _range(node)
        key = (path, start, end)
        if key not in seen and len(seen) >= max_definitions:
            missing(origin, reference, start, "definition_limit")
            return
        edge = (origin, reference, path, start, end)
        if reference != "located_source" and edge not in edge_keys:
            edge_keys.add(edge)
            edges.append({"from_path": origin, "reference": reference, "path": path, "start_line": start, "end_line": end})
        if key not in seen:
            seen.add(key)
            expanded.setdefault(path, []).append({"path": path, "start_line": start, "end_line": end})
            queue.append((path, node))

    def imported(path, node, name, attrs, line):
        key = (path, node.lineno, name, tuple(attrs))
        if key in imports_seen:
            return
        imports_seen.add(key)
        alias = next(a for a in node.names if (a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name)) == name)
        if isinstance(node, ast.Import):
            module_name = alias.name
            if not alias.asname and "." in alias.name:
                prefix = alias.name.split(".")[1:]
                if attrs[:len(prefix)] != prefix:
                    missing(path, name, line, "module_object")
                    return
                attrs = attrs[len(prefix):]
            symbol = attrs[0] if attrs else None
            remaining = attrs[1:]
        else:
            base = path.removesuffix(".py").split("/")[:-1]
            if base and base[0] == "src":
                base = base[1:]
            if node.level:
                if node.level > len(base):
                    missing(path, name, line, "relative_import_outside_project")
                    return
                base = base[:len(base) - node.level + 1]
                module_name = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                module_name = node.module or ""
            symbol = alias.name
            remaining = attrs
        target = resolve_module(module_name, path, line)
        data = module(target) if target else None
        definitions = data[2].get(symbol, []) if data and symbol else []
        # `from package import submodule` can name a module instead of a binding.
        if not definitions and isinstance(node, ast.ImportFrom) and attrs:
            submodule = resolve_module(module_name + "." + symbol, path, line)
            subdata = module(submodule) if submodule else None
            if subdata:
                target, definitions = submodule, subdata[2].get(attrs[0], [])
                symbol, remaining = attrs[0], attrs[1:]
        if definitions:
            for definition in definitions:
                enqueue(target, definition, path, name + ("." + ".".join(attrs) if attrs else ""))
                if isinstance(definition, (ast.Import, ast.ImportFrom)):
                    imported(target, definition, symbol, remaining, line)
        elif target:
            missing(path, name, line, "unresolved_imported_binding")

    for path, mapped in locations.items():
        if not path.endswith(".py") or path not in manifest or not editable(path, config):
            continue
        data = module(path)
        if not data:
            continue
        text, tree, _ = data
        _, selected = source_variants(path, manifest[path], text, mapped)
        if not selected[0]["truncated"]:
            expanded[path].append({"path": path, "whole_file": True})
        # Start at maximal statements wholly inside selected ranges. This also
        # handles a method slice without pulling in unrelated sibling methods.
        def roots(node):
            if isinstance(node, ast.stmt) and any(s["start_line"] <= _range(node)[0] and _range(node)[1] <= s["end_line"] for s in selected):
                enqueue(path, node, path, "located_source")
                return
            for child in ast.iter_child_nodes(node):
                roots(child)
        roots(tree)

    while queue:
        path, node = queue.popleft()
        data = modules[path]
        refs, dynamic = _references(node)
        for name, line, reason in dynamic:
            missing(path, name, line, reason)
        for name, attrs, line in refs:
            definitions = data[2].get(name, [])
            if not definitions:
                if name not in vars(builtins):
                    missing(path, name, line, "unresolved_name")
                continue
            for definition in definitions:
                enqueue(path, definition, path, name)
                if isinstance(definition, (ast.Import, ast.ImportFrom)):
                    imported(path, definition, name, attrs, line)
        for nested in ast.walk(node):
            if isinstance(nested, ast.ImportFrom) and any(a.name == "*" for a in nested.names):
                missing(path, "*", nested.lineno, "wildcard_import")
    return expanded, {"method": "bounded static module bindings; not runtime dependency completeness",
                      "status": "partial_static_subset" if unresolved else "resolved_static_subset",
                      "limits": {"files": max_files, "definitions": max_definitions},
                      "files_inspected": len(modules), "definitions_included": len(seen),
                      "resolved": edges, "unresolved": unresolved}
