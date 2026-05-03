# Claude Runner Revamp Design

**Date:** 2026-05-03
**Status:** Approved

## Problem

The existing claude runner (ansible/roles/claude-runner) treats each autonomous session as a context-free skill invocation. A spec is created in one session, then a new session runs a skill against it — losing all the conversation context that was built up during design and planning. The queue/step/skill machinery is also complex and tightly coupled.

The core insight: the right unit of work is not a skill invocation, it is a **conversation**. The user should be able to build up context interactively, then hand off that exact conversation to run autonomously.

---

## Solution Overview

A Python FastAPI service (`runner/`) that tracks named Claude Code sessions. Sessions transition from an **interactive** phase (user at terminal, full TTY) to an **autonomous** phase (service manages the subprocess, streams output). The existing ansible role is left untouched; the new role deploys this service alongside it.

---

## Architecture

### Session lifecycle

```
new <name> <repo>
  → user runs claude interactively (full TTY via subprocess.run)
  → on exit: session ID captured from ~/.claude/projects/
  → POST /sessions to register with API
  → status: idle

run <name> ["extra prompt"]
  → POST /sessions/{name}/run
  → API spawns: claude --resume <session_id> --dangerously-skip-permissions --print "<base_prompt + extra_prompt>"
  → output streamed to /opt/claude-runner/logs/<name>.jsonl
  → status: running → done | error

logs <name>
  → GET /sessions/{name}/logs/stream (SSE)
  → frontend or CLI tails the JSONL log
```

### Split responsibility

- **CLI handles the interactive phase** — `claude-runner new` launches `claude` directly in the user's terminal. No API involvement until session capture.
- **API handles the autonomous phase** — manages subprocess lifecycle, state, and log streaming.
- **Future UI compatibility** — the `POST /sessions` endpoint is already there; a websocket terminal proxy can be added later to handle `new` through the browser without changing the rest of the design.

---

## Python Project

### Location

New top-level directory: `runner/`

### Packaging

Hatch project, published to the Gitea PyPI registry at `gitea.schollar.dev`. Installed in Ansible with:

```
pip install --extra-index-url https://gitea.schollar.dev/api/packages/<owner>/pypi/simple/ claude-runner
```

Auth token for the registry comes from the host's environment (set via Ansible vars / Swarm secret).

### Directory structure

```
runner/
├── pyproject.toml
└── runner/
    ├── __init__.py
    ├── main.py          # FastAPI app, lifespan, router wiring
    ├── db.py            # SQLite setup, connection management
    ├── models.py        # Session dataclass, status enum
    ├── sessions.py      # session CRUD + state transitions
    ├── process.py       # asyncio subprocess management for autonomous runs
    ├── logs.py          # log file I/O, SSE streaming, JSONL parsing
    └── cli.py           # Typer CLI entry point
```

### Entry points

```toml
[project.scripts]
claude-runner = "runner.cli:app"        # CLI
claude-runner-api = "runner.main:main"  # uvicorn wrapper
```

---

## State — SQLite

Database at `/opt/claude-runner/runner.db`.

```sql
CREATE TABLE sessions (
    name        TEXT PRIMARY KEY,
    repo_path   TEXT NOT NULL,
    session_id  TEXT,              -- Claude Code UUID; NULL until captured
    status      TEXT NOT NULL,     -- idle | running | done | error
    base_prompt TEXT,              -- injected on every autonomous run
    pid         INTEGER,           -- subprocess PID when running, NULL otherwise
    created_at  TEXT NOT NULL,     -- ISO 8601
    updated_at  TEXT NOT NULL
);
```

Logs are stored as JSONL files on disk at `/opt/claude-runner/logs/<name>.jsonl` — no DB write pressure, easy to tail, compatible with the existing log parser.

---

## API

Base URL: `http://localhost:8080` (internal; optionally exposed via Traefik).

### Sessions

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/sessions` | Register a session. Body: `{name, repo_path, session_id, base_prompt?}` |
| `GET` | `/sessions` | List all sessions with status |
| `GET` | `/sessions/{name}` | Get session detail |
| `DELETE` | `/sessions/{name}` | Remove session and its log |
| `PATCH` | `/sessions/{name}` | Update base_prompt or session_id |

### Autonomous runs

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/sessions/{name}/run` | Start autonomous run. Body: `{extra_prompt?}` |
| `POST` | `/sessions/{name}/stop` | Kill the running subprocess |

### Logs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/sessions/{name}/logs` | Last N lines (query param `n`, default 100) |
| `GET` | `/sessions/{name}/logs/stream` | SSE stream — pushes lines as they arrive |

---

## CLI

Built with Typer. All commands except `new` are thin wrappers around API calls.

```
claude-runner new <name> <repo> [--base-prompt TEXT]
    Start an interactive Claude session. Captures session ID on exit and registers with API.

claude-runner run <name> [EXTRA_PROMPT]
    Resume the session autonomously (delegates to POST /sessions/{name}/run).

claude-runner stop <name>
    Kill the running autonomous session.

claude-runner logs <name> [--follow]
    Tail the log with human-readable output (SSE stream or last-N).

claude-runner list
    Show all sessions, status, and current activity.

claude-runner remove <name>
    Delete the session.

claude-runner set-prompt <name> PROMPT
    Update the base prompt for a session.
```

### Session ID capture (`new` command)

1. `subprocess.run(['claude'], cwd=repo_path)` — blocks until user exits, full TTY passthrough
2. Walk `~/.claude/projects/<url-encoded-repo-path>/` for the most recently modified `.jsonl` session file
3. Extract the session UUID from the filename stem (Claude Code names session files `<uuid>.jsonl`)
4. `POST /sessions` with the captured data

---

## Prompt injection

When running autonomously, the prompt sent to `claude --print` is:

```
<base_prompt>

---

<extra_prompt>
```

If only one is set, the separator is omitted. If neither is set, `claude --resume` is invoked without `--print` (continues from where the conversation left off with no new message).

---

## Ansible Deployment

New role: `ansible/roles/runner`

### Tasks

1. Install Python 3 + pip
2. Configure pip to use the Gitea PyPI index (writes `/etc/pip.conf` or `~/.config/pip/pip.conf` for the claude user)
3. `pip install claude-runner` (pulls from Gitea registry)
4. Create `/opt/claude-runner/` subdirs: `logs/`, `db/`
5. Deploy `claude-runner-api.service` systemd unit (runs `claude-runner-api`)
6. Enable and start the service

### Systemd unit

```ini
[Unit]
Description=Claude Runner API
After=network.target

[Service]
User=claude
ExecStart=/usr/local/bin/claude-runner-api
Restart=on-failure
RestartSec=5
Environment=CLAUDE_RUNNER_BASE_DIR=/opt/claude-runner
Environment=CLAUDE_RUNNER_PORT=8080

[Install]
WantedBy=multi-user.target
```

### Relationship to existing role

The new `runner` role deploys to a **new VM** (separate from the existing `claude` host at 192.168.3.79). The existing `ansible/roles/claude-runner` role and its VM are left running untouched — the old runner continues operating during the transition. Once the new approach is validated, the old VM and role will be decommissioned.

---

## What is dropped from the existing runner

- Queue files (`tasks/<name>/queue`)
- Step/total progression files
- Done/stuck state files
- Watcher service (`claude-watcher@.service`)
- Lifecycle hook system (`skills.conf`, action scripts)
- Skill command files (`implement.md`, `review-pr.md`, `handle-pr-comments.md`)

These concepts are replaced by the conversation-centric model: the user builds context interactively, so skills and multi-step orchestration are no longer needed as infrastructure.

---

## Out of scope (future)

- WebSocket terminal proxy for browser-based `new` sessions
- Frontend UI (the API is designed to support it)
- Auth on the API (internal network only for now)
- Multiple concurrent autonomous runs per session
