from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from memoryos.evaluation.real_workload_models import load_real_workload_manifest
from memoryos.evaluation.real_workload_runner import load_runner_inputs

ROOT = Path(__file__).parents[1]
TASK_ROOT = ROOT / "benchmarks" / "real_workload" / "swebench_verified" / "label_seek_v2"


def test_adaptive_pack_is_frozen_and_fail_closed() -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    provenance = json.loads((TASK_ROOT / "provenance.json").read_bytes())
    partition_lock = json.loads((TASK_ROOT / "partition-lock.json").read_bytes())
    verification = json.loads((TASK_ROOT / "scorer-verification.json").read_bytes())

    assert manifest.name == "swebench-adaptive-label-seeking-v2"
    assert provenance["gold_patch_used_by_agent"] is False
    assert provenance["prior_agent_patch_used_by_agent"] is False
    assert provenance["selection_observed_prior_invalidated_outcomes"] is True
    assert partition_lock["locked_before_outcomes"] is True
    assert "not an effectiveness sample" in partition_lock["selection_policy"]
    assert len(manifest.tasks) == 2
    assert {item["partition"] for item in partition_lock["assignments"]} == {"train"}
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


def test_adaptive_scorers_and_provenance_are_bound() -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    provenance = json.loads((TASK_ROOT / "provenance.json").read_bytes())
    partition_lock = json.loads((TASK_ROOT / "partition-lock.json").read_bytes())
    verification = json.loads((TASK_ROOT / "scorer-verification.json").read_bytes())
    tasks = {task.id: task for task in manifest.tasks}
    source_by_instance = {item["instance_id"]: item for item in provenance["tasks"]}
    checked_by_task = {item["task_id"]: item for item in verification["results"]}

    assert set(tasks) == set(checked_by_task)
    for assignment in partition_lock["assignments"]:
        task = tasks[assignment["task_id"]]
        source = source_by_instance[assignment["instance_id"]]
        checked = checked_by_task[task.id]
        patch = TASK_ROOT / "hidden" / task.hidden_test.hidden_patch

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

    copied_pylint = TASK_ROOT / "hidden" / "pylint_pyreverse_annotations_hidden.patch"
    original_pylint = TASK_ROOT.parent / "cross_repo_v1" / "hidden" / copied_pylint.name
    assert copied_pylint.read_bytes() == original_pylint.read_bytes()

    pytest_patch = TASK_ROOT / "hidden" / "pytest_mark_mro_closure_hidden.patch"
    pytest_source = TASK_ROOT / "hidden_source" / "pytest_mark_mro_closure_hidden.py"
    assert _added_file_source(pytest_patch) == pytest_source.read_text(encoding="utf-8")
    compile(pytest_source.read_text(encoding="utf-8"), str(pytest_source), "exec")


def test_adaptive_repeat_schedule_is_frozen_and_balanced() -> None:
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
    assert lock["repeats_per_task"] == 2
    assert "conflicting direction invalidates" in lock["task_level_label_rule"]
    assert lock["manifest_sha256"] == manifest.digest()
    assert lock["runtime_sha256"] == hashlib.sha256(runtime_payload).hexdigest()
    assert (
        lock["runtime_file_sha256"]
        == hashlib.sha256((TASK_ROOT / "runtime-terra-medium.json").read_bytes()).hexdigest()
    )

    runs_by_task: dict[str, list[dict[str, object]]] = {}
    for item in lock["runs"]:
        runs_by_task.setdefault(item["task_id"], []).append(item)
        order = ["memoryos_full", "memoryos_minus_memory"]
        payload = (
            f"{lock['order_seed']}\x1f{item['repeat_id']}\x1f"
            f"{item['task_id']}\x1f{item['memory_id']}"
        ).encode()
        if hashlib.sha256(payload).digest()[0] & 1:
            order.reverse()
        assert item["expected_arm_order"] == order

    assert set(runs_by_task) == {task.id for task in manifest.tasks}
    assert {task_id: len(items) for task_id, items in runs_by_task.items()} == {
        task.id: 2 for task in manifest.tasks
    }
    assert all(
        {item["expected_arm_order"][0] for item in items}
        == {"memoryos_full", "memoryos_minus_memory"}
        for items in runs_by_task.values()
    )


@pytest.mark.parametrize(
    ("source", "expected_exit_code"),
    [
        ("base", 1),
        ("inline_inspect", 0),
        ("helper_closure", 0),
    ],
)
def test_pytest_scorer_accepts_equivalent_architectures(
    tmp_path: Path,
    source: str,
    expected_exit_code: int,
) -> None:
    patch = TASK_ROOT / "hidden" / "pytest_mark_mro_closure_hidden.patch"
    (tmp_path / "benchmark_hidden_test.py").write_text(
        _added_file_source(patch),
        encoding="utf-8",
        newline="\n",
    )
    structures = tmp_path / "src" / "_pytest" / "mark" / "structures.py"
    structures.parent.mkdir(parents=True)
    structures.write_text(
        _pytest_structure_source(source),
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [sys.executable, "benchmark_hidden_test.py"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )

    assert result.returncode == expected_exit_code, result.stderr


def _added_file_source(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("@@ ")) + 1
    return "\n".join(line[1:] for line in lines[start:] if line.startswith("+")) + "\n"


def _pytest_structure_source(variant: str) -> str:
    normalize = """
def normalize_mark_list(mark_list):
    for mark in mark_list:
        mark_obj = getattr(mark, "mark", mark)
        if not isinstance(mark_obj, Mark):
            raise TypeError(mark_obj)
        yield mark_obj
"""
    if variant == "base":
        behavior = """
def get_unpacked_marks(obj):
    mark_list = getattr(obj, "pytestmark", [])
    if not isinstance(mark_list, list):
        mark_list = [mark_list]
    return normalize_mark_list(mark_list)

def store_mark(obj, mark):
    obj.pytestmark = [*get_unpacked_marks(obj), mark]
"""
    elif variant == "inline_inspect":
        behavior = """
import inspect

def get_unpacked_marks(obj):
    if inspect.isclass(obj):
        marks = []
        for cls in obj.__mro__:
            direct = cls.__dict__.get("pytestmark", [])
            marks.extend(direct if isinstance(direct, list) else [direct])
    else:
        direct = getattr(obj, "pytestmark", [])
        marks = direct if isinstance(direct, list) else [direct]
    return normalize_mark_list(marks)

def store_mark(obj, mark):
    if inspect.isclass(obj):
        direct = obj.__dict__.get("pytestmark", [])
        direct = direct if isinstance(direct, list) else [direct]
        obj.pytestmark = [*normalize_mark_list(direct), mark]
    else:
        obj.pytestmark = [*get_unpacked_marks(obj), mark]
"""
    elif variant == "helper_closure":
        behavior = """
def _as_list(value):
    return value if isinstance(value, list) else [value]

def get_unpacked_marks(obj):
    if isinstance(obj, type):
        return normalize_mark_list(
            mark
            for cls in obj.__mro__
            for mark in _as_list(cls.__dict__.get("pytestmark", []))
        )
    return normalize_mark_list(_as_list(getattr(obj, "pytestmark", [])))

def store_mark(obj, mark):
    if isinstance(obj, type):
        obj.pytestmark = [
            *normalize_mark_list(_as_list(obj.__dict__.get("pytestmark", []))),
            mark,
        ]
    else:
        obj.pytestmark = [*get_unpacked_marks(obj), mark]
"""
    else:
        raise AssertionError(f"unknown variant: {variant}")
    return "from __future__ import annotations\n" + normalize + behavior
