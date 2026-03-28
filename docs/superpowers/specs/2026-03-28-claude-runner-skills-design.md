# Claude Runner Skills Architecture

**Date:** 2026-03-28
**Status:** Design

## Overview

Rework the claude-runner to replace hardcoded inline prompts in `run.sh` with composable Claude Code custom commands (skills). The runner becomes a pure orchestration loop; all workflow logic lives in skill files. Named queues allow multiple skills to be chained sequentially under a single instance name.

## Goals

- `run.sh` has no domain logic — only: invoke Claude, handle token exhaustion, track step progress, read `result.json`, dispatch lifecycle hooks
- Skills are reusable Claude custom commands installed globally for the `claude` user
- Lifecycle hooks (PR creation, notifications, chaining) are configured per skill type, not per invocation
- Multiple skills can be queued under one name and run sequentially
- New skill types can be added without modifying `run.sh`

## Architecture

Three distinct layers:

### 1. Runner (`run.sh`)

Responsibilities:
- Read `SKILL` and `SKILL_ARGS` from the instance env file
- Invoke Claude: `claude --print "/$SKILL $SKILL_ARGS step=$STEP total=$TOTAL"` (branch is auto-generated as `claude-runner/<name>` by `run.sh` and passed as `branch=<value>` appended to the invocation, not set by the user)
- Detect token exhaustion and restart
- Read `result.json` after each step; if `done` and step < total advance the step counter; if `done` and step == total dispatch `on_success` (or matching `on_result`); if `stuck` dispatch `on_stuck`
- After hook completion, pop the queue and start the next skill (or exit if queue is empty)
- On hook completion, pop the queue and start the next skill (or exit if queue is empty)

`run.sh` has no knowledge of git, PRs, Gitea, or any skill-specific logic.

### 2. Action Scripts

Composable shell scripts installed by Ansible at `/opt/claude-runner/actions/`. Each takes explicit arguments. Initial set:

| Script | Args | Purpose |
|---|---|---|
| `create_pr` | `--base <branch>` | Push branch and open PR on Gitea via `tea` |
| `notify_user` | `<message>` | Send notification (initially: write to log) |
| `post_gitea_comment` | `pr=<n> body=<text>` | Post a comment on a Gitea PR |
| `chain_skill` | `<skill> [args...]` | Prepend a skill entry to the front of the queue |

### 3. Skills (`~/.claude/commands/`)

Claude Code custom command files. Installed globally for the `claude` user by Ansible from `ansible/roles/claude-runner/files/commands/`. Each skill:

- Receives context via `$ARGUMENTS` (e.g. `spec=/path/to/spec.md step=2 total=5`)
- Writes `result.json` at the end of every step

## Skill Files

### `result.json` Format

Written by Claude to `/opt/claude-runner/tasks/<name>/result.json` at the end of each step:

```json
{
  "status": "done|stuck",
  "outcome": "<skill-specific string>",
  "message": "human readable description"
}
```

- `status`: drives the runner loop — `done` advances the step counter, `stuck` triggers `ON_STUCK`
- `outcome`: skill-specific value used for `on_result.<outcome>` hook dispatch
- `message`: available as `$MESSAGE` in hook strings

### Initial Skills

| Skill | Key args | Outcomes |
|---|---|---|
| `implement` | `spec=<path>` | `all_steps_complete` |
| `review-pr` | `pr=<number> repo=<path>` | `approved`, `changes_required` |
| `handle-pr-comments` | `pr=<number> repo=<path>` | `resolved` |

## Skill Lifecycle Configuration

`/opt/claude-runner/skills.conf` — installed by Ansible, defines default hooks per skill type:

```ini
[implement]
on_success=create_pr --base main
on_stuck=notify_user "stuck on step $STEP: $MESSAGE"

[review-pr]
on_result.changes_required=chain_skill handle-pr-comments pr=$PR repo=$REPO
on_result.approved=notify_user "PR $PR approved, no changes needed"
on_stuck=notify_user "review could not complete: $MESSAGE"

[handle-pr-comments]
on_success=notify_user "PR comments resolved"
on_stuck=notify_user "could not resolve comments: $MESSAGE"
```

Hook dispatch order:
1. `on_result.<outcome>` — if a matching entry exists, this takes priority
2. `on_success` — fallback when status is `done` and no matching `on_result`
3. `on_stuck` — when status is `stuck`

Hook values are shell strings. Variables expanded before execution:
- From `result.json`: `$STATUS`, `$OUTCOME`, `$MESSAGE`
- From runner state: `$STEP`, `$TOTAL`
- From SKILL_ARGS: each `key=value` pair is exported as `$KEY` (uppercased) — e.g. `pr=42` makes `$PR` available

Lifecycle hooks are updated by redeploying Ansible — no per-instance changes needed.

## Named Queues

`<name>` identifies a queue, not a single skill invocation. Skills added under the same name run sequentially — the next skill starts only after the current skill and all its lifecycle hooks complete.

Queue stored at `/opt/claude-runner/tasks/<name>/queue`, one entry per line:
```
implement spec=/path/to/spec.md
review-pr pr=42
```

The systemd service for `<name>` starts on the first `add skill` and runs until the queue is empty. After each skill completes and its hooks execute, the runner pops the first line and starts the next.

`chain_skill` prepends an entry at the front of the queue, allowing outcome-driven injection (e.g. injecting `handle-pr-comments` before advancing when `review-pr` returns `changes_required`).

## CLI

### Changed Commands

`add` and `add-instruction` are replaced by `add skill`:

```sh
claude-runner add skill implement <name> <repo-path> spec=<path>
claude-runner add skill review-pr <name> <repo-path> pr=<number>
claude-runner add skill handle-pr-comments <name> <repo-path> pr=<number>
```

Each invocation appends to the queue for `<name>`. The service is started automatically on the first `add skill` for a name.

### Argument Validation

`add skill` validates all arguments before writing to the queue. Failures print an error and exit non-zero without modifying state.

| Skill | Validation |
|---|---|
| `implement` | `spec=` path exists and is a file; `<repo-path>` exists and is a git repo |
| `review-pr` | `<repo-path>` exists and is a git repo; `pr=` exists on Gitea (`tea pr view <n>` succeeds) |
| `handle-pr-comments` | same as `review-pr` |

All skills: `<repo-path>` must exist and be a git repository.

### Unchanged Commands

`remove`, `list`, `status`, `logs`, `list-stuck`, `clear-stuck`, `list-done`, `clear-done`, `add-account`

`list` is updated to show queue depth and current active skill per name.

## File Layout on Target

```
/opt/claude-runner/
  actions/
    create_pr
    notify_user
    post_gitea_comment
    chain_skill
  tasks/
    <name>/
      queue          # ordered list of pending skills
      result.json    # written by Claude after each step
      step           # current step number
      total          # total steps
      branch         # git branch name
      done           # exists when queue is empty and all complete
      stuck          # exists when current skill is stuck
  env/
    <name>           # REPO_PATH, SKILL, SKILL_ARGS
  skills.conf        # lifecycle hooks per skill type
  run.sh
  logs/

~/.claude/commands/
  implement.md
  review-pr.md
  handle-pr-comments.md
```

## Ansible Changes

- Add `files/commands/` directory to `claude-runner` role with the three skill `.md` files
- Add `files/actions/` directory with the action scripts
- Add `templates/skills.conf.j2`
- Add tasks to install commands to `~/.claude/commands/` and actions to `/opt/claude-runner/actions/`
- Update `run.sh.j2` to remove inline prompts and PR creation; add skill invocation and hook dispatch
- Update `claude-runner.j2` CLI to replace `add`/`add-instruction` with `add skill`
- Remove `install-gh.yml` dependency (already done); `create_pr` action uses `tea`
