from __future__ import annotations

import ast
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


config_path = Path("pylint/config/__init__.py")
require(config_path.is_file(), "missing pylint/config/__init__.py")
source = config_path.read_text(encoding="utf-8")
tree = ast.parse(source)

strings = {
    node.value
    for node in ast.walk(tree)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
}
require("PYLINTHOME" in strings or "PYLINTHOME" in source, "PYLINTHOME override was removed")

cache_calls: list[ast.Call] = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    function = node.func
    name = (
        function.attr
        if isinstance(function, ast.Attribute)
        else function.id
        if isinstance(function, ast.Name)
        else ""
    )
    if name in {"user_cache_dir", "user_cache_path"}:
        cache_calls.append(node)

portable_library_cache = any(
    any(isinstance(arg, ast.Constant) and arg.value == "pylint" for arg in call.args)
    for call in cache_calls
)
direct_xdg_cache = "XDG_CACHE_HOME" in source and "pylint" in source
require(
    portable_library_cache or direct_xdg_cache,
    "default PYLINT_HOME is not derived from a platform-aware user cache",
)

if portable_library_cache:
    dependency_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (Path("setup.cfg"), Path("setup.py"), Path("pyproject.toml"))
        if path.is_file()
    ).lower()
    imported = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    cache_packages = {name for name in imported if name in {"appdirs", "platformdirs"}}
    require(cache_packages, "cache helper is not imported from appdirs or platformdirs")
    require(
        any(package in dependency_text for package in cache_packages),
        "runtime cache-directory dependency is not declared",
    )

require(".pylint.d" in source, "legacy ~/.pylint.d compatibility handling was removed")
print("hidden scorer passed: platform cache and override contract")
