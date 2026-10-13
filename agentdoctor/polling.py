"""Conservative, non-executing checks of exhausted polling call trees."""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .core import read_text
from .traces import evidence_id


def _pending(value) -> bool:
    if not isinstance(value, dict):
        return False
    signals = [value[k] is False for k in ("done", "completed") if k in value]
    if "status" in value:
        signals.append(value["status"] in ("pending", "running", "in_progress"))
    return bool(signals) and all(signals)


class _Code:
    """Resolve top-level functions and integer constants, without imports."""

    def __init__(self, project, manifest):
        self.project, self.manifest, self.trees = project, manifest, {}

    def tree(self, path):
        if path not in self.manifest or not path.endswith(".py"):
            return None
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(read_text(self.project / path))
            except (SyntaxError, UnicodeError, OSError, RecursionError):
                self.trees[path] = None
        return self.trees[path]

    def module(self, parts):
        name = "/".join(parts)
        matches = [p for p in (name + ".py", name + "/__init__.py", "src/" + name + ".py", "src/" + name + "/__init__.py") if p in self.manifest]
        return matches[0] if len(set(matches)) == 1 else None

    def resolve(self, path, value, seen=()):
        if len(seen) >= 32:
            return None
        if isinstance(value, ast.Constant) and type(value.value) is int:
            return ("integer", value.value)
        if isinstance(value, ast.Attribute):
            base = self.resolve(path, value.value, seen)
            if base and base[0] == "module":
                return self.resolve(base[1], ast.Name(id=value.attr), seen)
            return None
        if not isinstance(value, ast.Name) or (path, value.id) in seen:
            return None
        tree = self.tree(path)
        if tree is None:
            return None
        seen = (*seen, (path, value.id))
        matches = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)):
                if any(isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == value.id for n in ast.walk(node)):
                    return None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == value.id:
                matches.append(("function", path, node))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == value.id for t in targets):
                    matches.append(self.resolve(path, node.value, seen))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if (alias.asname or alias.name.split(".")[0]) == value.id:
                        target = self.module((alias.name if alias.asname else alias.name.split(".")[0]).split("."))
                        matches.append(("module", target) if target else None)
            elif isinstance(node, ast.ImportFrom):
                if any(a.name == "*" for a in node.names):
                    return None
                for alias in node.names:
                    if (alias.asname or alias.name) != value.id:
                        continue
                    parts = list(PurePosixPath(path).parent.parts) if node.level else []
                    if node.level > len(parts) + 1:
                        return None
                    if node.level > 1:
                        parts = parts[:len(parts) - node.level + 1]
                    parts += node.module.split(".") if node.module else []
                    module = self.module(parts)
                    submodule = self.module(parts + [alias.name])
                    resolved = self.resolve(module, ast.Name(id=alias.name), seen) if module else None
                    matches.append(resolved or (("module", submodule) if submodule else None))
        return matches[0] if len(matches) == 1 else None

    def function(self, span):
        source = span.get("source", {})
        resolved = self.resolve(source.get("path", ""), ast.Name(id=source.get("function", "")))
        if resolved and resolved[0] == "function":
            node = resolved[2]
            if source.get("line") and not node.lineno <= source["line"] <= node.end_lineno:
                return None
            return resolved[1], node
        return None

    def budget(self, path, function, bound, span):
        if isinstance(bound, ast.Name):
            if any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == bound.id for n in ast.walk(function)):
                return None
            parameters = {a.arg for a in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
            if bound.id in parameters:
                return span.get("input", {}).get(bound.id) if isinstance(span.get("input"), dict) else None
        value = self.resolve(path, bound)
        return value[1] if value and value[0] == "integer" else None


def _direct_calls(body):
    calls = []
    for statement in body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)):
            value = statement.value
            value = value.value if isinstance(value, ast.Await) else value
            if isinstance(value, ast.Call):
                calls.append(value)
    return calls


def _edge_budget(code, parent, children):
    located = code.function(parent)
    if not located:
        return None
    path, function = located
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
              ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    if any(isinstance(n, nested) for s in function.body for n in ast.walk(s)):
        return None
    if any(isinstance(n, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Delete))
           or isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, ast.Store)
           for n in ast.walk(function)):
        return None
    child = children[0]
    signature = lambda s: (s["kind"], s.get("tool")) if s["kind"] == "tool" else (s["kind"], s.get("source", {}).get("path"), s.get("source", {}).get("function"))
    if any(signature(c) != signature(child) for c in children):
        return None
    parameters = {a.arg for a in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
    stores = {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}

    def matches(call):
        base = call.func
        while isinstance(base, ast.Attribute):
            base = base.value
        if not isinstance(base, ast.Name) or base.id in parameters | stores:
            return False
        name = call.func.id if isinstance(call.func, ast.Name) else call.func.attr
        resolved = code.resolve(path, call.func)
        if child["kind"] == "tool":
            # Recorder source can describe either the caller or the tool body.
            source = child.get("source", {})
            if source.get("function") == child["tool"]:
                return bool(resolved and resolved[0] == "function" and resolved[1] == source["path"] and resolved[2].name == source["function"])
            return name == child["tool"]
        target = code.function(child)
        return bool(target and resolved and resolved[0] == "function" and (resolved[1], resolved[2]) == target)

    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call) and matches(n)]
    if len(calls) != 1:
        return None
    call = calls[0]
    loops = [n for n in ast.walk(function) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))]
    if not loops:
        return (1, call.lineno, "single_call") if call in _direct_calls(function.body) else None
    if len(loops) != 1 or loops[0] not in function.body:
        return None
    loop = loops[0]
    if loop.orelse or call not in _direct_calls(loop.body):
        return None
    # Allow a stop only on the just-returned child's pending signal, with
    # evidence from every child that this branch was not taken.
    assignments = [s for s in loop.body if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                   and (s.value.value if isinstance(s.value, ast.Await) else s.value) is call]
    result_name = assignments[0].targets[0].id if len(assignments) == 1 else None
    allowed_breaks = set()
    for statement in loop.body:
        if isinstance(statement, ast.If):
            test = statement.test
            if (len(statement.body) != 1 or not isinstance(statement.body[0], ast.Break) or statement.orelse
                    or not isinstance(test, ast.Subscript) or not isinstance(test.value, ast.Name)
                    or test.value.id != result_name or not isinstance(test.slice, ast.Constant)
                    or test.slice.value not in ("done", "completed") or statement.lineno <= call.lineno
                    or any(not isinstance(c.get("output"), dict) or c["output"].get(test.slice.value) is not False for c in children)):
                return None
            allowed_breaks.add(statement.body[0])
    if any(isinstance(n, (ast.Continue, ast.Return)) or isinstance(n, ast.Break) and n not in allowed_breaks for n in ast.walk(loop)):
        return None
    if isinstance(loop, ast.For) and isinstance(loop.iter, ast.Call) and isinstance(loop.iter.func, ast.Name) and loop.iter.func.id == "range" and len(loop.iter.args) == 1 and not loop.iter.keywords:
        if "range" in parameters | stores or code.resolve(path, ast.Name(id="range")) is not None:
            return None
        bound = loop.iter.args[0]
    elif isinstance(loop, ast.While) and isinstance(loop.test, ast.Compare) and len(loop.test.ops) == 1 and isinstance(loop.test.ops[0], ast.Lt) and isinstance(loop.test.left, ast.Name):
        counter = loop.test.left.id
        bound = loop.test.comparators[0]
        seeds = [n for n in function.body[:function.body.index(loop)] if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == counter and isinstance(n.value, ast.Constant) and type(n.value.value) is int and n.value.value == 0]
        increments = [n for n in loop.body if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == counter and isinstance(n.op, ast.Add) and isinstance(n.value, ast.Constant) and type(n.value.value) is int and n.value.value == 1]
        writes = [n for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == counter]
        if len(seeds) != 1 or len(increments) != 1 or len(writes) != 2:
            return None
    else:
        return None
    budget = code.budget(path, function, bound, parent)
    return (budget, call.lineno, "exhausted_loop") if type(budget) is int and budget > 0 else None


def bounded_pending_chain(group: list[dict], spans: list[dict], project: Path, manifest: dict) -> dict | None:
    if not group or any(s.get("error") or not _pending(s.get("output")) for s in group):
        return None
    run_id = group[0]["run_id"]
    if any(s["run_id"] != run_id for s in group):
        return None
    run = [s for s in spans if s["run_id"] == run_id]
    by_id = {s["span_id"]: s for s in run}
    if len(by_id) != len(run) or any(s.get("task_status") == "failed" or any(not a["passed"] for a in s.get("assertions", [])) for s in run):
        return None
    edges, included, roots = defaultdict(set), set(), set()
    for leaf in group:
        span, chain = leaf, set()
        while True:
            key = span["span_id"]
            if key in chain or len(chain) >= 128:
                return None
            chain.add(key)
            included.add(key)
            parent_id = span.get("parent_id")
            if not parent_id:
                roots.add(key)
                break
            parent = by_id.get(parent_id)
            if not parent or parent.get("error") or parent.get("output") is None:
                return None
            edges[parent_id].add(key)
            span = parent
    if len(roots) != 1 or not edges:
        return None
    code, budgets = _Code(project, manifest), []
    for parent_id, child_ids in edges.items():
        parent = by_id[parent_id]
        children = [s for s in run if s["span_id"] in child_ids]
        proof = _edge_budget(code, parent, children)
        if proof is None or proof[0] != len(children):
            return None
        budget, line, basis = proof
        budgets.append({"parent": evidence_id(parent), "children": [evidence_id(c) for c in children],
                        "source": {"path": parent["source"]["path"], "line": line},
                        "expected_calls": budget, "observed_calls": len(children), "basis": basis})
    if not any(b["basis"] == "exhausted_loop" for b in budgets):
        return None
    return {"root": by_id[next(iter(roots))], "spans": [s for s in run if s["span_id"] in included], "budgets": budgets}


def bounded_pending_parent(group: list[dict], spans: list[dict], project: Path, manifest: dict) -> dict | None:
    """Compatibility wrapper; the returned root can be a distant ancestor."""
    proof = bounded_pending_chain(group, spans, project, manifest)
    return proof["root"] if proof else None
