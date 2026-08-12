from __future__ import annotations

import ast
import copy
import os
import re
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def function_nodes(tree: ast.Module) -> list[ast.FunctionDef]:
    return [node for node in tree.body if isinstance(node, ast.FunctionDef)]


def class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name}")


def clean_function(node: ast.FunctionDef) -> ast.FunctionDef:
    cloned = copy.deepcopy(node)
    cloned.decorator_list = []
    return cloned


expand_path = Path("pylint/lint/expand_modules.py")
pylinter_path = Path("pylint/lint/pylinter.py")
require(expand_path.is_file(), "missing expand_modules.py")
require(pylinter_path.is_file(), "missing pylinter.py")

expand_tree = ast.parse(expand_path.read_text(encoding="utf-8"))
pylinter_tree = ast.parse(pylinter_path.read_text(encoding="utf-8"))
pylinter_class = class_node(pylinter_tree, "PyLinter")
method_by_name = {
    node.name: node for node in pylinter_class.body if isinstance(node, ast.FunctionDef)
}
discover = method_by_name.get("_discover_files")
require(discover is not None, "PyLinter._discover_files is missing")

selected_methods = {"_discover_files"}
pending = [discover]
while pending:
    current = pending.pop()
    for node in ast.walk(current):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"self", "cls"}
            and node.attr in method_by_name
            and node.attr not in selected_methods
        ):
            selected_methods.add(node.attr)
            pending.append(method_by_name[node.attr])

nodes: list[ast.stmt] = [
    ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
]
nodes.extend(clean_function(node) for node in function_nodes(expand_tree))
nodes.extend(clean_function(method_by_name[name]) for name in sorted(selected_methods))
module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
namespace: dict[str, object] = {"os": os, "re": re}
exec(compile(module, "isolated_pylint_discovery.py", "exec"), namespace)  # noqa: S102

config = SimpleNamespace(
    ignore=["ignored_name"],
    ignore_patterns=[re.compile(r"^regex_")],
    ignore_paths=[re.compile(r".*path_only.*")],
)
subject = SimpleNamespace(config=config)
for name in selected_methods:
    node = method_by_name[name]
    value = namespace.get(node.name)
    if callable(value) and node.name != "_discover_files":
        setattr(subject, node.name, types.MethodType(value, subject))
for node in function_nodes(expand_tree):
    value = namespace.get(node.name)
    if callable(value) and not hasattr(subject, node.name):
        setattr(subject, node.name, value)

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    (root / "keep.py").write_text("ok = True\n", encoding="utf-8")
    for directory in ("ignored_name", "regex_hidden", "path_only"):
        target = root / directory
        target.mkdir()
        (target / "drop.py").write_text("bad = True\n", encoding="utf-8")
    package = root / "package"
    package.mkdir()
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    (package / "module.py").write_text("ok = True\n", encoding="utf-8")

    runner = namespace["_discover_files"]
    first_argument = discover.args.args[0].arg
    if first_argument in {"self", "cls"}:
        discovered = list(runner(subject, [str(root)]))
    else:
        discovered = list(runner([str(root)]))

normalized = {os.path.normcase(os.path.abspath(str(item))) for item in discovered}
require(
    os.path.normcase(os.path.abspath(str(root / "keep.py"))) in normalized,
    "recursive discovery lost a normal Python file",
)
require(
    os.path.normcase(os.path.abspath(str(package))) in normalized,
    "recursive discovery lost a normal package",
)
for directory in ("ignored_name", "regex_hidden", "path_only"):
    prefix = os.path.normcase(os.path.abspath(str(root / directory)))
    require(
        not any(item == prefix or item.startswith(prefix + os.sep) for item in normalized),
        f"recursive discovery leaked ignored directory {directory}",
    )

print("hidden scorer passed: recursive ignore semantics")
