import json
import pytest
from pathlib import Path
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
from runner.cli import app, _encode_path, _capture_session_id

runner_cli = CliRunner()


def test_encode_path():
    assert _encode_path("/home/claude/repos/myapp") == "-home-claude-repos-myapp"


def test_capture_session_id_finds_most_recent(tmp_path):
    encoded = _encode_path(str(tmp_path / "myrepo"))
    projects_dir = Path.home() / ".claude" / "projects" / encoded
    projects_dir.mkdir(parents=True, exist_ok=True)
    older = projects_dir / "old-uuid.jsonl"
    newer = projects_dir / "new-uuid.jsonl"
    older.write_text("")
    import time; time.sleep(0.01)
    newer.write_text("")

    result = _capture_session_id(str(tmp_path / "myrepo"))
    assert result == "new-uuid"


def test_capture_session_id_missing_dir(tmp_path):
    result = _capture_session_id(str(tmp_path / "nonexistent-repo"))
    assert result is None


def test_list_command_no_sessions():
    with patch("runner.cli._api") as mock_api:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.json.return_value = []
        mock_client.get.return_value.raise_for_status = MagicMock()
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No sessions" in result.output


def test_list_command_shows_sessions():
    with patch("runner.cli._api") as mock_api:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.json.return_value = [
            {"name": "myapp", "status": "idle", "pid": None, "repo_path": "/repos/myapp"}
        ]
        mock_client.get.return_value.raise_for_status = MagicMock()
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["list"])
    assert "myapp" in result.output
    assert "idle" in result.output


def test_run_command_calls_api():
    with patch("runner.cli._api") as mock_api:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value.status_code = 202
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["run", "myapp", "do the thing"])
    assert result.exit_code == 0
    assert "myapp" in result.output


def test_new_command_registers_session(tmp_path):
    """new command: invokes subprocess.run(['claude']), captures session ID, POSTs to API."""
    repo = tmp_path / "myrepo"
    repo.mkdir()

    with patch("runner.cli.subprocess.run") as mock_run, \
         patch("runner.cli._capture_session_id", return_value="captured-uuid") as mock_capture, \
         patch("runner.cli._api") as mock_api:

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value.status_code = 201
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["new", "testapp", str(repo)])

    assert result.exit_code == 0
    mock_run.assert_called_once_with(["claude"], cwd=str(repo))
    mock_capture.assert_called_once_with(str(repo))
    posted = mock_client.post.call_args
    assert posted[0][0] == "/sessions"
    assert posted[1]["json"]["name"] == "testapp"
    assert posted[1]["json"]["session_id"] == "captured-uuid"
    assert "testapp" in result.output


def test_logs_command_non_follow(tmp_path):
    """logs command without --follow fetches last-N lines."""
    with patch("runner.cli._api") as mock_api:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.status_code = 200
        mock_client.get.return_value.raise_for_status = MagicMock()
        mock_client.get.return_value.json.return_value = {
            "lines": ['{"type": "result", "num_turns": 3, "cost_usd": 0.001}']
        }
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["logs", "myapp"])

    assert result.exit_code == 0
    assert "turns=3" in result.output


def test_print_log_line_parses_assistant():
    from runner.cli import _print_log_line
    from io import StringIO
    import sys

    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello there"}]}
    })
    # Should not raise; output contains text
    captured = []
    with patch("runner.cli.typer.echo", side_effect=lambda s, **kw: captured.append(s)):
        _print_log_line(line)
    assert any("hello there" in str(c) for c in captured)
