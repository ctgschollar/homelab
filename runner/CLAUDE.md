# Claude Runner

## Purpose

A FastAPI service + Typer CLI for managing named, persistent Claude Code sessions on the `claude` VM. Two entry points:

- **`claude-runner-api`** — FastAPI/uvicorn HTTP service; manages session state in SQLite, spawns `claude` subprocesses
- **`claude-runner`** — CLI client that talks to the API; used interactively to seed sessions and then trigger autonomous runs

## Architecture

```
claude-runner-api (systemd, port 8080)
    ├── SQLite DB: $CLAUDE_RUNNER_BASE_DIR/runner.db
    └── Logs: $CLAUDE_RUNNER_BASE_DIR/logs/<name>.jsonl
                                        <name>.blocked  (written by Claude if stuck)

claude-runner (CLI, run by the claude user)
    └── talks to API at CLAUDE_RUNNER_API (default: http://localhost:8080)
```

Session state machine: `idle` → `running` → `done` / `error` / `waiting`

`waiting` means the session hit a rate limit and is scheduled to auto-resume at the reset time.

## Workflow

```bash
# 1. Seed a session interactively (drops into claude CLI; registers on exit)
claude-runner new <name> /path/to/repo --base-prompt "..."

# Multiple named sessions can share the same repo — separate contexts, run independently
claude-runner new feature-x /path/to/repo

# 2. Trigger an autonomous run
claude-runner run <name>
claude-runner run <name> "extra one-off instructions"

# 3. Observe
claude-runner logs <name> --follow
claude-runner list   # shows status, PID, blocked reason, and retry time if waiting

# 4. Manage
claude-runner stop <name>
claude-runner remove <name>
claude-runner set-prompt <name> "new base prompt"
```

`new` seeds `~/.claude/projects/<encoded-path>/*.jsonl` by running an interactive `claude` session; the session ID is captured from the most recently modified `.jsonl` file on exit.

## Autonomous Run Behaviour

Every run prepends an autonomous preamble to the prompt:

> You are running autonomously with no human available. Do not ask clarifying questions — make reasonable assumptions and proceed. If you are truly blocked, write a brief reason to `<base_dir>/logs/<name>.blocked` and exit.

Claude Code is invoked as:
```
claude --resume <session_id> --dangerously-skip-permissions --output-format stream-json --verbose --print <prompt>
```

`stdin` is connected to `/dev/null` so any interactive prompt (e.g. rate limit menu) causes Claude Code to exit rather than hang.

## Rate Limit Handling

When Claude Code hits a usage limit, the output contains:

```
You're out of extra usage · resets 6pm (Africa/Johannesburg)
```

The runner detects this pattern in the stream, kills the process, parses the reset time, and:

1. Marks the session `waiting` with `retry_at` stored in the DB
2. Schedules an `asyncio` task to call `start_run` again at the reset time
3. On API restart, all `waiting` sessions are rescheduled from their stored `retry_at`

`claude-runner list` shows: `waiting (retry at 18:00 SAST)`

## Auth

Claude Code credentials are stored in `~/.claude/` after the first interactive login. No `ANTHROPIC_API_KEY` env var needed — the `claude` user's `~/.claude/` is used automatically.

## Key Files

| File | Purpose |
|------|---------|
| `runner/main.py` | FastAPI app + REST endpoints; reschedules waiting sessions on startup |
| `runner/sessions.py` | Session CRUD (SQLite via aiosqlite) |
| `runner/process.py` | Subprocess management, log streaming, rate limit detection, retry scheduling |
| `runner/logs.py` | Log file read + tail |
| `runner/cli.py` | Typer CLI (`claude-runner` command) |
| `runner/db.py` | DB init and connection helper (with safe ALTER TABLE migration) |
| `runner/models.py` | Session dataclass + Status enum |

## Development

```bash
# Run tests
hatch run pytest

# Bump version (manages version in runner/__init__.py)
hatch version patch   # or minor / major

# Build package
hatch build

# Publish to Gitea PyPI (credentials from env)
hatch publish -r https://gitea.schollar.dev/api/packages/chris/pypi -u $GITEA_USER -a $GITEA_TOKEN dist/claude_runner-<version>*
```

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_RUNNER_BASE_DIR` | `/opt/claude-runner` | DB and log file root |
| `CLAUDE_RUNNER_PORT` | `8080` | API listen port |
| `CLAUDE_RUNNER_API` | `http://localhost:8080` | CLI → API base URL |
