from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from memoryos.evaluation.real_workload_models import load_real_workload_manifest

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


def _added_file_source(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("@@ ")) + 1
    return "\n".join(line[1:] for line in lines[start:] if line.startswith("+")) + "\n"
