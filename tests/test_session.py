"""会话存储测试。"""

from pathlib import Path

from xcode.runtime.session import SessionStore


def test_create_list_and_resolve(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    a = store.create(ws, name="alpha")
    b = store.create(ws)
    items = store.list_sessions(ws)
    assert {m.session_id for m in items} >= {a.session_id, b.session_id}

    restored = store.resolve(ws, session_id=a.session_id)
    assert restored.session_id == a.session_id
    assert restored.workspace_root == ws.resolve()

    latest = store.resolve(ws)
    assert latest.session_id in {a.session_id, b.session_id}
