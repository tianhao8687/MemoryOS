"""Compatibility bridge for legacy AST transformers under Python 3.12."""

from __future__ import annotations

import ast
import builtins
from typing import Any

_compile = builtins.compile


def _compile_with_locations(
    source: Any,
    filename: str,
    mode: str,
    flags: int = 0,
    dont_inherit: bool = False,
    optimize: int = -1,
    **kwargs: Any,
) -> Any:
    if isinstance(source, ast.AST):
        ast.fix_missing_locations(source)
    return _compile(
        source,
        filename,
        mode,
        flags,
        dont_inherit,
        optimize,
        **kwargs,
    )


builtins.compile = _compile_with_locations
