"""权限规则与 hard deny 测试。"""

import json
from pathlib import Path

from xcode.permissions.engine import PermissionEngine


def test_hard_deny_sudo(tmp_path: Path) -> None:
    eng = PermissionEngine(auto_allow=True)
    assert eng.check("Bash", {"command": "sudo ls"}) is False
    assert eng.check("Bash", {"command": "echo ok"}) is True


def test_project_deny_rule(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = ws / ".xcode" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "rules": [
                        {
                            "tool_name": "Bash",
                            "field": "command_word",
                            "pattern": "rm",
                            "decision": "deny",
                            "reason": "no rm",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    eng = PermissionEngine.from_settings(data_home=home, workspace=ws, auto_allow=True)
    assert eng.check("Bash", {"command": "rm -rf build"}) is False
    assert eng.check("Bash", {"command": "echo hi"}) is True


def test_ask_without_callback_denies(tmp_path: Path) -> None:
    eng = PermissionEngine(auto_allow=False, ask=None)
    assert eng.check("Bash", {"command": "echo x"}) is False
    assert eng.check("Read", {"path": "a"}) is True
