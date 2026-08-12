from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from memoryos.evaluation.real_workload_models import load_real_workload_manifest
from memoryos.evaluation.real_workload_runner import load_runner_inputs

ROOT = Path(__file__).parents[1]
TASK_ROOT = ROOT / "benchmarks" / "real_workload" / "swebench_verified" / "label_seek_v1"


def test_label_seeking_pack_is_frozen_before_outcomes() -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    provenance = json.loads((TASK_ROOT / "provenance.json").read_bytes())
    partition_lock = json.loads((TASK_ROOT / "partition-lock.json").read_bytes())
    verification = json.loads((TASK_ROOT / "scorer-verification.json").read_bytes())

    assert manifest.name == "swebench-label-seeking-ablation-v1"
    assert provenance["gold_patch_used_by_agent"] is False
    assert provenance["selection_observed_agent_outcomes"] is False
    assert partition_lock["locked_before_outcomes"] is True
    assert "not an effectiveness sample" in partition_lock["selection_policy"]
    assert len(manifest.tasks) == 4
    assert {item["partition"] for item in partition_lock["assignments"]} == {"train"}
    assert {item["repository_id"] for item in partition_lock["assignments"]} == {
        "pylint-dev-pylint",
        "pytest-dev-pytest",
    }
    assert verification["network"] == "none"
    assert verification["read_only_root"] is True

    _, runtime = load_runner_inputs(
        TASK_ROOT / "manifest.json",
        TASK_ROOT / "runtime-terra-medium.json",
    )
    assert runtime.model == "gpt-5.6-terra"
    assert runtime.provider == "openai-codex-chatgpt"
    assert runtime.timeout_seconds == 900
    assert runtime.evidence_type.value == "real_coding_agent"


def test_label_seeking_provenance_hashes_and_scorers_are_bound() -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    provenance = json.loads((TASK_ROOT / "provenance.json").read_bytes())
    partition_lock = json.loads((TASK_ROOT / "partition-lock.json").read_bytes())
    verification = json.loads((TASK_ROOT / "scorer-verification.json").read_bytes())
    tasks = {task.id: task for task in manifest.tasks}
    provenance_by_instance = {item["instance_id"]: item for item in provenance["tasks"]}
    verification_by_task = {item["task_id"]: item for item in verification["results"]}

    assert set(verification_by_task) == set(tasks)
    for assignment in partition_lock["assignments"]:
        task = tasks[assignment["task_id"]]
        source = provenance_by_instance[assignment["instance_id"]]
        checked = verification_by_task[task.id]
        assert task.hidden_test.hidden_patch is not None
        patch = TASK_ROOT / "hidden" / task.hidden_test.hidden_patch
        scorer_source = TASK_ROOT / "hidden_source" / f"{patch.stem}.py"

        assert source["partition"] == assignment["partition"]
        assert source["base_commit"] == task.base_commit
        assert source["source_memory_commit"] == task.base_commit
        assert source["solution_commit"] == task.solution_commit
        assert datetime.fromisoformat(source["source_memory_commit_at"].replace("Z", "+00:00")) <= (
            task.cutoff
        )
        assert datetime.fromisoformat(source["solution_committed_at"].replace("Z", "+00:00")) > (
            task.cutoff
        )
        assert hashlib.sha256(patch.read_bytes()).hexdigest() == (
            task.hidden_test.hidden_patch_sha256
        )
        assert checked == {
            "task_id": task.id,
            "hidden_patch_sha256": task.hidden_test.hidden_patch_sha256,
            "base_commit": task.base_commit,
            "base_exit_code": 1,
            "solution_commit": task.solution_commit,
            "solution_exit_code": 0,
        }
        assert _added_file_source(patch) == scorer_source.read_text(encoding="utf-8")
        compile(scorer_source.read_text(encoding="utf-8"), str(scorer_source), "exec")


def test_label_seeking_memories_are_scoped_and_temporally_valid() -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    memories = {memory.id: memory for memory in manifest.memories}

    for task in manifest.tasks:
        assert len(task.memory_seed_ids) == 1
        memory = memories[task.memory_seed_ids[0]]
        assert memory.repository_id == task.repository_id
        assert memory.source_commit == task.base_commit
        assert memory.captured_at <= task.cutoff
        assert memory.expectation == "helpful"


def test_label_seeking_run_order_and_runtime_are_locked() -> None:
    manifest, runtime = load_runner_inputs(
        TASK_ROOT / "manifest.json",
        TASK_ROOT / "runtime-terra-medium.json",
    )
    lock = json.loads((TASK_ROOT / "run-lock.json").read_bytes())
    runtime_payload = json.dumps(
        runtime.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert lock["locked_before_outcomes"] is True
    assert lock["manifest_sha256"] == manifest.digest()
    assert lock["runtime_sha256"] == hashlib.sha256(runtime_payload).hexdigest()
    assert (
        lock["runtime_file_sha256"]
        == hashlib.sha256((TASK_ROOT / "runtime-terra-medium.json").read_bytes()).hexdigest()
    )
    assert {item["task_id"] for item in lock["runs"]} == {task.id for task in manifest.tasks}
    assert sorted(item["expected_arm_order"][0] for item in lock["runs"]) == [
        "memoryos_full",
        "memoryos_full",
        "memoryos_minus_memory",
        "memoryos_minus_memory",
    ]
    for item in lock["runs"]:
        order = ["memoryos_full", "memoryos_minus_memory"]
        payload = (
            f"{lock['order_seed']}\x1f{item['repeat_id']}\x1f"
            f"{item['task_id']}\x1f{item['memory_id']}"
        ).encode()
        if hashlib.sha256(payload).digest()[0] & 1:
            order.reverse()
        assert item["expected_arm_order"] == order


def test_label_seeking_post_run_audit_fails_closed() -> None:
    audit = json.loads((TASK_ROOT / "post-run-audit.json").read_bytes())

    assert audit["status"] == "completed_with_scorer_invalidations"
    assert audit["raw_pairs"] == 4
    assert audit["protocol_valid_pairs"] == 1
    assert audit["invalidated_pairs"] == 3
    assert audit["raw_training_observations"] == 2
    assert audit["eligible_training_observations"] == 0
    assert audit["production_effect_claim"] is False
    assert len({item["run_id"] for item in audit["runs"]}) == 4

    invalid = [item for item in audit["runs"] if item["pair_status"].startswith("invalid_")]
    assert len(invalid) == 3
    assert all(item["training_observation_eligible"] is False for item in invalid)
    assert all(len(item["invalidation_reason"]) >= 80 for item in invalid)
    assert all(
        len(digest) == 64 for item in audit["runs"] for digest in item["artifact_sha256"].values()
    )


def _added_file_source(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("@@ ")) + 1
    return "\n".join(line[1:] for line in lines[start:] if line.startswith("+")) + "\n"
