from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from memoryos.evaluation.openai_compatible_coding_agent import OpenAICompatibleAgentRuntime

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the optional Qwen coding-agent toolchain image and freeze its lock."
    )
    parser.add_argument("--runtime-template", type=Path, default=Path("runtime/qwen-local.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("build/context-efficiency/qwen-runtime.json")
    )
    parser.add_argument(
        "--lock", type=Path, default=Path("build/context-efficiency/qwen-image-lock.json")
    )
    parser.add_argument("--tag", default="memoryos-context-efficiency-qwen:local")
    parser.add_argument("--transformers-version", default="4.51.3")
    arguments = parser.parse_args()
    if not _VERSION.fullmatch(arguments.transformers_version):
        parser.error("--transformers-version must be an exact semantic version")
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required")
    daemon = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if daemon.returncode != 0:
        detail = (daemon.stderr or daemon.stdout).strip()
        message = "Docker CLI is installed, but the Docker daemon is unavailable"
        if detail:
            message = f"{message}: {detail}"
        raise SystemExit(message)

    project_root = Path(__file__).resolve().parents[1]
    template = arguments.runtime_template.resolve()
    payload = json.loads(template.read_text(encoding="utf-8"))
    runtime = OpenAICompatibleAgentRuntime.model_validate(payload)
    if runtime.transport.value == "fixture":
        raise SystemExit("the Qwen image cannot use fixture transport")

    dockerfile = project_root / "docker" / "real-workload-qwen" / "Dockerfile"
    try:
        subprocess.run(
            [
                docker,
                "build",
                "--pull=false",
                "--provenance=false",
                "--build-arg",
                f"TRANSFORMERS_VERSION={arguments.transformers_version}",
                "--file",
                str(dockerfile),
                "--tag",
                arguments.tag,
                str(project_root),
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Docker build failed with exit code {exc.returncode}; no image lock was written"
        ) from exc
    image_id = subprocess.run(
        [docker, "image", "inspect", arguments.tag, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise SystemExit("Docker returned an invalid image id")

    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(runtime.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    lock = {
        "schema_version": "1.0",
        "role": "optional_qwen_coding_agent_toolchain",
        "image": image_id,
        "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
        "runtime_sha256": runtime.digest(),
        "model": runtime.model,
        "model_revision": runtime.model_revision,
        "quantization": runtime.quantization,
        "transformers_version": arguments.transformers_version,
    }
    lock_path = arguments.lock.resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(output)
    print(lock_path)


if __name__ == "__main__":
    main()
