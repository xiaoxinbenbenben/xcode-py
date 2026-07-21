"""TUI 辅助与入口烟雾。"""

from typer.testing import CliRunner

from xcode.entrypoints.cli import app
from xcode.entrypoints.tui import SLASH_COMMANDS

runner = CliRunner()


def test_doctor() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "api_key" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "xcode" in result.stdout


def test_session_new_and_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XCODE_HOME", str(tmp_path / "home"))
    result = runner.invoke(app, ["session", "new", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    sid = result.stdout.strip()
    assert sid.startswith("sess-")
    listed = runner.invoke(app, ["session", "list", "--workspace", str(tmp_path)])
    assert sid in listed.stdout


def test_slash_commands_cover_basics() -> None:
    for name in ("/help", "/exit", "/tools", "/status", "/compact"):
        assert name in SLASH_COMMANDS
