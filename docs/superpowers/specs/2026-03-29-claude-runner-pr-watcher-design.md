# Claude Runner: PR Watcher & Review Workflow Design

## Overview

Three related enhancements to the claude-runner system:

1. **PR title humanization** — `create_pr` derives the PR title from the branch name instead of using the runner instance name.
2. **Repo watcher** — a new `claude-watcher@<name>` systemd service that polls a Gitea repo for PRs with "changes requested" and queues `handle-pr-comments` on the companion runner.
3. **`handle-pr-comments` skill redesign** — the watcher pre-classifies comments and writes a task list; the skill executes that list without any Gitea API calls.

---

## 1. PR Title Humanization

### Change

In `ansible/roles/claude-runner/files/actions/create_pr`, the `--title` argument currently uses the runner instance name (`feat: ${NAME}`). It will instead derive a human-readable title from the branch name.

### Algorithm

Given branch name `$BRANCH`:
1. Strip a leading `<anything>/` prefix (e.g. `claude-runner/`)
2. Strip a leading date prefix matching `YYYY-MM-DD-`
3. Replace `-` and `_` with spaces
4. Capitalise the first letter

Examples:
- `claude-runner/homelab` → `feat: Homelab`
- `claude-runner/2026-03-29-gitea-runner` → `feat: Gitea runner`
- `claude-runner/add-dns-entries` → `feat: Add dns entries`

---

## 2. Repo Watcher

### Architecture

The watcher is a new long-running bash loop (`watch.sh`) managed by a new systemd service template (`claude-watcher@<name>.service`). A watcher instance is always paired 1:1 with a companion runner instance — you cannot create a watcher without an existing runner.

The watcher shares the companion runner's env file (`env/<name>`) for `REPO_PATH` and `CLAUDE_CONFIG_DIR`.

### Poll Loop

Every 60 seconds the watcher:

1. Fetches all open PRs from the repo via `tea pr list --state open`
2. For each PR, checks if its review state is "changes requested" (via `tea pr view <pr>`)
3. Checks for a lock file at `tasks/<name>/watch/<pr_number>/lock` — skips if present
4. If no lock:
   a. Fetches all PR comments via `tea issue comment list <pr>`
   b. Filters to comments that have **not** been replied to by the bot account (`GITEA_BOT_USER` from env)
   c. Classifies remaining comments:
      - Body starts with `question:` (case-insensitive) → **answer** list
      - No tag → **implement** list
   d. If both lists are empty → nothing to do, skip
   e. Creates `tasks/<name>/watch/<pr_number>/` directory
   f. Writes `details.json` (see schema below)
   g. Creates `lock` file
   h. Appends `handle-pr-comments pr=<pr_number>` to the companion runner's queue
   i. Starts the companion runner service if not already active

### `details.json` Schema

```json
{
  "pr": 42,
  "branch": "feature/something",
  "captured_at": "2026-03-29T12:00:00+00:00",
  "reviewers_requested_changes": ["alice", "bob"],
  "to_implement": [
    { "id": 1001, "author": "alice", "body": "rename foo to bar" }
  ],
  "to_answer": [
    { "id": 1002, "author": "bob", "body": "question: why did you choose approach X?" }
  ]
}
```

### Lock Lifecycle

| Event | Lock state |
|-------|-----------|
| Watcher finds unprocessed comments | Created |
| `handle-pr-comments` succeeds | Removed by `resolve_watch` |
| `handle-pr-comments` gets stuck | Lock remains; watcher skips PR; human must clear |

### New CLI Commands

Added to `claude-runner`:

```
add-watcher <name> <repo-path>    Start watching a repo (requires companion runner to exist)
remove-watcher <name>             Stop and remove the watcher
list-watchers                     Show all active watchers and their status
```

`add-watcher` validates that a runner instance named `<name>` is already registered before creating the watcher service.

### New Systemd Service

`claude-watcher@<name>.service` — mirrors the runner service structure, runs `watch.sh <repo-path> <name>`.

### New Action Script: `resolve_watch`

Called by `skills.conf` as the `on_success` hook for `handle-pr-comments`:

```
resolve_watch pr=<number>
```

1. Reads `tasks/<name>/watch/<pr_number>/details.json`
2. For each reviewer in `reviewers_requested_changes`: calls Gitea API to re-request their review
3. Removes `tasks/<name>/watch/<pr_number>/lock`

The watcher will then resume monitoring the PR on its next poll cycle.

### `skills.conf` Change

```ini
[handle-pr-comments]
on_success=resolve_watch pr=$PR
on_stuck=notify_user "could not resolve PR $PR comments: $MESSAGE"
```

### `GITEA_BOT_USER` Configuration

The watcher needs to know the bot's Gitea username to identify its own previous replies. This is set in the runner's env file (`env/<name>`) as `GITEA_BOT_USER=<username>`. The `add-watcher` command prompts for this value if not already present in the env file.

---

## 3. `handle-pr-comments` Skill Redesign

### Responsibilities

The skill no longer fetches or classifies comments. The watcher has already done that. The skill reads its pre-classified task list and executes it.

### Updated Resolution Process

1. Locate `details.json` at `$CLAUDE_RUNNER_BASE_DIR/tasks/$NAME/watch/$PR/details.json`
2. Read `to_implement` and `to_answer` lists
3. **For each `to_implement` comment:**
   - Implement the requested change in the codebase
   - If there is insufficient information to implement: post a top-level PR comment quoting the original and asking for clarification (do not get stuck)
   - If implemented successfully: post a top-level PR comment quoting the original and stating "Implemented"
4. **For each `to_answer` comment:**
   - Post a top-level PR comment quoting the original comment and answering the question
5. Commit and push any code changes (skip commit if no code was changed)
6. Write `result.json` with `status: done`

### Reply Format

Since Gitea does not support threaded comment replies, all responses are top-level PR comments in this format:

```
> @<author>: <original comment body>

<response>
```

### Stuck Condition

The skill only marks itself stuck if it cannot push the branch or cannot post comments due to an API/auth failure. Insufficient information to implement a comment is handled by posting a clarifying question — not by getting stuck.

### No Gitea API calls for reading

The skill reads exclusively from `details.json`. It does not call `tea pr view` or `tea issue comment list`. This avoids redundant API calls and keeps the skill's token usage focused on implementation.
