# Claude Runner: PR Watcher & Review Workflow Design

## Overview

Three related enhancements to the claude-runner system:

1. **PR title humanization** — `create_pr` derives the PR title from the branch name instead of using the runner instance name.
2. **Repo watcher** — a new `claude-watcher@<name>` systemd service that polls a Gitea repo for PRs with "changes requested" and queues `handle-pr-comments` on the companion runner.
3. **`handle-pr-comments` skill redesign** — the watcher pre-classifies comments and writes a task list; the skill executes that list by writing response files only (no Gitea API calls); the `resolve_watch` action posts a single consolidated comment and tracks which comments have been addressed.

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

The watcher shares the companion runner's env file (`env/<name>`) for `REPO_PATH`.

### Watch State Directory

All per-PR watcher state lives under `tasks/<name>/watch/<pr_number>/`:

```
tasks/<name>/watch/<pr_number>/
  posted_comments.json   # persistent across rounds — JSON array of comment IDs already posted to Gitea
  lock                   # present while this PR is being processed by the runner
  details.json           # written each round by the watcher; removed by resolve_watch
  replied/
    <comment_id>         # response text written by the skill for each comment
```

`posted_comments.json` is the only file that survives `resolve_watch` cleanup. Everything else is per-round.

### Poll Loop

Every 60 seconds the watcher:

1. Fetches all open PRs from the repo via `tea pr list --state open`
2. For each PR, checks if its review state is "changes requested" (via `tea pr view <pr>`)
3. Checks for `tasks/<name>/watch/<pr_number>/lock` — skips if present
4. Fetches all PR comments via `tea issue comment list <pr>`
5. Reads `posted_comments.json` (defaults to `[]` if absent) and filters out any comment IDs already in that list
6. Classifies remaining comments:
   - Body starts with `question:` (case-insensitive) → **answer** list
   - No tag → **implement** list
7. If both lists are empty → nothing to do, skip
8. Creates `tasks/<name>/watch/<pr_number>/` directory (if not present)
9. Writes `details.json` (see schema below)
10. Creates `lock` file
11. Appends `handle-pr-comments pr=<pr_number>` to the companion runner's queue
12. Starts the companion runner service if not already active

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
| Watcher queues PR for processing | Created |
| `resolve_watch` runs on success | Removed |
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

1. Reads all files from `tasks/<name>/watch/<pr_number>/replied/`
2. Composes a single consolidated PR comment (see format below) from the response files
3. Posts the consolidated comment to Gitea via `tea issue comment <pr> --body <comment>`
4. Reads `posted_comments.json` (or starts with `[]`) and appends the IDs of all replied comments
5. Writes the updated array back to `posted_comments.json`
6. For each reviewer in `details.json` `reviewers_requested_changes`: re-requests their review via the Gitea API
7. Removes `lock`, `details.json`, and the `replied/` directory (leaves `posted_comments.json` intact)

### Consolidated Comment Format

```
## Review Response

> @alice: rename foo to bar

Implemented.

---

> @bob: question: why did you choose approach X?

[answer text]
```

Each section is separated by `---`. The comment ID from each `replied/<id>` filename maps to the corresponding entry in `details.json` to retrieve the author and original body for the quote.

### `skills.conf` Change

```ini
[handle-pr-comments]
on_success=resolve_watch pr=$PR
on_stuck=notify_user "could not resolve PR $PR comments: $MESSAGE"
```

---

## 3. `handle-pr-comments` Skill Redesign

### Responsibilities

The skill no longer fetches comments from Gitea or posts anything to Gitea. The watcher has already classified the work; the skill reads its task list, does the work, and writes response files. All Gitea API interaction is handled by shell scripts (`resolve_watch`).

### Updated Resolution Process

1. Locate `details.json` at `$CLAUDE_RUNNER_BASE_DIR/tasks/$NAME/watch/$PR/details.json`
2. Read `to_implement` and `to_answer` lists
3. **For each `to_implement` comment (by `id`):**
   - Implement the requested change in the codebase
   - If insufficient information: write a clarifying question as the response file (do not get stuck)
   - Write response text to `$CLAUDE_RUNNER_BASE_DIR/tasks/$NAME/watch/$PR/replied/<id>`
4. **For each `to_answer` comment (by `id`):**
   - Write the answer text to `$CLAUDE_RUNNER_BASE_DIR/tasks/$NAME/watch/$PR/replied/<id>`
5. Commit and push any code changes (skip commit if no code was changed)
6. Write `result.json` with `status: done`

### Response File Content

Each `replied/<id>` file contains only the response text (no quoting, no author — `resolve_watch` assembles the final comment). For example:

```
Implemented.
```

or:

```
I don't have enough context to implement this safely — could you clarify whether
this change should also apply to the staging config?
```

### Stuck Condition

The skill only marks itself stuck if it cannot push the branch (e.g. auth failure or merge conflict it cannot resolve). Insufficient information for a comment is handled by writing a clarifying question to the response file — never by getting stuck.

### No Gitea API calls

The skill reads exclusively from `details.json` and writes exclusively to the `replied/` directory and the codebase. Zero Gitea API calls. This keeps the skill's token usage focused entirely on implementation and response generation.
