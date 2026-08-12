from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from memoryos.evaluation.real_workload_models import load_real_workload_manifest

ROOT = Path(__file__).parents[1]
TASK_ROOT = ROOT / "benchmarks" / "real_workload" / "swebench_verified" / "cross_repo_v1"


def test_cross_repository_public_replay_assets_are_pinned_and_disjoint() -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    provenance = json.loads((TASK_ROOT / "provenance.json").read_bytes())
    partition_lock = json.loads((TASK_ROOT / "partition-lock.json").read_bytes())
    scorer_verification = json.loads((TASK_ROOT / "scorer-verification.json").read_bytes())
    tasks = {task.id: task for task in manifest.tasks}
    provenance_tasks = {item["instance_id"]: item for item in provenance["tasks"]}

    assert provenance["gold_patch_used_by_agent"] is False
    assert partition_lock["locked_before_outcomes"] is True
    assert len(tasks) == 3
    assert scorer_verification["network"] == "none"
    assert scorer_verification["read_only_root"] is True
    assert {
        (item["task_id"], item["base_exit_code"], item["solution_exit_code"])
        for item in scorer_verification["results"]
    } == {
        ("pylint-pr-4551", 1, 0),
        ("pytest-pr-5787", 1, 0),
        ("seaborn-pr-3069", 1, 0),
    }
    train_repositories = {
        item["repository_id"]
        for item in partition_lock["assignments"]
        if item["partition"] == "train"
    }
    development_repositories = {
        item["repository_id"]
        for item in partition_lock["assignments"]
        if item["partition"] == "dev"
    }
    assert train_repositories == {"pylint-dev-pylint", "pytest-dev-pytest"}
    assert development_repositories == {"mwaskom-seaborn"}
    assert train_repositories.isdisjoint(development_repositories)

    for assignment in partition_lock["assignments"]:
        task = tasks[assignment["task_id"]]
        source = provenance_tasks[assignment["instance_id"]]
        patch = TASK_ROOT / "hidden" / task.hidden_test.hidden_patch
        assert source["base_commit"] == task.base_commit
        assert source["solution_merge_commit"] == task.solution_commit
        assert source["partition"] == assignment["partition"]
        assert datetime.fromisoformat(source["base_commit_at"].replace("Z", "+00:00")) <= (
            task.cutoff
        )
        assert datetime.fromisoformat(source["solution_merged_at"].replace("Z", "+00:00")) > (
            task.cutoff
        )
        assert hashlib.sha256(patch.read_bytes()).hexdigest() == (
            task.hidden_test.hidden_patch_sha256
        )


@pytest.mark.parametrize(
    "task_id",
    [
        "seaborn-pr-3069",
        "pylint-pr-4551",
        "pytest-pr-5787",
    ],
)
def test_cross_repository_hidden_scorers_separate_base_and_fix(
    tmp_path: Path,
    task_id: str,
) -> None:
    if task_id == "seaborn-pr-3069":
        base_files = {"seaborn/_core/plot.py": _seaborn_source(fixed=False)}
        fixed_files = {"seaborn/_core/plot.py": _seaborn_source(fixed=True)}
    elif task_id == "pylint-pr-4551":
        base_files = _pylint_sources(fixed=False)
        fixed_files = _pylint_sources(fixed=True)
    else:
        base_files = {"src/_pytest/reports.py": _pytest_source(fixed=False)}
        fixed_files = {"src/_pytest/reports.py": _pytest_source(fixed=True)}
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    task = next(task for task in manifest.tasks if task.id == task_id)
    assert task.hidden_test.hidden_patch is not None
    source = _added_file_source(TASK_ROOT / "hidden" / task.hidden_test.hidden_patch)
    (tmp_path / "benchmark_hidden_test.py").write_text(
        source,
        encoding="utf-8",
        newline="\n",
    )
    _write_files(tmp_path, base_files)
    base = _run_scorer(tmp_path)
    assert base.returncode != 0

    _write_files(tmp_path, fixed_files)
    fixed = _run_scorer(tmp_path)
    assert fixed.returncode == 0, fixed.stderr


def test_seaborn_hidden_scorer_accepts_equivalent_axes_tick_api(tmp_path: Path) -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    task = next(task for task in manifest.tasks if task.id == "seaborn-pr-3069")
    assert task.hidden_test.hidden_patch is not None
    source = _added_file_source(TASK_ROOT / "hidden" / task.hidden_test.hidden_patch)
    (tmp_path / "benchmark_hidden_test.py").write_text(
        source,
        encoding="utf-8",
        newline="\n",
    )
    _write_files(
        tmp_path,
        {"seaborn/_core/plot.py": _seaborn_source(fixed=True, use_axes_ticks=True)},
    )

    result = _run_scorer(tmp_path)

    assert result.returncode == 0, result.stderr


def _run_scorer(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "benchmark_hidden_test.py"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")


def _added_file_source(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("@@ ")) + 1
    return "\n".join(line[1:] for line in lines[start:] if line.startswith("+")) + "\n"


def _seaborn_source(*, fixed: bool, use_axes_ticks: bool = False) -> str:
    grid_call = "axis_obj.grid(False)" if use_axes_ticks else 'axis_obj.grid(False, which="both")'
    tick_count = (
        'len(getattr(ax, f"get_{axis}ticks")())'
        if use_axes_ticks
        else "len(axis_obj.get_major_ticks())"
    )
    behavior = (
        f"""
                elif isinstance(self._scales.get(axis_key), Nominal):
                    axis_obj = getattr(ax, f"{{axis}}axis")
                    {grid_call}
                    count = {tick_count}
                    low, high = -0.5, count - 0.5
                    if axis == "y":
                        low, high = high, low
                    getattr(ax, f"set_{{axis}}lim")(low, high, auto=None)
"""
        if fixed
        else ""
    )
    return f"""from __future__ import annotations

class Nominal:
    pass

class Plot:
    pass

class Plotter:
    def _finalize_figure(self, p: Plot) -> None:
        for sub in self._subplots:
            ax = sub["ax"]
            for axis in "xy":
                axis_key = sub[axis]
                if axis_key in p._limits:
                    low, high = p._limits[axis_key]
                    getattr(ax, f"set_{{axis}}lim")(low, high, auto=None)
{behavior}
        set_layout_engine(self._figure, "tight")
"""


def _pylint_sources(*, fixed: bool) -> dict[str, str]:
    inference = (
        "frame.locals_type[node.name] = [node.parent.annotation]"
        if fixed
        else "frame.locals_type[node.name] = list(set(node.infer()))"
    )
    accepted = (
        "(astroid.ClassDef, astroid.Name, astroid.Subscript)" if fixed else "astroid.ClassDef"
    )
    label = (
        'label += f"{func.name}(value: {func.args.annotations[1].name}): " '
        'f"{func.returns.name}\\\\l"'
        if fixed
        else 'label += f"{func.name}(value)\\\\l"'
    )
    return {
        "pylint/pyreverse/utils.py": """def is_exception(node):
    return node.type == "exception"
""",
        "pylint/pyreverse/inspector.py": f"""class Linker:
    def visit_assignname(self, node):
        if hasattr(node, "_handled"):
            return
        node._handled = True
        frame = node.frame() if node.name in node.frame() else node.root()
        {inference}
""",
        "pylint/pyreverse/diagrams.py": f"""class ClassDiagram:
    def class_names(self, nodes):
        names = []
        for node in nodes:
            if isinstance(node, {accepted}) and hasattr(node, "name") and not self.has_node(node):
                if node.name not in names:
                    names.append(node.name)
        return names
""",
        "pylint/pyreverse/writer.py": f"""from pylint.pyreverse.utils import is_exception

class DiagramWriter:
    pass

class DotWriter(DiagramWriter):
    def get_values(self, obj):
        label = obj.title + "|\\\\l|"
        for func in obj.methods:
            {label}
        label = "{{" + label + "}}"
        return {{"label": label}}
""",
    }


def _pytest_source(*, fixed: bool) -> str:
    if not fixed:
        methods = """    def _to_json(self):
        data = self.__dict__.copy()
        data["longrepr"] = {
            "reprtraceback": self.longrepr.reprtraceback.__dict__.copy(),
            "reprcrash": self.longrepr.reprcrash.__dict__.copy(),
            "sections": self.longrepr.sections,
        }
        return data

    @classmethod
    def _from_json(cls, data):
        return cls(**data)
"""
    else:
        methods = """    def _to_json(self):
        def entry(value):
            return {"type": type(value).__name__, "data": value.__dict__.copy()}
        def traceback(value):
            return {
                "reprentries": [entry(item) for item in value.reprentries],
                "extraline": value.extraline,
                "style": value.style,
            }
        def crash(value):
            return value.__dict__.copy()
        data = self.__dict__.copy()
        data["longrepr"] = {
            "reprtraceback": traceback(self.longrepr.reprtraceback),
            "reprcrash": crash(self.longrepr.reprcrash),
            "sections": self.longrepr.sections,
            "chain": [
                (traceback(tb), crash(location), description)
                for tb, location, description in self.longrepr.chain
            ],
        }
        return data

    @classmethod
    def _from_json(cls, data):
        def traceback(value):
            entries = []
            for item in value["reprentries"]:
                fields = item["data"]
                entries.append(ReprEntry(
                    lines=fields["lines"],
                    reprfuncargs=fields["reprfuncargs"],
                    reprlocals=fields["reprlocals"],
                    filelocrepr=fields["reprfileloc"],
                    style=fields["style"],
                ))
            return ReprTraceback(entries, extraline=value["extraline"], style=value["style"])
        def crash(value):
            return ReprFileLocation(**value)
        values = dict(data)
        encoded = values["longrepr"]
        chain = [
            (traceback(tb), crash(location), description)
            for tb, location, description in encoded["chain"]
        ]
        longrepr = ExceptionChainRepr(chain)
        longrepr.sections = list(encoded["sections"])
        values["longrepr"] = longrepr
        return cls(**values)
"""
    return f"""from _pytest._code.code import ExceptionChainRepr
from _pytest._code.code import ReprEntry
from _pytest._code.code import ReprFileLocation
from _pytest._code.code import ReprTraceback

class BaseReport:
{methods}

class TestReport(BaseReport):
    def __init__(self, nodeid, location, keywords, outcome, longrepr, when, sections=(), **extra):
        self.nodeid = nodeid
        self.location = location
        self.keywords = keywords
        self.outcome = outcome
        self.longrepr = longrepr
        self.when = when
        self.sections = list(sections)
        self.__dict__.update(extra)
"""
