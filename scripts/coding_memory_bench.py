from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from memoryos.evaluation import CodingMemoryBench, ProductionCodingMemoryBench


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run CodingMemoryBench fixture and/or production-path integration suites"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("docs/verification/v2.1"))
    parser.add_argument(
        "--suite",
        choices=("fixture", "production", "both"),
        default="fixture",
        help="Run the deterministic fixture, the local production-path integration, or both.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Fresh database directory for --suite production/both; temporary by default.",
    )
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    passed = True
    if args.suite in {"fixture", "both"}:
        benchmark = CodingMemoryBench()
        fixture_report = benchmark.run()
        fixture_paths = benchmark.write(fixture_report, args.output_dir)
        if args.print_json:
            print(json.dumps(fixture_report, ensure_ascii=False, indent=2))
        print(f"CodingMemoryBench fixture: {fixture_paths['json']}")
        print(f"Fixture gates passed: {fixture_report['all_measured_gates_passed']}")
        passed = passed and bool(fixture_report["all_measured_gates_passed"])

    if args.suite in {"production", "both"}:
        production = ProductionCodingMemoryBench()
        if args.data_dir is not None:
            production_report = production.run(args.data_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="memoryos-production-bench-") as directory:
                production_report = production.run(Path(directory))
        production_paths = production.write(production_report, args.output_dir)
        if args.print_json:
            print(json.dumps(production_report, ensure_ascii=False, indent=2))
        print(f"CodingMemoryBench production path: {production_paths['json']}")
        production_passed = all(production_report["gates"].values())
        print(f"Production-path gates passed: {production_passed}")
        passed = passed and production_passed
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
