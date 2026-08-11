from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from memoryos.evaluation.real_workload_agent import (
    AgentEvidenceType,
    AgentRuntimeSpec,
    CredentialMountSpec,
    NetworkAccess,
)
from memoryos.evaluation.real_workload_containers import default_container_user

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


def _image_id(docker: str, reference: str) -> str:
    inspected = subprocess.run(
        [docker, "image", "inspect", reference, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    if not inspected.startswith("sha256:") or len(inspected) != 71:
        raise SystemExit(f"Docker returned an invalid image id for {reference}")
    return inspected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the isolated Codex real-workload agent image and runtime JSON."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/real-workload/codex-runtime.json"),
    )
    parser.add_argument("--tag", default="memoryos-real-workload-codex:local")
    parser.add_argument("--mcp-image", default="memoryos-real-workload-fixture:local")
    parser.add_argument("--codex-version", default="0.147.0")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max", "ultra"],
        default="max",
    )
    parser.add_argument("--service-tier", choices=["default", "priority"], default="priority")
    parser.add_argument("--auth-environment", default="MEMORYOS_CODEX_AUTH_FILE")
    arguments = parser.parse_args()
    if not _VERSION.fullmatch(arguments.codex_version):
        parser.error("--codex-version must be an exact semantic version")
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required")
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            docker,
            "build",
            "--pull=false",
            "--provenance=false",
            "--build-arg",
            f"CODEX_VERSION={arguments.codex_version}",
            "--file",
            str(project_root / "docker" / "real-workload-codex" / "Dockerfile"),
            "--tag",
            arguments.tag,
            str(project_root),
        ],
        check=True,
    )
    user = default_container_user()
    runtime = AgentRuntimeSpec(
        image=_image_id(docker, arguments.tag),
        mcp_image=_image_id(docker, arguments.mcp_image),
        command=[
            "python3",
            "/opt/benchmark-agent/codex_benchmark_agent.py",
            "--workspace",
            "{workspace}",
            "--prompt",
            "{prompt_file}",
            "--mcp-config",
            "{mcp_config}",
            "--result",
            "{result_file}",
            "--auth-file",
            "/run/credentials/codex-auth.json",
            "--model",
            arguments.model,
            "--reasoning-effort",
            arguments.reasoning_effort,
            "--service-tier",
            arguments.service_tier,
            "--timeout-seconds",
            "1740",
        ],
        provider="openai-codex-chatgpt",
        model=arguments.model,
        agent_version=f"codex-cli/{arguments.codex_version}+app-server-adapter/4",
        evidence_type=AgentEvidenceType.REAL_CODING_AGENT,
        credential_mounts=[
            CredentialMountSpec(
                source_environment=arguments.auth_environment,
                destination="/run/credentials/codex-auth.json",
            )
        ],
        network_access=NetworkAccess.INTERNET,
        allow_unconfined_seccomp_for_nested_sandbox=True,
        user=user,
        mcp_user=user,
        scoring_user=user,
        timeout_seconds=1800,
    )
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(runtime.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(output)


if __name__ == "__main__":
    main()
