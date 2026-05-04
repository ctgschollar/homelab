# Claude Runner

## Purpose

A FastAPI service + Typer CLI for managing named, persistent Claude Code sessions on the `claude` VM. Two entry points:

- **`claude-runner-api`** — FastAPI/uvicorn HTTP service; manages session state in SQLite, spawns `claude` subprocesses
- **`claude-runner`** — CLI client that talks to the API; used interactively to seed sessions and then trigger autonomous runs

## Architecture

```
claude-runner-api (systemd, port 8080)
    ├── SQLite DB: $CLAUDE_RUNNER_BASE_DIR/sessions.db
    └── Logs: $CLAUDE_RUNNER_BASE_DIR/logs/<name>.jsonl

claude-runner (CLI, run by the claude user)
    └── talks to API at CLAUDE_RUNNER_API (default: http://localhost:8080)
```

Session state machine: `idle` → `running` → `done` / `error`

## Workflow

```bash
# 1. Seed a session interactively (drops into claude CLI; register on exit)
claude-runner new <name> /path/to/repo --base-prompt "..."

# 2. Trigger an autonomous run
claude-runner run <name> ["extra instructions"]

# 3. Observe
claude-runner logs <name> --follow

# 4. Manage
claude-runner list
claude-runner stop <name>
claude-runner remove <name>
claude-runner set-prompt <name> "new base prompt"
```

`new` seeds `~/.claude/projects/<encoded-path>/*.jsonl` by running an interactive `claude` session; the session ID is captured from the most recently modified `.jsonl` file on exit.

Autonomous runs use: `claude --resume <session_id> --dangerously-skip-permissions --output-format stream-json --print <prompt>`

## Auth

Claude Code credentials are stored in `~/.claude/` after the first interactive login. No `ANTHROPIC_API_KEY` env var needed — the `claude` user's `~/.claude/` is used automatically.

## Key Files

| File | Purpose |
|------|---------|
| `runner/main.py` | FastAPI app + REST endpoints |
| `runner/sessions.py` | Session CRUD (SQLite via aiosqlite) |
| `runner/process.py` | Subprocess management, log streaming |
| `runner/logs.py` | Log file read + tail |
| `runner/cli.py` | Typer CLI (`claude-runner` command) |
| `runner/db.py` | DB init and connection helper |
| `runner/models.py` | Pydantic models + Status enum |

## Development

```bash
# Run tests
hatch run pytest

# Build package
hatch build

# Publish to Gitea PyPI
hatch publish
```

`pyproject.toml` configures publish to `https://gitea.schollar.dev/api/packages/chris/pypi`.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_RUNNER_BASE_DIR` | `/opt/claude-runner` | SQLite DB and log file root |
| `CLAUDE_RUNNER_PORT` | `8080` | API listen port |
| `CLAUDE_RUNNER_API` | `http://localhost:8080` | CLI → API base URL |
