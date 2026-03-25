# Claude Runner Improvements

**Date:** 2026-03-25
**Scope:** `ansible/roles/claude-runner/`, new `ansible/install-basic-tools.yml`
**PR:** standalone

---

## Overview

Six improvements to the claude-runner ansible role:

1. Extract common tool installation into a shared playbook
2. Interactive `add-instruction` with superpower spec/plan selection
3. Multi-account Claude config support
4. Git-commit-based progress tracking for restarts
5. Stuck token to halt the runner when blocked
6. New CLI commands: `list-stuck`, `clear-stuck`, `add-account`, `remove-account`, `list-accounts`

---

## 1. `ansible/install-basic-tools.yml` — new shared playbook

A standalone playbook that installs common tools on target servers. Run this before `deploy-claude-runner.yml`.

**Hosts:** configurable (default `all`)

**Tasks:**

```yaml
- name: Install apt packages
  apt:
    name:
      - curl
      - wget
      - git
      - vim
      - python3-pip
      - pipx
    state: present
    update_cache: yes

- name: Install hatch via pipx
  become_user: "{{ target_user | default('claude') }}"
  command: pipx install hatch
  environment:
    HOME: "{{ target_user_home | default('/home/claude') }}"
  args:
    creates: "{{ target_user_home | default('/home/claude') }}/.local/bin/hatch"
```

**Effect on the runner role:** remove `pipx` from the runner's apt task and remove any hatch install step. The runner role assumes `install-basic-tools.yml` has already run.

---

## 2. `run.sh.j2` — loop script changes

### 2a. Stuck token check

At the top of the loop, before checking for a task file:

```bash
STUCK_FILE="{{ claude_runner_base_dir }}/tasks/${NAME}.stuck"

if [[ -f "$STUCK_FILE" ]]; then
    echo "$(date -Iseconds) STUCK: $(cat "$STUCK_FILE"). Clear with: claude-runner clear-stuck ${NAME}"
    sleep 60
    continue
fi
```

The runner idles and logs the stuck reason every 60 seconds. Claude is not launched. The loop only resumes after a human runs `claude-runner clear-stuck <name>`.

### 2b. Git progress resume

Before launching Claude, read the last `[PROGRESS]` commit from the repo and prepend a resume header to the task prompt:

```bash
# REPO_PATH is provided as $1 to run.sh (same as existing script)
LAST_PROGRESS=$(git -C "$REPO_PATH" log --oneline | grep '\[PROGRESS' | head -1)
if [[ -n "$LAST_PROGRESS" ]]; then
    RESUME_PREFIX="RESUME CONTEXT: Last completed progress: ${LAST_PROGRESS}\nContinue from the next incomplete step in the plan.\n\n"
else
    RESUME_PREFIX=""
fi

{ printf "%b" "$RESUME_PREFIX"; cat "$TASK_FILE"; } | claude --print --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE"
```

### 2c. Remove trailing sleep

Remove the `sleep 5` between Claude runs. Claude exits when its work is done; the systemd `Restart=always` handles crash recovery.

---

## 3. Task boilerplate — file written by `add-instruction`

The file written to `{{ claude_runner_base_dir }}/tasks/<name>.md`:

```markdown
# Task: <name>

## Rules — read these first, they apply to every step

- Work autonomously. Do not ask for permission or human approval at any point.
- Use your own discretion on all decisions.
- After completing each plan step, commit all changes with a message starting with
  `[PROGRESS N/T]` where N is the completed step number and T is the total number
  of steps, followed by a brief description of what was completed.
  Example: `[PROGRESS 2/3] Completed: concurrent shell gate serialisation`
- If you reach a point where you cannot continue for any reason, write a plain-text
  description of why to:
  {{ claude_runner_base_dir }}/tasks/<name>.stuck
  Then stop immediately — do not loop or retry.
[IF PYTHON]
- Always use `hatch run` for all Python commands:
  `hatch run pytest`, `hatch run python`, etc.
  Never use bare `python`, `python3`, or `pip` directly.
[END IF PYTHON]

## Spec

Source: <relative path to spec file>

---

<full contents of spec file embedded here>

---

## Plans — execute in this order

### Step 1 of T: <plan filename>

---

<full contents of plan 1 embedded here>

---

### Step 2 of T: <plan filename>

---

<full contents of plan 2 embedded here>

---
```

Spec and plan file contents are embedded verbatim so Claude does not need to locate them separately.

---

## 4. `claude-runner.j2` — CLI changes

### 4a. Updated `add-instruction` interactive flow

```
1. Validate <name> exists in repos.conf
2. Read REPO_PATH from env/<name>
3. Scan REPO_PATH/docs/superpowers/specs/ for *.md files → present numbered menu
   "Select spec file:"
   1) 2026-03-24-fix5-pydantic-config-schema-design.md
   2) ...
   > _
4. Scan REPO_PATH/docs/superpowers/plans/ for *.md files → present numbered menu
   "Select plans (comma-separated numbers, in execution order):"
   1) 2026-03-24-fix3-concurrent-shell-gate-design.md
   2) ...
   > 1,3,2
5. Ask: "Does this project use Python? (y/n)"
6. If accounts.conf has >1 entry, present numbered menu:
   "Select Claude account:"
   1) personal
   2) work
   > _
   (If 0 or 1 accounts registered, skip this step)
7. Write task file with boilerplate + embedded spec/plan contents
   (silently overwrites any existing task file for this name)
8. Write CLAUDE_CONFIG_DIR to env/<name> if an account was selected
9. Print: "Instruction set. Restart to apply: systemctl restart claude-runner@<name>.service"
```

If `docs/superpowers/specs/` or `docs/superpowers/plans/` does not exist in the repo, print a warning and skip that menu — the operator can still provide a plain task via stdin or `-f`.

### 4b. New commands

**`list-stuck`** — scan all `tasks/*.stuck` files and print a table:

```
NAME        REASON
----        ------
homelab     Cannot resolve SSH key — missing ansible/roles/claude-runner/files/id_rsa
```

If no stuck files, print: `No stuck runners.`

**`clear-stuck <name>`** — delete `tasks/<name>.stuck`, print confirmation. Does not restart the service; operator restarts manually.

**`add-account <name> <config-dir>`** — append `name=config-dir` to `accounts.conf`. Validates directory exists. Rejects duplicate names.

**`remove-account <name>`** — remove entry from `accounts.conf`. Also clears `CLAUDE_CONFIG_DIR` from any env files that reference it (prints a warning per affected instance).

**`list-accounts`** — print all registered accounts:

```
NAME        CONFIG DIR
----        ----------
personal    /home/claude/.claude-personal
work        /home/claude/.claude-work
```

### 4c. Updated usage block

```
Commands:
  add <name> <path>                      Register a repo and start its runner
  remove <name>                          Stop and remove a runner
  add-instruction <name>                 Interactively set the task for a runner
  list                                   Show all runners and their status
  status <name>                          Tail the journal for a runner
  list-stuck                             Show all runners with a stuck token
  clear-stuck <name>                     Remove the stuck token for a runner
  add-account <name> <config-dir>        Register a Claude account config dir
  remove-account <name>                  Deregister a Claude account
  list-accounts                          Show all registered Claude accounts
```

---

## 5. Account config — runtime mechanism

The selected account's config dir is written to `env/<name>`:

```
REPO_PATH=/path/to/repo
CLAUDE_CONFIG_DIR=/home/claude/.claude-work
```

The systemd unit already loads this file via `EnvironmentFile=`. Claude Code respects `CLAUDE_CONFIG_DIR` to locate its config instead of the default `~/.claude`. No changes needed to the service unit or `run.sh`.

---

## 6. New file: `accounts.conf`

Created at `{{ claude_runner_base_dir }}/accounts.conf` by the ansible role (empty, same pattern as `repos.conf`):

```yaml
- name: Create accounts.conf (skip if already populated)
  copy:
    content: ""
    dest: "{{ claude_runner_base_dir }}/accounts.conf"
    owner: root
    group: root
    mode: '0644'
    force: no
```

---

## Files changed

| File | Change |
|------|--------|
| `ansible/install-basic-tools.yml` | New — installs curl, wget, git, vim, python3-pip, pipx, hatch |
| `ansible/roles/claude-runner/tasks/main.yml` | Remove pipx from apt list; add accounts.conf creation task |
| `ansible/roles/claude-runner/templates/run.sh.j2` | Add stuck check; add git progress resume; remove sleep 5 |
| `ansible/roles/claude-runner/templates/claude-runner.j2` | Interactive add-instruction; new list-stuck, clear-stuck, add-account, remove-account, list-accounts commands |
| `ansible/roles/claude-runner/files/repos.conf` | No change |

---

## Out of scope

- No changes to `agent/` code (Slack removal from the agent is a separate task)
- No changes to the systemd unit template
- No changes to `defaults/main.yml`
