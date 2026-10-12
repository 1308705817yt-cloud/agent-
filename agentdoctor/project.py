from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .contracts import CONFIG_SCHEMA, check_schema, validate
from .core import DoctorError, contained_path, digest, is_link, normalize_path, read_json

IGNORED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".agentdoctor", ".pytest_cache", ".mypy_cache", "build", "dist", ".aws", ".ssh", ".codex", ".agents"}
SECRET_NAMES = {"credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", ".npmrc", ".pypirc"}


def private_path(relative: str) -> bool:
    parts = Path(relative).parts
    return any(p.lower() in IGNORED_DIRS or p.lower() in SECRET_NAMES or p.lower().startswith(".env") for p in parts) or Path(relative).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}


def matches(relative: str, patterns: list[str]) -> bool:
    relative = relative.casefold()
    return any(relative == p.rstrip("/").casefold() or relative.startswith(p.rstrip("/").casefold() + "/") or fnmatch.fnmatchcase(relative, p.casefold()) for p in patterns)


def load_config(project: Path, relative: str = "doctor.json") -> dict:
    config = read_json(contained_path(project, relative))
    validate(config, CONFIG_SCHEMA, "doctor.json")
    config.setdefault("checks", {})
    config["checks"].setdefault("loop_threshold", 3)
    config["checks"].setdefault("preserve_state_keys", [])
    policy = config.setdefault("policy", {})
    for name, default in {"max_candidates": 3, "max_changed_files": 3, "max_changed_lines": 120, "max_model_calls": 8, "max_context_chars": 48000, "protected_paths": []}.items():
        policy.setdefault(name, default)
    policy["protected_paths"] = list(set(policy["protected_paths"] + [relative, "tests", "test_*", "**/test_*", "checks.py"]))
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
        # Freeze file arguments, including custom-named evaluation scripts.
        for argument in command["argv"][1:]:
            if argument.startswith("-") or "{" in argument:
                continue
            try:
                path = contained_path(project, argument)
            except DoctorError:
                continue
            if path.is_file():
                policy["protected_paths"].append(argument)
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


def snapshot(project: Path, destination: Path, excluded: list[Path] | None = None) -> dict[str, str]:
    """Copy ordinary files only; never import or execute the inspected project."""
    project = project.resolve()
    destination = destination.resolve()
    if destination == project or project.is_relative_to(destination):
        raise DoctorError("Snapshot destination must not contain the source project")
    excluded_roots = [p.resolve() for p in (excluded or [])] + [destination]
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    manifest = {}
    for directory, directories, files in os.walk(project, followlinks=False):
        folder = Path(directory)
        retained = []
        for name in sorted(directories):
            child = folder / name
            relative = child.relative_to(project).as_posix()
            if private_path(relative) or any(child.resolve().is_relative_to(p) for p in excluded_roots):
                continue
            if is_link(child):
                raise DoctorError(f"Snapshot refuses symlink/junction: {relative}")
            retained.append(name)
        directories[:] = retained
        for name in sorted(files):
            original = folder / name
            relative = original.relative_to(project).as_posix()
            if private_path(relative) or name.endswith((".pyc", ".pyo")) or any(original.resolve().is_relative_to(p) for p in excluded_roots):
                continue
            if is_link(original) or not original.is_file():
                raise DoctorError(f"Snapshot refuses non-regular file: {relative}")
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


def check_manifest(project: Path, manifest: dict[str, str]) -> list[str]:
    changed = []
    for relative, expected in manifest.items():
        try:
            path = contained_path(project, relative)
            if not path.is_file() or digest(path.read_bytes()) != expected:
                changed.append(relative)
        except DoctorError:
            changed.append(relative)
    return changed
