"""用 tree-sitter 从源码抽出定义（函数/类/方法）和调用。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tree_sitter import Language, Parser, Query, QueryCursor

_QUERIES: dict[str, str] = {
    "python": """
(function_definition name: (identifier) @def.function)
(class_definition name: (identifier) @def.class)
(class_definition
  name: (identifier) @class.name
  body: (block
    [
      (function_definition name: (identifier) @def.method)
      (decorated_definition
        definition: (function_definition name: (identifier) @def.method))
    ]))
(call function: (identifier) @ref.call)
(call function: (attribute attribute: (identifier) @ref.call))
""",
    "javascript": """
(function_declaration name: (identifier) @def.function)
(class_declaration name: (identifier) @def.class)
(class_declaration
  name: (identifier) @class.name
  body: (class_body
    (method_definition name: (property_identifier) @def.method)))
(call_expression function: (identifier) @ref.call)
(call_expression function: (member_expression property: (property_identifier) @ref.call))
""",
    "typescript": """
(function_declaration name: (identifier) @def.function)
(class_declaration name: (type_identifier) @def.class)
(class_declaration
  name: (type_identifier) @class.name
  body: (class_body
    (method_definition name: (property_identifier) @def.method)))
(call_expression function: (identifier) @ref.call)
(call_expression function: (member_expression property: (property_identifier) @ref.call))
""",
    "tsx": """
(function_declaration name: (identifier) @def.function)
(class_declaration name: [(identifier) (type_identifier)] @def.class)
(class_declaration
  name: [(identifier) (type_identifier)] @class.name
  body: (class_body
    (method_definition name: (property_identifier) @def.method)))
(call_expression function: (identifier) @ref.call)
(call_expression function: (member_expression property: (property_identifier) @ref.call))
""",
    "go": """
(function_declaration name: (identifier) @def.function)
(type_spec name: (type_identifier) @def.class)
(method_declaration
  receiver: (parameter_list
    (parameter_declaration type: [
      (type_identifier) @class.name
      (pointer_type (type_identifier) @class.name)
    ]))
  name: (field_identifier) @def.method)
(call_expression function: (identifier) @ref.call)
(call_expression function: (selector_expression field: (field_identifier) @ref.call))
""",
    "rust": """
(function_item name: (identifier) @def.function)
(struct_item name: (type_identifier) @def.class)
(impl_item
  type: (type_identifier) @class.name
  body: (declaration_list
    (function_item name: (identifier) @def.method)))
(call_expression function: (identifier) @ref.call)
(call_expression function: (field_expression field: (field_identifier) @ref.call))
""",
}

_EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}


@dataclass(frozen=True, slots=True)
class Symbol:
    """一条定义或调用。"""

    path: str
    name: str
    kind: str
    line: int
    is_def: bool
    parent: str | None = None


def language_for_path(path: str | Path) -> str | None:
    """按后缀返回语言名；不在白名单则 None。"""
    return _EXT_LANG.get(Path(path).suffix.lower())


@lru_cache(maxsize=8)
def _language(lang: str) -> Language:
    """加载对应语法包。"""
    if lang == "python":
        import tree_sitter_python as mod

        return Language(mod.language())
    if lang == "javascript":
        import tree_sitter_javascript as mod

        return Language(mod.language())
    if lang in {"typescript", "tsx"}:
        import tree_sitter_typescript as mod

        ctor = mod.language_tsx if lang == "tsx" else mod.language_typescript
        return Language(ctor())
    if lang == "go":
        import tree_sitter_go as mod

        return Language(mod.language())
    if lang == "rust":
        import tree_sitter_rust as mod

        return Language(mod.language())
    raise KeyError(lang)


def parse_source(path: str, source: str) -> list[Symbol]:
    """解析一段源码；语言看后缀。失败返回空列表。"""
    lang = language_for_path(path)
    if lang is None:
        return []
    try:
        language = _language(lang)
        parser = Parser(language)
        tree = parser.parse(source.encode("utf-8", errors="replace"))
        query = Query(language, _QUERIES[lang])
        matches = QueryCursor(query).matches(tree.root_node)
    except Exception:
        return []
    return _symbols_from_matches(path, matches)


def parse_file(path: Path, *, rel: str) -> list[Symbol]:
    """读磁盘文件再解析。"""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse_source(rel, source)


def _node_text(node) -> str:
    raw = node.text or b""
    return raw.decode("utf-8", errors="replace")


def _symbols_from_matches(path: str, matches) -> list[Symbol]:
    """把 query match 收成 Symbol；方法优先于同位置的函数。"""
    methods: list[Symbol] = []
    functions: list[Symbol] = []
    classes: list[Symbol] = []
    calls: list[Symbol] = []
    for _pat, caps in matches:
        if "def.method" in caps:
            node = caps["def.method"][0]
            parent_nodes = caps.get("class.name") or []
            parent = _node_text(parent_nodes[0]) if parent_nodes else None
            methods.append(
                Symbol(
                    path=path,
                    name=_node_text(node),
                    kind="method",
                    line=node.start_point[0] + 1,
                    is_def=True,
                    parent=parent,
                )
            )
            continue
        if "def.function" in caps:
            node = caps["def.function"][0]
            functions.append(
                Symbol(
                    path=path,
                    name=_node_text(node),
                    kind="function",
                    line=node.start_point[0] + 1,
                    is_def=True,
                )
            )
            continue
        if "def.class" in caps:
            node = caps["def.class"][0]
            classes.append(
                Symbol(
                    path=path,
                    name=_node_text(node),
                    kind="class",
                    line=node.start_point[0] + 1,
                    is_def=True,
                )
            )
            continue
        if "ref.call" in caps:
            node = caps["ref.call"][0]
            calls.append(
                Symbol(
                    path=path,
                    name=_node_text(node),
                    kind="call",
                    line=node.start_point[0] + 1,
                    is_def=False,
                )
            )
    method_keys = {(s.line, s.name) for s in methods}
    functions = [s for s in functions if (s.line, s.name) not in method_keys]
    return [*classes, *methods, *functions, *calls]
