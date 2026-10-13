"""Archive a completed benchmark, preserving model requests and acceptance data."""
from __future__ import annotations

import argparse
from pathlib import Path

from agentdoctor.core import digest, read_json, write_json


def archive(source: Path, destination: Path):
    if destination.exists():
        raise SystemExit("Refusing to overwrite an archived measurement")
    report = read_json(source, limit=50_000_000)
    report["source_report"] = str(source)
    report["source_report_sha256"] = digest(source.read_bytes())
    report["implementation_manifest"] = read_json(source.parent / "implementation-manifest.json")
    for row in report["cases"]:
        if not row.get("engine_report"):
            continue
        engine_path = Path(row["engine_report"])
        engine = read_json(engine_path, limit=50_000_000)
        row["engine_report_sha256"] = digest(engine_path.read_bytes())
        row["engine"] = engine
        row["traces"] = read_json(engine_path.parent / "traces.redacted.json", limit=20_000_000)
        row["model_requests"] = []
        for event in engine.get("model_events", []):
            if event.get("request_file"):
                request = Path(event["request_file"])
                if digest(request.read_bytes()) != event["request_sha256"]:
                    raise SystemExit("Model request hash mismatch")
                row["model_requests"].append(read_json(request, limit=20_000_000))
        row["patches"] = [{"number": a["number"], "diff": Path(a["patch"]).read_text(encoding="utf-8")}
                          for a in engine.get("attempts", []) if a.get("patch")]
    write_json(destination, report)
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    archive(args.source, args.destination)
