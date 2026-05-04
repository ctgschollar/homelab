"""Typer CLI entry point."""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer

app = typer.Typer(help="Claude Runner — manage named Claude Code sessions", add_completion=False)

API_URL = os.environ.get("CLAUDE_RUNNER_API", "http://localhost:8080")


def _api() -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=30)


def _encode_path(repo_path: str) -> str:
    """Encode repo path to match Claude Code's ~/.claude/projects/ dir name.

    Claude Code encodes /home/user/repo as -home-user-repo (replacing / with -).
    """
    return repo_path.replace("/", "-")


def _capture_session_id(repo_path: str, projects_root: Optional[Path] = None) -> Optional[str]:
    """Find the most recently modified .jsonl session file under ~/.claude/projects/<encoded>/."""
    encoded = _encode_path(repo_path)
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"
    projects_dir = projects_root / encoded
    if not projects_dir.exists():
        return None
    session_files = sorted(
        projects_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return session_files[0].stem if session_files else None


def _print_log_line(line: str) -> None:
    """Parse a stream-json log line and print human-readable output."""
    try:
        obj = json.loads(line)
        t = obj.get("type")
        if t == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    typer.echo(block["text"], nl=False)
                elif block.get("type") == "tool_use":
                    snippet = json.dumps(block["input"])[:200]
                    typer.echo(f"\n[tool: {block['name']}] {snippet}")
        elif t == "result":
            cost = obj.get("cost_usd", 0) or 0
            typer.echo(f"\n[session] turns={obj.get('num_turns')} cost=${cost:.4f}")
    except (json.JSONDecodeError, KeyError):
        typer.echo(line)


@app.command()
def new(
    name: str = typer.Argument(..., help="Unique name for this session"),
    repo: str = typer.Argument(..., help="Absolute path to the git repo"),
    base_prompt: Optional[str] = typer.Option(
        None, "--base-prompt", "-p",
        help="Instructions injected on every autonomous run",
    ),
):
    """Start an interactive Claude session, then register it with the API."""
    repo_path = str(Path(repo).resolve())
    if not Path(repo_path).is_dir():
        typer.echo(f"Error: repo path does not exist: {repo_path}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Starting Claude session '{name}' in {repo_path} ...")
    subprocess.run(["claude"], cwd=repo_path)

    session_id = _capture_session_id(repo_path)
    if not session_id:
        typer.echo(
            "Warning: could not capture session ID. Use 'claude-runner set-prompt' to register manually.",
            err=True,
        )

    with _api() as client:
        r = client.post("/sessions", json={
            "name": name,
            "repo_path": repo_path,
            "session_id": session_id,
            "base_prompt": base_prompt,
        })
        if r.status_code == 409:
            typer.echo(f"Error: session '{name}' already exists", err=True)
            raise typer.Exit(1)
        r.raise_for_status()

    sid_display = f" (session_id: {session_id})" if session_id else " (no session ID captured)"
    typer.echo(f"Registered session '{name}'{sid_display}")


@app.command()
def run(
    name: str = typer.Argument(..., help="Session name"),
    extra_prompt: Optional[str] = typer.Argument(None, help="Extra instructions appended to base prompt"),
):
    """Resume a session autonomously."""
    with _api() as client:
        r = client.post(f"/sessions/{name}/run", json={"extra_prompt": extra_prompt})
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        if r.status_code == 409:
            typer.echo(f"Error: session '{name}' is already running", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Started autonomous run for '{name}'. Follow with: claude-runner logs {name} --follow")


@app.command()
def stop(name: str = typer.Argument(..., help="Session name")):
    """Kill a running autonomous session."""
    with _api() as client:
        r = client.post(f"/sessions/{name}/stop")
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        if r.status_code == 409:
            typer.echo(f"Error: session '{name}' is not running", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Stopped '{name}'")


@app.command()
def logs(
    name: str = typer.Argument(..., help="Session name"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream live output"),
    n: int = typer.Option(100, "--lines", "-n", help="Number of recent lines to show"),
):
    """Show log output for a session."""
    if follow:
        with httpx.stream("GET", f"{API_URL}/sessions/{name}/logs/stream", timeout=None) as r:
            if r.status_code == 404:
                typer.echo(f"Error: session '{name}' not found", err=True)
                raise typer.Exit(1)
            r.raise_for_status()
            for line in r.iter_lines():
                if line.startswith("data: "):
                    _print_log_line(line[6:])
    else:
        with _api() as client:
            r = client.get(f"/sessions/{name}/logs", params={"n": n})
            if r.status_code == 404:
                typer.echo(f"Error: session '{name}' not found", err=True)
                raise typer.Exit(1)
            r.raise_for_status()
            for line in r.json()["lines"]:
                _print_log_line(line)


@app.command(name="list")
def list_sessions():
    """Show all sessions."""
    with _api() as client:
        r = client.get("/sessions")
        r.raise_for_status()
        sessions = r.json()
    if not sessions:
        typer.echo("No sessions. Create one with: claude-runner new <name> <repo>")
        return
    fmt = "%-20s %-10s %-6s %s"
    typer.echo(fmt % ("NAME", "STATUS", "PID", "REPO"))
    typer.echo(fmt % ("----", "------", "---", "----"))
    for s in sessions:
        typer.echo(fmt % (s["name"], s["status"], s["pid"] or "-", s["repo_path"]))
        if s.get("blocked_reason"):
            typer.echo(f"  blocked: {s['blocked_reason']}")


@app.command()
def remove(name: str = typer.Argument(..., help="Session name")):
    """Delete a session."""
    with _api() as client:
        r = client.delete(f"/sessions/{name}")
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Removed session '{name}'")


@app.command()
def set_prompt(
    name: str = typer.Argument(..., help="Session name"),
    prompt: str = typer.Argument(..., help="Base prompt injected on every autonomous run"),
):
    """Update the base prompt for a session."""
    with _api() as client:
        r = client.patch(f"/sessions/{name}", json={"base_prompt": prompt})
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Updated base prompt for '{name}'")
