from __future__ import annotations

import ast
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


source_path = Path("src/_pytest/python.py")
require(source_path.is_file(), "missing pytest Python collector")
tree = ast.parse(source_path.read_text(encoding="utf-8"))


def find_class(name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name}")


def find_method(owner: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in owner.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing {owner.name}.{name}")


package = find_class("Package")
collect = find_method(package, "collect")

eager_mount = any(
    isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and node.func.attr == "_mount_obj_if_needed"
    for node in ast.walk(collect)
)
direct_obj_access = any(
    isinstance(node, ast.Attribute)
    and isinstance(node.value, ast.Name)
    and node.value.id == "self"
    and node.attr == "obj"
    for node in ast.walk(collect)
)
getattr_obj_access = any(
    isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "getattr"
    and len(node.args) >= 2
    and isinstance(node.args[0], ast.Name)
    and node.args[0].id == "self"
    and isinstance(node.args[1], ast.Constant)
    and node.args[1].value == "obj"
    for node in ast.walk(collect)
)
require(not eager_mount, "Package.collect eagerly mounts and imports the package object")
require(
    not direct_obj_access and not getattr_obj_access,
    "Package.collect crosses the lazy object boundary during directory discovery",
)
require(
    any(
        isinstance(node, ast.Attribute) and node.attr in {"visit", "_collectfile"}
        for node in ast.walk(collect)
    ),
    "Package.collect no longer performs package traversal",
)

pyobj = find_class("PyobjMixin")
lazy_nodes = [node for node in pyobj.body if isinstance(node, ast.FunctionDef)]
require(
    any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_getobj"
        for method in lazy_nodes
        for node in ast.walk(method)
    ),
    "PyobjMixin no longer has a lazy object-loading path",
)

print("hidden scorer passed: package discovery remains lazy")
