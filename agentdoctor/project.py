from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .contracts import CONFIG_SCHEMA, check_schema, validate
from .core import DoctorError, contained_path, digest, is_link, normalize_path, read_json
from .visible_cases import visible_cases

IGNORED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".agentdoctor", ".pytest_cache", ".mypy_cache", "build", "dist", ".aws", ".ssh", ".codex", ".agents"}
SECRET_NAMES = {"credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", ".npmrc", ".pypirc"}


def private_path(relative: str) -> bool:
    parts = Path(relative).parts
    return any(p.lower() in IGNORED_DIRS or p.lower() in SECRET_NAMES or p.lower().startswith(".env") for p in parts) or Path(relative).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}


def matches(relative: str, patterns: list[str]) -> bool:
    relative = relative.casefold()
    return any(relative == p.rstrip("/").casefold() or relative.startswith(p.rstrip("/").casefold() + "/") or fnmatch.fnmatchcase(relative, p.casefold()) for p in patterns)


def _module_paths(project: Path, selector: str) -> set[str]:
    """Resolve local module/test selectors without importing target code.

    Protect the selected package subtree and executable ancestor __init__.py
    files. Both root and conventional src layouts are inspected conservatively.
    Dotted unittest class/method selectors resolve to their longest file prefix.
    """
    parts = selector.split(".")
    if not parts or not all(part.isidentifier() for part in parts):
        return set()
    protected = set()
    for prefix in ("", "src/"):
        for length in range(len(parts), 0, -1):
            relative = prefix + "/".join(parts[:length])
            module = contained_path(project, relative + ".py")
            package = contained_path(project, relative)
            if module.is_file():
                protected.add(relative + ".py")
            elif package.is_dir():
                protected.add(relative)
            else:
                continue
            for ancestor in range(1, length):
                initializer = prefix + "/".join(parts[:ancestor]) + "/__init__.py"
                if contained_path(project, initializer).is_file():
                    protected.add(initializer)
            break
    return protected


def _validation_paths(project: Path, argv: list[str]) -> set[str]:
    protected = set()
    for argument in argv[1:]:
        if argument.startswith("-") or "{" in argument:
            continue
        # Also handle pytest node IDs and unittest dotted module/class selectors.
        selector = argument.split("::", 1)[0]
        try:
            path = contained_path(project, selector)
        except DoctorError:
            continue
        if path.is_file() or path.is_dir():
            protected.add(selector)
        protected.update(_module_paths(project, selector))
    return protected


def load_config(project: Path, relative: str = "doctor.json") -> dict:
    config = read_json(contained_path(project, relative))
    validate(config, CONFIG_SCHEMA, "doctor.json")
    config.setdefault("checks", {})
    config["checks"].setdefault("loop_threshold", 3)
    config["checks"].setdefault("preserve_state_keys", [])
    policy = config.setdefault("policy", {})
    for name, default in {"max_candidates": 3, "max_changed_files": 3, "max_changed_lines": 120, "max_model_calls": 8, "max_context_chars": 48000, "protected_paths": []}.items():
        policy.setdefault(name, default)
    policy["protected_paths"] = sorted(set(policy["protected_paths"] + [relative, "tests", "test_*", "**/test_*", "checks.py"]))
    for name in ("source_paths",):
        for pattern in config[name]:
            normalize_path(pattern.rstrip("/"))
    for pattern in policy["protected_paths"]:
        normalize_path(pattern.rstrip("/"))
    names = set()
    for command in config["validation"]["commands"]:
        if command["name"] in names:
            raise DoctorError("Validation command names must be unique")
        names.add(command["name"])
        command.setdefault("timeout_seconds", 30)
        policy["protected_paths"].extend(_validation_paths(project, command["argv"]))
        if "replay" in command:
            relative_cases = normalize_path(command["replay"]["cases_file"])
            if private_path(relative_cases):
                raise DoctorError("Replay cases must be an ordinary visible project file")
            visible_cases(project, command)
            policy["protected_paths"].append(relative_cases)
    policy["protected_paths"] = sorted(set(policy["protected_paths"]))
    config["validation"].setdefault("repetitions", 2)
    config["validation"].setdefault("docker_image", "python:3.11-slim")
    for tool in config["tools"].values():
        check_schema(tool["input_schema"])
        tool.setdefault("aliases", {})
        properties = tool["input_schema"].get("properties", {}) if isinstance(tool["input_schema"], dict) else {}
        for old, new in tool["aliases"].items():
            if old == new or new not in properties or old in properties:
                raise DoctorError("Tool aliases must map an obsolete key to a declared current property")
    return config


def editable(relative: str, config: dict) -> bool:
    relative = normalize_path(relative)
    return not private_path(relative) and matches(relative, config["source_paths"]) and not matches(relative, config["policy"]["protected_paths"])


def _project_files(project: Path, excluded: list[Path] | None = None):
    """Single inclusion policy shared by snapshots and integrity rescans."""
    project = project.resolve()
    if not project.is_dir():
        raise DoctorError("Project directory is unavailable")
    excluded_roots = [p.resolve() for p in (excluded or [])]
    def fail(error):
        raise DoctorError(f"Cannot inventory project: {error}") from error
    for directory, directories, files in os.walk(project, followlinks=False, onerror=fail):
        folder = Path(directory)
        retained = []
        for name in sorted(directories):
            child = folder / name
            relative = child.relative_to(project).as_posix()
            if private_path(relative) or any(child.absolute().is_relative_to(p) for p in excluded_roots):
                continue
            if is_link(child):
                raise DoctorError(f"Inventory refuses symlink/junction: {relative}")
            retained.append(name)
        directories[:] = retained
        for name in sorted(files):
            original = folder / name
            relative = original.relative_to(project).as_posix()
            if private_path(relative) or name.endswith((".pyc", ".pyo")) or any(original.absolute().is_relative_to(p) for p in excluded_roots):
                continue
            if is_link(original) or not original.is_file():
                raise DoctorError(f"Inventory refuses non-regular file: {relative}")
            yield relative, original


def snapshot(project: Path, destination: Path, excluded: list[Path] | None = None) -> dict[str, str]:
    """Copy ordinary files only; never import or execute the inspected project."""
    project, destination = project.resolve(), destination.resolve()
    if destination == project or project.is_relative_to(destination):
        raise DoctorError("Snapshot destination must not contain the source project")
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    manifest = {}
    for relative, original in _project_files(project, [*(excluded or []), destination]):
        size = original.stat().st_size
        total += size
        if size > 2_000_000 or total > 25_000_000 or len(manifest) >= 2000:
            raise DoctorError("Project snapshot limit exceeded (2 MB/file, 25 MB total, 2000 files); provide a smaller project")
        content = original.read_bytes()
        target = contained_path(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest[relative] = digest(content)
    return manifest


def check_manifest(project: Path, manifest: dict[str, str], *, excluded: list[Path] | None = None) -> list[str]:
    changed, seen = set(), set()
    try:
        for relative, path in _project_files(project, excluded):
            seen.add(relative)
            if relative not in manifest or path.stat().st_size > 2_000_000:
                changed.add(relative)
            elif digest(path.read_bytes()) != manifest[relative]:
                changed.add(relative)
        changed.update(set(manifest) - seen)
    except (DoctorError, OSError) as exc:
        # An incomplete inventory must fail closed, never masquerade as no changes.
        changed.add(f"<inventory unavailable: {exc}>")
    return sorted(changed)
