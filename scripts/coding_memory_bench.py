from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.coding_memory_bench import CodingMemoryBench


def main() -> int:
    parser = argparse.ArgumentParser(description="Run blind CodingMemoryBench V2.1")
    parser.add_argument("--output-dir", type=Path, default=Path("docs/verification/v2.1"))
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    benchmark = CodingMemoryBench()
    report = benchmark.run()
    paths = benchmark.write(report, args.output_dir)
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"CodingMemoryBench: {paths['json']}")
    print(f"Measured gates passed: {report['all_measured_gates_passed']}")
    return 0 if report["all_measured_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
