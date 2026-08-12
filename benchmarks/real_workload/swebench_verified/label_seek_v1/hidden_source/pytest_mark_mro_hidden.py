from __future__ import annotations

import ast
import copy
from pathlib import Path
from types import SimpleNamespace


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


source_path = Path("src/_pytest/mark/structures.py")
require(source_path.is_file(), "missing mark structures module")
tree = ast.parse(source_path.read_text(encoding="utf-8"))
wanted = {"normalize_mark_list", "get_unpacked_marks", "store_mark"}
functions = {
    node.name: node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in wanted
}
require(set(functions) == wanted, "public mark helper is missing")

body: list[ast.stmt] = [
    ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
]
for name in ("normalize_mark_list", "get_unpacked_marks", "store_mark"):
    function = copy.deepcopy(functions[name])
    function.decorator_list = []
    body.append(function)
module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))


class Mark:
    def __init__(self, name: str) -> None:
        self.name = name


class MarkDecorator:
    def __init__(self, mark: Mark) -> None:
        self.mark = mark


namespace: dict[str, object] = {"Mark": Mark, "MarkDecorator": MarkDecorator}
exec(compile(module, "isolated_mark_structures.py", "exec"), namespace)  # noqa: S102
get_unpacked_marks = namespace["get_unpacked_marks"]
store_mark = namespace["store_mark"]

a = Mark("a")
b = Mark("b")
c = Mark("c")
new = Mark("new")


class A:
    pass


class B:
    pass


class C(A, B):
    pass


A.pytestmark = [a]
B.pytestmark = MarkDecorator(b)
C.pytestmark = [c]

require(
    list(get_unpacked_marks(C)) == [c, a, b],
    "class marks are not returned in closest-first MRO order",
)
require(
    list(get_unpacked_marks(C, consider_mro=False)) == [c],
    "direct-only class mark lookup includes inherited marks",
)
store_mark(C, new)
require(C.__dict__["pytestmark"] == [c, new], "store_mark copied inherited marks onto child")
require(
    list(get_unpacked_marks(C)) == [c, new, a, b],
    "stored child mark broke subsequent MRO lookup",
)

plain = SimpleNamespace(pytestmark=MarkDecorator(a))
require(list(get_unpacked_marks(plain)) == [a], "non-class mark behavior regressed")

print("hidden scorer passed: mark MRO read/write contract")
