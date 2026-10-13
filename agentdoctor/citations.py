"""Resolve citations only inside supplied source excerpts, never by fuzzy search."""
from __future__ import annotations

import copy
import json
import re

from .core import DoctorError, digest


def source_id(path: str, sha256: str, start: int, end: int) -> str:
    return "src_" + digest(json.dumps([path, sha256, start, end], ensure_ascii=False))[:24]


def _physical_lines(text: str) -> list[str]:
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text)
    return lines[:-1] if lines and lines[-1] == "" else lines


def _line_bodies(text: str) -> list[str]:
    # Ignore physical line-ending representation and ONE terminal line ending.
    # Indentation, spaces, empty lines, string contents and Unicode stay exact.
    return [line.removesuffix("\n").removesuffix("\r") for line in _physical_lines(text)]


def checked_source(quote: dict, manifest: dict, sources: list[dict]) -> tuple[dict, dict]:
    if not quote["text"].strip():
        raise DoctorError("Source citation must contain non-whitespace source text")
    anchored = "source_id" in quote
    matches = []
    if anchored:
        entries = [s for s in sources if s.get("source_id") == quote["source_id"]]
        if len(entries) != 1:
            raise DoctorError("Source citation uses an unknown or ambiguous supplied source_id")
    else:
        if quote["end_line"] < quote["start_line"]:
            raise DoctorError("Root cause cites an invalid line range")
        entries = [s for s in sources if s["path"] == quote["path"]
                   and s["start_line"] <= quote["start_line"] <= quote["end_line"] <= s["end_line"]]
    for source in entries:
        path = source["path"]
        if path not in manifest or source["sha256"] != manifest[path]:
            continue
        identifier = source_id(path, source["sha256"], source["start_line"], source["end_line"])
        if anchored and identifier != quote["source_id"]:
            continue
        raw = _physical_lines(source["text"])
        bodies, wanted = _line_bodies(source["text"]), _line_bodies(quote["text"])
        if len(raw) != source["end_line"] - source["start_line"] + 1:
            continue
        if anchored:
            offsets = range(len(raw) - len(wanted) + 1)
        else:
            if len(wanted) != quote["end_line"] - quote["start_line"] + 1:
                continue
            offsets = [quote["start_line"] - source["start_line"]]
        for offset in offsets:
            if bodies[offset:offset + len(wanted)] == wanted:
                start = source["start_line"] + offset
                matches.append({"path": path, "start_line": start, "end_line": start + len(wanted) - 1,
                                "text": "".join(raw[offset:offset + len(wanted)]), "sha256": manifest[path], "source_id": identifier})
    if len(matches) != 1:
        raise DoctorError("Root cause source must match one unique contiguous excerpt supplied to the model; line contents and ranges must agree")
    canonical = matches[0]
    audit = {"protocol": "source_id_quote" if anchored else "legacy_range_quote",
             "normalization": "none" if canonical["text"] == quote["text"] else "line_endings",
             "submitted_source": copy.deepcopy(quote)}
    return canonical, audit
