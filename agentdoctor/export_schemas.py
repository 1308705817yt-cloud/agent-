"""Generate public JSON Schemas from the same definitions used at runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import CONFIG_SCHEMA, TRACE_SCHEMA


def public_schemas() -> dict[str, dict]:
    return {filename: {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
            for filename, schema in (("doctor.schema.json", CONFIG_SCHEMA), ("trace.schema.json", TRACE_SCHEMA))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("schemas"))
    parser.add_argument("--check", action="store_true", help="Fail if public schemas need regeneration; do not write files")
    args = parser.parse_args(argv)
    stale = []
    for filename, schema in public_schemas().items():
        path = args.output / filename
        expected = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                stale.append(filename)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    if stale:
        print("Public schemas are stale: " + ", ".join(stale))
        print("Run: python -m agentdoctor.export_schemas")
        return 1
    print("Public schemas match runtime definitions" if args.check else "Public schemas generated from agentdoctor.contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
