"""Additional predeclared long-feedback boundary probe, separate from the first three."""
from pathlib import Path

from agentdoctor.core import digest
from benchmarks import supplemental_eval as evaluation


def main():
    cases = evaluation.retry_cases()
    case = next(c for c in cases if c["id"] == "dependency-near-budget-v052")
    case["id"] = "dependency-long-feedback"
    case["edit"]["after"] = 'print("retry-diagnostic " * 256)\n    return {"quantity": quantity + 1}'
    case["provenance"].update(stress_driver_sha256=digest(Path(__file__).read_bytes()),
                              stress="Known wrong candidate adds 4097 stdout characters per tool call; no validation protocol changes")
    evaluation.retry_cases = lambda: [case]
    evaluation.run_group("retry", evaluation.ROOT / "artifacts/supplemental-v053-long-feedback")


if __name__ == "__main__":
    main()
