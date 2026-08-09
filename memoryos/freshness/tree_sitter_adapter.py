from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from tree_sitter import Node
from tree_sitter_language_pack import get_parser


@dataclass(frozen=True)
class SymbolSlice:
    symbol_fqn: str
    symbol_kind: str
    line_start: int
    line_end: int
    excerpt: str
    backend: str


LANGUAGES = {
    "py": "python",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "rs": "rust",
}

SYMBOL_NODES = {
    "python": {"class_definition", "function_definition"},
    "typescript": {
        "class_declaration",
        "function_declaration",
        "interface_declaration",
        "method_definition",
        "type_alias_declaration",
        "variable_declarator",
    },
    "javascript": {
        "class_declaration",
        "function_declaration",
        "method_definition",
        "variable_declarator",
    },
    "rust": {
        "enum_item",
        "function_item",
        "struct_item",
        "trait_item",
        "type_item",
    },
}


def language_for_path(path: str) -> str | None:
    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return LANGUAGES.get(suffix)


def parser_backend(language: str | None) -> str:
    return "tree-sitter" if language in SYMBOL_NODES else "fallback"


def locate_symbol(source: str, language: str | None, symbol_fqn: str) -> SymbolSlice | None:
    """Locate a bounded symbol with Tree-sitter, then use conservative parser fallbacks."""

    if language in SYMBOL_NODES:
        try:
            result = _locate_tree_sitter(source, language, symbol_fqn)
        except (LookupError, RuntimeError, ValueError):
            result = None
        if result is not None:
            return result
    return _locate_fallback(source, language, symbol_fqn)


def _locate_tree_sitter(source: str, language: str, symbol_fqn: str) -> SymbolSlice | None:
    encoded = source.encode("utf-8")
    parser = get_parser(language)
    tree = parser.parse(encoded)
    requested = [part for part in symbol_fqn.split(".") if part]
    symbol_name = requested[-1]
    exact: list[tuple[Node, Node, list[str]]] = []
    loose: list[tuple[Node, Node, list[str]]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type not in SYMBOL_NODES[language]:
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        name = encoded[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")
        if name != symbol_name:
            continue
        anchor = _anchor_node(node)
        parts = _qualified_parts(node, encoded)
        candidate = (anchor, node, parts)
        if len(requested) <= len(parts) and parts[-len(requested) :] == requested:
            exact.append(candidate)
        else:
            loose.append(candidate)
    matches = exact or loose
    if not matches:
        return None
    anchor, symbol, parts = matches[0]
    excerpt = encoded[anchor.start_byte : anchor.end_byte].decode("utf-8", errors="replace")
    line_start = anchor.start_point.row + 1
    line_end = line_start + excerpt.count("\n")
    return SymbolSlice(
        symbol_fqn=".".join(parts) or symbol_fqn,
        symbol_kind=_symbol_kind(symbol.type),
        line_start=line_start,
        line_end=line_end,
        excerpt=excerpt,
        backend="tree-sitter",
    )


def _anchor_node(node: Node) -> Node:
    if node.type == "variable_declarator" and node.parent is not None:
        return node.parent
    return node


def _qualified_parts(node: Node, encoded: bytes) -> list[str]:
    parts = []
    current: Node | None = node
    while current is not None:
        is_qualifier = current.type in {
            "class_definition",
            "class_declaration",
            "function_definition",
            "function_declaration",
            "interface_declaration",
            "method_definition",
            "trait_item",
        } or (current is node and current.type in SYMBOL_NODES.get(_node_language(node), set()))
        if is_qualifier:
            name = current.child_by_field_name("name")
            if name is not None:
                parts.append(
                    encoded[name.start_byte : name.end_byte].decode("utf-8", errors="replace")
                )
        current = current.parent
    return list(reversed(parts))


def _node_language(node: Node) -> str:
    for language, node_types in SYMBOL_NODES.items():
        if node.type in node_types:
            return language
    return ""


def _symbol_kind(node_type: str) -> str:
    if "class" in node_type or node_type == "struct_item":
        return "class"
    if "function" in node_type or "method" in node_type or node_type == "function_item":
        return "function"
    if "interface" in node_type or node_type == "trait_item":
        return "interface"
    if "enum" in node_type:
        return "enum"
    if "type" in node_type:
        return "type"
    if "variable" in node_type:
        return "variable"
    return node_type


def _locate_fallback(source: str, language: str | None, symbol_fqn: str) -> SymbolSlice | None:
    symbol_name = symbol_fqn.rsplit(".", 1)[-1]
    lines = source.splitlines()
    if language == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol_name
        ]
        if not candidates:
            return None
        node = candidates[0]
        end = int(getattr(node, "end_lineno", node.lineno))
        return SymbolSlice(
            symbol_fqn=symbol_fqn,
            symbol_kind="class" if isinstance(node, ast.ClassDef) else "function",
            line_start=node.lineno,
            line_end=end,
            excerpt="\n".join(lines[node.lineno - 1 : end]),
            backend="python-ast",
        )
    patterns = {
        "typescript": re.compile(
            rf"^\s*(?:export\s+)?(?:default\s+)?(?P<kind>class|function|const|let)\s+{re.escape(symbol_name)}\b"
        ),
        "javascript": re.compile(
            rf"^\s*(?:export\s+)?(?:default\s+)?(?P<kind>class|function|const|let)\s+{re.escape(symbol_name)}\b"
        ),
        "rust": re.compile(
            rf"^\s*(?:pub\s+)?(?P<kind>fn|struct|enum|trait|type)\s+{re.escape(symbol_name)}\b"
        ),
    }
    pattern = patterns.get(language or "")
    if pattern is None:
        return None
    for index, line in enumerate(lines):
        match = pattern.search(line)
        if not match:
            continue
        end = _brace_end(lines, index)
        return SymbolSlice(
            symbol_fqn=symbol_fqn,
            symbol_kind=match.group("kind"),
            line_start=index + 1,
            line_end=end + 1,
            excerpt="\n".join(lines[index : end + 1]),
            backend="bounded-regex",
        )
    return None


def _brace_end(lines: list[str], start: int) -> int:
    balance = 0
    opened = False
    for index in range(start, min(len(lines), start + 1000)):
        balance += lines[index].count("{") - lines[index].count("}")
        opened = opened or "{" in lines[index]
        if opened and balance <= 0:
            return index
        if not opened and index > start:
            return index - 1
    return min(len(lines) - 1, start + 200)


__all__ = ["SymbolSlice", "language_for_path", "locate_symbol", "parser_backend"]
