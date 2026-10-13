from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


class DoctorError(Exception):
    """A user-actionable input, policy, or execution error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _reject_constant(value: str) -> None:
    raise DoctorError(f"Non-finite JSON number is not allowed: {value}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DoctorError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(text: str) -> Any:
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        raise DoctorError(f"Invalid JSON: {exc}") from exc


def read_json(path: Path, limit: int = 2_000_000) -> Any:
    if path.stat().st_size > limit:
        raise DoctorError(f"JSON file exceeds {limit} bytes: {path.name}")
    return parse_json(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_text(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def normalize_path(relative: str) -> str:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise DoctorError("Paths must be nonempty, relative, and use forward slashes")
    path = PurePosixPath(relative)
    if path.is_absolute() or PureWindowsPath(relative).drive or any(p in ("..", ".") for p in relative.split("/")):
        raise DoctorError(f"Unsafe relative path: {relative}")
    if any(":" in p or p.endswith((".", " ")) for p in path.parts):
        raise DoctorError(f"Unsafe path component: {relative}")
    return path.as_posix()


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def contained_path(root: Path, relative: str) -> Path:
    relative = normalize_path(relative)
    root = root.resolve()
    path = root / relative
    current = root
    for component in PurePosixPath(relative).parts:
        current = current / component
        if is_link(current):
            raise DoctorError(f"Symlink/junction is not allowed: {relative}")
    if not path.resolve().is_relative_to(root):
        raise DoctorError(f"Path escapes project: {relative}")
    return path


_SENSITIVE_KEY = re.compile(r"(^|[_-])(api[_-]?key|token|password|secret|authorization|cookie|credential)(s|$)", re.I)
_SECRET_TEXT = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/-]{8,})", re.I)
_ASSIGNMENT = re.compile(r"(?i)((?:api[_-]?key|password|secret|access_token)\s*[=:]\s*['\"]?)([^\s'\",;]+)")
_METRIC_KEYS = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens", "cached_input_tokens", "reasoning_tokens", "max_output_tokens", "prompt_tokens", "completion_tokens"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if str(k).lower() not in _METRIC_KEYS and _SENSITIVE_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _ASSIGNMENT.sub(r"\1[REDACTED]", _SECRET_TEXT.sub("[REDACTED]", value))
    return value
