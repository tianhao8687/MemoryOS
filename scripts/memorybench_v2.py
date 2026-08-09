from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation import MemoryBenchV2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the frozen MemoryBench V2 protocol")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/verification/v2"),
        help="Directory for JSON and HTML reports",
    )
    parser.add_argument(
        "--skip-performance",
        action="store_true",
        help="Skip the measured 100k FTS5 performance suite (development only)",
    )
    parser.add_argument("--print-json", action="store_true", help="Print the full report")
    args = parser.parse_args()
    runner = MemoryBenchV2()
    report = runner.run(include_performance=not args.skip_performance)
    paths = runner.write(report, args.output_dir)
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"MemoryBench V2 report: {paths['json']}")
    print(f"MemoryBench V2 dashboard: {paths['html']}")
    print(f"Measured gates passed: {report['release_gates']['measured_all_passed']}")
    print("Real-model Agent A/B: external blocker (fixture is harness-only)")
    return 0 if report["release_gates"]["measured_all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
