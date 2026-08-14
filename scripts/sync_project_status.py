from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

READINESS_START = "<!-- MEMORYOS:READINESS:START -->"
READINESS_END = "<!-- MEMORYOS:READINESS:END -->"
DEFAULT_DOCUMENTS = ("README.md", "PROJECT_STATUS.md")


def load_readiness(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("readiness registry must be a JSON object")
    gates = value.get("gates")
    blockers = value.get("blockers")
    if not isinstance(gates, dict) or not isinstance(blockers, list):
        raise ValueError("readiness registry is missing gates or blockers")
    if not all(isinstance(item, str) and item for item in blockers):
        raise ValueError("readiness blockers must be non-empty strings")
    return value


def render_readiness_block(readiness: dict[str, Any]) -> str:
    gates = readiness["gates"]
    blockers = readiness["blockers"]
    blocker_lines = "\n".join(f"{index}. {item}" for index, item in enumerate(blockers, 1))
    profile_state = "active" if readiness.get("production_profile_active") else "inactive"
    frozen_state = "yes" if readiness.get("production_weights_frozen") else "no"
    approved_state = "yes" if gates.get("promotion_approved") else "no"
    return "\n".join(
        (
            READINESS_START,
            "### AI calibration readiness (自动生成)",
            "",
            "> 单一事实源: `benchmarks/ai_calibration_v1/readiness.json`。请运行 "
            "`python scripts/sync_project_status.py --write` 更新本段; 不要手工修改。",
            "",
            f"- 状态: `{readiness.get('status', 'unknown')}`",
            f"- 生产 profile: `{profile_state}`; 生产权重冻结: `{frozen_state}`",
            f"- 有效 real-agent 配对: `{gates.get('real_agent_ablation_pairs', 0)}`",
            "- AI Jury 有效覆盖: "
            f"`{gates.get('effective_jury_model_families', 0)}` 个模型家族 / "
            f"`{gates.get('effective_jury_providers', 0)}` 个 provider",
            "- Sealed promotion: "
            f"`{gates.get('sealed_promotion_tasks', 0)}` tasks / "
            f"`{gates.get('sealed_promotion_repositories', 0)}` repositories / "
            f"`{gates.get('sealed_promotion_sequences', 0)}` sequences; "
            f"批准: `{approved_state}`",
            "",
            "当前阻塞:",
            "",
            blocker_lines,
            READINESS_END,
        )
    )


def replace_readiness_block(text: str, block: str) -> str:
    pattern = re.compile(
        rf"{re.escape(READINESS_START)}.*?{re.escape(READINESS_END)}",
        re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError("document must contain exactly one readiness marker block")
    return pattern.sub(block, text, count=1)


def sync_documents(
    readiness_path: Path,
    document_paths: list[Path],
    *,
    write: bool,
) -> list[Path]:
    block = render_readiness_block(load_readiness(readiness_path))
    drifted: list[Path] = []
    for path in document_paths:
        original = path.read_text(encoding="utf-8")
        expected = replace_readiness_block(original, block)
        if original == expected:
            continue
        drifted.append(path)
        if write:
            path.write_text(expected, encoding="utf-8")
    return drifted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize generated project-readiness blocks from the canonical registry."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Rewrite drifted marker blocks.")
    mode.add_argument("--check", action="store_true", help="Fail when marker blocks are stale.")
    parser.add_argument(
        "--readiness",
        type=Path,
        default=Path("benchmarks/ai_calibration_v1/readiness.json"),
    )
    parser.add_argument("documents", nargs="*", type=Path)
    args = parser.parse_args()
    documents = args.documents or [Path(item) for item in DEFAULT_DOCUMENTS]
    drifted = sync_documents(args.readiness, documents, write=args.write)
    if args.check and drifted:
        print("Readiness status drift detected: " + ", ".join(str(path) for path in drifted))
        return 1
    if args.write:
        print("Updated: " + (", ".join(str(path) for path in drifted) if drifted else "none"))
    else:
        print("Readiness status blocks are synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
