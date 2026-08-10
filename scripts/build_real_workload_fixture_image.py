from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from memoryos.evaluation.real_workload_containers import default_container_user


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the infrastructure-only real-workload fixture image and runtime JSON."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/real-workload/fixture-runtime.json"),
    )
    parser.add_argument("--tag", default="memoryos-real-workload-fixture:local")
    arguments = parser.parse_args()
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required")
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            docker,
            "build",
            "--pull=false",
            "--file",
            str(project_root / "docker" / "real-workload" / "Dockerfile"),
            "--tag",
            arguments.tag,
            str(project_root),
        ],
        check=True,
    )
    inspected = subprocess.run(
        [docker, "image", "inspect", arguments.tag, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not inspected.startswith("sha256:") or len(inspected) != 71:
        raise SystemExit("Docker returned an invalid image id")
    payload = {
        "image": inspected,
        "mcp_image": inspected,
        "command": [
            "python",
            "-m",
            "memoryos.evaluation.fixture_agent",
            "--strategy",
            "markupsafe-deprecation",
            "--workspace",
            "{workspace}",
            "--prompt",
            "{prompt_file}",
            "--mcp-config",
            "{mcp_config}",
            "--result",
            "{result_file}",
        ],
        "provider": "deterministic_fixture",
        "model": "none",
        "agent_version": "1.0",
        "evidence_type": "deterministic_fixture",
        "environment_variables": [],
        "network_access": "internal",
        "user": default_container_user(),
        "mcp_user": default_container_user(),
        "scoring_user": default_container_user(),
    }
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
