"""代码索引：tree-sitter 抽符号、打分、search_code、仓库地图、跳过规则。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from xcode.code_index import (
    CodeIndexManager,
    CodeIndexStore,
    iter_index_files,
    parse_source,
    render_repo_map,
)
from xcode.context.builder import assemble_system_prompt
from xcode.tools.base import ToolContext
from xcode.tools.builtins import EditFileTool, SearchCodeTool, WriteFileTool

_APP_PY = """\
class Greeter:
    def greet(self, name):
        return hello(name)

def hello(name):
    return f"hi {name}"

def main():
    g = Greeter()
    print(g.greet("x"))
"""

_OTHER_PY = """\
from app import hello

def run():
    hello("world")
    os.path.join("a")
"""


def _by_name(symbols, name: str):
    return [s for s in symbols if s.name == name]


def test_parse_python_extracts_class_function_method_and_calls():
    symbols = parse_source("app.py", _APP_PY)
    defs = {(s.name, s.kind, s.parent) for s in symbols if s.is_def}
    assert ("Greeter", "class", None) in defs
    assert ("greet", "method", "Greeter") in defs
    assert ("hello", "function", None) in defs
    assert ("main", "function", None) in defs

    calls = {(s.name, s.line) for s in symbols if not s.is_def}
    assert ("hello", 3) in calls
    assert ("Greeter", 9) in calls
    assert ("greet", 10) in calls
    assert ("print", 10) in calls
    assert all(s.name != "name" for s in symbols)
    assert all(s.name != "os" for s in symbols)


def test_parse_skips_import_as_reference():
    symbols = parse_source("other.py", _OTHER_PY)
    assert any(s.name == "run" and s.is_def for s in symbols)
    assert any(s.name == "hello" and not s.is_def and s.line == 4 for s in symbols)
    assert not any(s.name == "hello" and not s.is_def and s.line == 1 for s in symbols)


def test_iter_index_files_skips_generated_dirs_and_huge_files(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "x.py").write_text("def nope():\n    pass\n", encoding="utf-8")
    (ws / "dist").mkdir()
    (ws / "dist" / "out.py").write_text("def built():\n    pass\n", encoding="utf-8")
    (ws / "vendor" / "lib").mkdir(parents=True)
    (ws / "vendor" / "lib" / "v.go").write_text("package v\nfunc V() {}\n", encoding="utf-8")
    (ws / "readme.md").write_text("# hi\n", encoding="utf-8")
    huge = ws / "big.py"
    huge.write_bytes(b"x" * (1 * 1024 * 1024 + 1))

    rels = {p.relative_to(ws).as_posix() for p in iter_index_files(ws)}
    assert rels == {"src/ok.py"}


def _index_example(tmp_path) -> tuple[Path, Path, CodeIndexStore]:
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(_APP_PY, encoding="utf-8")
    (ws / "other.py").write_text(_OTHER_PY, encoding="utf-8")
    store = CodeIndexStore(home, ws)
    store.replace_file("app.py", 1.0, parse_source("app.py", _APP_PY))
    store.replace_file("other.py", 1.0, parse_source("other.py", _OTHER_PY))
    store.rescore()
    store.set_status("ready")
    return home, ws, store


def test_score_counts_distinct_other_file_symbol_pairs(tmp_path):
    _, _, store = _index_example(tmp_path)
    assert store.file_score("app.py") == 1
    assert store.file_score("other.py") == 0


def test_search_exact_beats_prefix_and_caps(tmp_path):
    _, _, store = _index_example(tmp_path)
    text = store.search_code("hello")
    assert "app.py:5 function hello" in text
    assert "app.py:3 call hello" in text
    assert "other.py:4 call hello" in text
    assert "hello_world" not in text

    greet = store.search_code("gre")
    assert "Greeter.greet" in greet
    assert "function hello" not in greet

    assert "case" not in store.search_code("Hello").lower() or "no symbols" in store.search_code(
        "Hello"
    )
    assert "no symbols" in store.search_code("Hello")


def test_search_caps_defs_at_twenty(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    store = CodeIndexStore(home, ws)
    from xcode.code_index import Symbol

    symbols = [
        Symbol(path=f"f{i}.py", name="run", kind="function", line=1, is_def=True, parent=None)
        for i in range(21)
    ]
    for i, sym in enumerate(symbols):
        store.replace_file(sym.path, 1.0, [sym])
    store.set_status("ready")
    text = store.search_code("run")
    assert text.count("function run") == 20
    assert "and 1 more" in text


def test_search_not_ready_tells_model_to_grep(tmp_path):
    store = CodeIndexStore(tmp_path / "home", tmp_path / "ws")
    text = store.search_code("hello")
    assert "not ready" in text.lower()
    assert "grep" in text.lower()


def test_repo_map_nests_methods_and_omits_refs_and_lines(tmp_path):
    _, _, store = _index_example(tmp_path)
    text = render_repo_map(store)
    assert "app.py" in text
    assert "class Greeter" in text
    assert "\n    greet\n" in text or "\n    greet" in text
    assert "hello" in text
    assert "L1" not in text
    assert "call hello" not in text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines.index("app.py") < lines.index("other.py")


def test_repo_map_stops_before_splitting_a_class(tmp_path):
    from xcode.code_index import Symbol

    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    store = CodeIndexStore(home, ws)
    methods = [
        Symbol(path="big.py", name=f"m{i}", kind="method", line=i + 2, is_def=True, parent="Big")
        for i in range(8)
    ]
    store.replace_file(
        "big.py",
        1.0,
        [
            Symbol(path="big.py", name="Big", kind="class", line=1, is_def=True, parent=None),
            *methods,
        ],
    )
    store.replace_file(
        "small.py",
        1.0,
        [Symbol(path="small.py", name="tiny", kind="function", line=1, is_def=True, parent=None)],
    )
    store.set_file_score("big.py", 10)
    store.set_file_score("small.py", 1)
    text = render_repo_map(store, max_tokens=30)
    if "class Big" in text:
        for i in range(8):
            assert f"m{i}" in text
    else:
        assert "tiny" in text


def test_schema_mismatch_rebuilds(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    store = CodeIndexStore(home, ws)
    store.replace_file("app.py", 1.0, parse_source("app.py", "def hello():\n    return 1\n"))
    db = store.db_path
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    store2 = CodeIndexStore(home, ws)
    assert store2.search_code("hello").startswith("search_code")
    assert "hello" not in store2.search_code("hello") or "not ready" in store2.search_code("hello")


def test_system_prompt_injects_map_between_project_and_memory(tmp_path):
    home, ws, _store = _index_example(tmp_path)
    (ws / "XCODE.md").write_text("project rules here", encoding="utf-8")
    system = assemble_system_prompt(
        workspace=ws,
        tool_names=["search_code", "grep"],
        data_home=home,
    )
    project_at = system.index("项目说明：")
    map_at = system.index("仓库地图：")
    memory_at = system.index("## 长期记忆")
    assert project_at < map_at < memory_at
    assert "class Greeter" in system
    assert "search_code" in system
    assert "grep" in system
    assert "函数" in system or "符号" in system


def test_search_code_tool_reads_store(tmp_path):
    home, ws, _store = _index_example(tmp_path)
    ctx = ToolContext(workspace=ws, data_home=home)
    result = asyncio.run(SearchCodeTool().execute({"name": "hello"}, ctx))
    assert result.ok
    assert "function hello" in result.text


def test_search_code_tool_requires_name(tmp_path):
    ctx = ToolContext(workspace=tmp_path / "ws", data_home=tmp_path / "home")
    result = asyncio.run(SearchCodeTool().execute({}, ctx))
    assert result.is_error


def test_manager_indexes_workspace_and_write_file_refreshes(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(_APP_PY, encoding="utf-8")
    (ws / "other.py").write_text(_OTHER_PY, encoding="utf-8")
    mgr = CodeIndexManager(workspace=ws, data_home=home)

    async def _run() -> str:
        await mgr.start()
        ctx = ToolContext(workspace=ws, data_home=home, code_index=mgr)
        await WriteFileTool().execute(
            {
                "path": "extra.py",
                "content": "def hello():\n    return 2\n\ndef ping():\n    hello()\n",
            },
            ctx,
        )
        await mgr.aclose()
        return CodeIndexStore(home, ws).search_code("ping")

    text = asyncio.run(_run())
    assert "extra.py" in text
    assert "function ping" in text


def test_edit_file_refreshes_index(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    mgr = CodeIndexManager(workspace=ws, data_home=home)

    async def _run() -> str:
        await mgr.start()
        ctx = ToolContext(workspace=ws, data_home=home, code_index=mgr)
        await EditFileTool().execute(
            {
                "path": "app.py",
                "old_string": "def alpha():\n    return 1\n",
                "new_string": "def beta():\n    return 1\n",
            },
            ctx,
        )
        await mgr.aclose()
        store = CodeIndexStore(home, ws)
        return store.search_code("beta") + "\n" + store.search_code("alpha")

    text = asyncio.run(_run())
    assert "function beta" in text
    assert "no symbols" in text.split("alpha", 1)[-1] or "function alpha" not in text


def test_index_status_text_ready(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text(_APP_PY, encoding="utf-8")
    mgr = CodeIndexManager(workspace=ws, data_home=home)
    asyncio.run(mgr.start())
    text = mgr.status_text()
    asyncio.run(mgr.aclose())
    assert "ready" in text
    assert "files" in text or "文件" in text


def test_mtime_skips_unchanged_file(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    path = ws / "app.py"
    path.write_text("def one():\n    return 1\n", encoding="utf-8")
    mgr = CodeIndexManager(workspace=ws, data_home=home)
    asyncio.run(mgr.start())
    asyncio.run(mgr.aclose())
    store = CodeIndexStore(home, ws)
    first = store.file_mtime("app.py")
    mgr2 = CodeIndexManager(workspace=ws, data_home=home)
    asyncio.run(mgr2.start())
    asyncio.run(mgr2.aclose())
    assert CodeIndexStore(home, ws).file_mtime("app.py") == first


def test_parse_javascript_function_class_and_call():
    src = (
        "class Greeter {\n"
        "  greet(name) { return hello(name); }\n"
        "}\n"
        "function hello(name) { return name; }\n"
        "hello('x');\n"
    )
    symbols = parse_source("app.js", src)
    defs = {(s.name, s.kind, s.parent) for s in symbols if s.is_def}
    assert ("Greeter", "class", None) in defs
    assert ("greet", "method", "Greeter") in defs
    assert ("hello", "function", None) in defs
    assert any(s.name == "hello" and not s.is_def for s in symbols)
