# Claude Runner Skills Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded inline prompts in `run.sh` with composable Claude Code custom commands (skills) and a named queue system, making the runner a pure orchestration loop.

**Architecture:** Three layers: (1) `run.sh` reads from per-instance queue files, invokes Claude via `/$SKILL $SKILL_ARGS step=$STEP total=$TOTAL branch=$BRANCH name=$NAME`, reads `result.json`, dispatches lifecycle hooks from `skills.conf`, and pops queue on completion; (2) action scripts in `/opt/claude-runner/actions/` handle side effects (PR creation, notifications, chaining); (3) skill `.md` files in `~/.claude/commands/` contain all workflow logic for Claude.

**Tech Stack:** Bash, Ansible, Claude Code custom commands (Markdown), systemd, INI config parsing with `awk`, `tea` CLI for Gitea, `jq` for JSON

---

## File Structure

Files to **create**:
- `ansible/roles/claude-runner/files/commands/implement.md` — skill: create plan then execute it
- `ansible/roles/claude-runner/files/commands/review-pr.md` — skill: review a Gitea PR
- `ansible/roles/claude-runner/files/commands/handle-pr-comments.md` — skill: resolve PR review comments
- `ansible/roles/claude-runner/files/actions/create_pr` — action: push branch + open PR
- `ansible/roles/claude-runner/files/actions/notify_user` — action: write notification to log
- `ansible/roles/claude-runner/files/actions/post_gitea_comment` — action: post comment on PR
- `ansible/roles/claude-runner/files/actions/chain_skill` — action: prepend skill to queue
- `ansible/roles/claude-runner/templates/skills.conf.j2` — lifecycle hooks per skill type

Files to **modify**:
- `ansible/roles/claude-runner/templates/run.sh.j2` — complete rewrite: queue loop + hook dispatch, no domain logic
- `ansible/roles/claude-runner/templates/claude-runner.j2` — replace `add`/`add-instruction` with `add skill` + validation; update `list`/`remove` for new dir structure
- `ansible/roles/claude-runner/tasks/main.yml` — install actions, commands, skills.conf; create actions/ dir and tasks per-name subdirectory

**Key structural change:** Task state moves from flat files (`tasks/<name>.step`, `tasks/<name>.stuck`) to per-instance subdirectories (`tasks/<name>/step`, `tasks/<name>/stuck`). Queue file at `tasks/<name>/queue` replaces `.spec-ref`, `.plan-*.md` and `.total` files.

---

### Task 1: Ansible structure — skills.conf template and updated tasks/main.yml

**Files:**
- Create: `ansible/roles/claude-runner/templates/skills.conf.j2`
- Modify: `ansible/roles/claude-runner/tasks/main.yml`

- [ ] **Step 1: Write skills.conf.j2**

```jinja2
# /opt/claude-runner/skills.conf
# Lifecycle hooks per skill type.
# Hook values are shell strings; variables are expanded before execution:
#   $STATUS, $OUTCOME, $MESSAGE — from result.json
#   $STEP, $TOTAL — from runner state
#   $NAME — instance name
#   Uppercase of any key=value in SKILL_ARGS (e.g. pr=42 → $PR)

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

Save to `ansible/roles/claude-runner/templates/skills.conf.j2`.

- [ ] **Step 2: Update tasks/main.yml**

Replace the content of `ansible/roles/claude-runner/tasks/main.yml` with the current content plus these additional tasks appended after the existing tasks (before or after the systemd task — add after the CLI install task):

```yaml
- name: Create actions directory
  file:
    path: "{{ claude_runner_base_dir }}/actions"
    state: directory
    owner: root
    group: root
    mode: '0755'

- name: Install action scripts
  copy:
    src: "actions/{{ item }}"
    dest: "{{ claude_runner_base_dir }}/actions/{{ item }}"
    owner: root
    group: root
    mode: '0755'
  loop:
    - create_pr
    - notify_user
    - post_gitea_comment
    - chain_skill

- name: Install skills.conf
  template:
    src: skills.conf.j2
    dest: "{{ claude_runner_base_dir }}/skills.conf"
    owner: root
    group: root
    mode: '0644'

- name: Create claude commands directory
  file:
    path: "{{ claude_user_home }}/.claude/commands"
    state: directory
    owner: "{{ claude_user }}"
    group: "{{ claude_user }}"
    mode: '0755'

- name: Install skill command files
  copy:
    src: "commands/{{ item }}"
    dest: "{{ claude_user_home }}/.claude/commands/{{ item }}"
    owner: "{{ claude_user }}"
    group: "{{ claude_user }}"
    mode: '0644'
  loop:
    - implement.md
    - review-pr.md
    - handle-pr-comments.md
```

- [ ] **Step 3: Verify YAML is syntactically valid**

Run: `python3 -c "import yaml; yaml.safe_load(open('ansible/roles/claude-runner/tasks/main.yml'))" && echo OK`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/templates/skills.conf.j2
git add ansible/roles/claude-runner/tasks/main.yml
git commit -m "feat(runner): add skills.conf template and ansible tasks for actions/commands"
```

---

### Task 2: Create action scripts

**Files:**
- Create: `ansible/roles/claude-runner/files/actions/create_pr`
- Create: `ansible/roles/claude-runner/files/actions/notify_user`
- Create: `ansible/roles/claude-runner/files/actions/post_gitea_comment`
- Create: `ansible/roles/claude-runner/files/actions/chain_skill`

- [ ] **Step 1: Create `files/actions/create_pr`**

```bash
#!/bin/bash
# create_pr — push current branch and open a PR on Gitea
# Usage: create_pr --base <branch>
# Env: REPO_PATH (set by runner env file), NAME (instance name)
set -euo pipefail

BASE_BRANCH="main"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE_BRANCH="$2"; shift 2 ;;
        *) echo "create_pr: unknown arg: $1" >&2; exit 1 ;;
    esac
done

BRANCH_FILE="/opt/claude-runner/tasks/${NAME}/branch"
if [[ ! -f "$BRANCH_FILE" ]]; then
    echo "create_pr: no branch file found at $BRANCH_FILE" >&2
    exit 1
fi
BRANCH=$(cat "$BRANCH_FILE")

cd "${REPO_PATH:?REPO_PATH not set}"

git push -u origin "$BRANCH"

tea pr create \
    --title "feat: ${NAME}" \
    --base "$BASE_BRANCH" \
    --head "$BRANCH" \
    --description "Automated implementation by claude-runner instance: ${NAME}"

echo "PR created: branch=${BRANCH} base=${BASE_BRANCH}"
```

- [ ] **Step 2: Create `files/actions/notify_user`**

```bash
#!/bin/bash
# notify_user — write a notification message to the instance log
# Usage: notify_user <message>
# Env: NAME (instance name)
set -euo pipefail

MESSAGE="$*"
LOG_FILE="/opt/claude-runner/logs/${NAME}.log"

echo "$(date -Iseconds) NOTIFY: ${MESSAGE}" | tee -a "$LOG_FILE"
```

- [ ] **Step 3: Create `files/actions/post_gitea_comment`**

```bash
#!/bin/bash
# post_gitea_comment — post a comment on a Gitea PR
# Usage: post_gitea_comment pr=<number> body=<text>
# Env: REPO_PATH (set by runner env file)
set -euo pipefail

PR_NUM=""
BODY=""

for arg in "$@"; do
    case "$arg" in
        pr=*)  PR_NUM="${arg#pr=}" ;;
        body=*) BODY="${arg#body=}" ;;
        *) echo "post_gitea_comment: unknown arg: $arg" >&2; exit 1 ;;
    esac
done

if [[ -z "$PR_NUM" || -z "$BODY" ]]; then
    echo "post_gitea_comment: requires pr=<number> body=<text>" >&2
    exit 1
fi

cd "${REPO_PATH:?REPO_PATH not set}"
tea issue comment "$PR_NUM" --body "$BODY"
echo "Posted comment on PR #${PR_NUM}"
```

- [ ] **Step 4: Create `files/actions/chain_skill`**

```bash
#!/bin/bash
# chain_skill — prepend a skill entry to the front of the queue
# Usage: chain_skill <skill> [args...]
# Env: NAME (instance name)
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "chain_skill: requires <skill> [args...]" >&2
    exit 1
fi

SKILL="$1"
shift
SKILL_ARGS="$*"

QUEUE_FILE="/opt/claude-runner/tasks/${NAME}/queue"

if [[ -n "$SKILL_ARGS" ]]; then
    NEW_ENTRY="${SKILL} ${SKILL_ARGS}"
else
    NEW_ENTRY="${SKILL}"
fi

# Prepend to queue (write new entry then existing content)
if [[ -f "$QUEUE_FILE" ]]; then
    TMP=$(mktemp)
    echo "$NEW_ENTRY" > "$TMP"
    cat "$QUEUE_FILE" >> "$TMP"
    mv "$TMP" "$QUEUE_FILE"
else
    echo "$NEW_ENTRY" > "$QUEUE_FILE"
fi

echo "Chained skill '${SKILL}' to front of queue for ${NAME}"
```

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/claude-runner/files/actions/
git commit -m "feat(runner): add action scripts (create_pr, notify_user, post_gitea_comment, chain_skill)"
```

---

### Task 3: implement.md skill file

**Files:**
- Create: `ansible/roles/claude-runner/files/commands/implement.md`

- [ ] **Step 1: Create `files/commands/implement.md`**

```markdown
You are implementing a software specification autonomously in two phases.

**Arguments:** $ARGUMENTS

Parse these key=value pairs from the arguments:
- `spec`: path to the spec file
- `step`: current step number (1 = planning, 2 = implementation)
- `total`: current total steps
- `branch`: git branch to work on (format: claude-runner/<name>)
- `name`: runner instance name

**Task directory:** `/opt/claude-runner/tasks/<name>/`
**Result file:** `/opt/claude-runner/tasks/<name>/result.json`

## Rules

- Work fully autonomously — no asking for approval.
- If you cannot continue for any reason, write a plain-text description to `/opt/claude-runner/tasks/<name>/stuck`, then write `result.json` with `"status": "stuck"` and stop.
- Always write `result.json` as your **final action**.
- Do not advance beyond the current step's work.

## Step 1 — Planning Phase

When `step=1`:

1. Check out the working branch: `git checkout -B <branch>`
2. Read the spec file at the path given in `spec=`
3. Use the `superpowers:writing-plans` skill to create an implementation plan
4. Commit the plan: `git add docs/superpowers/plans/ && git commit -m "plan: <name>"`
5. Write `2` to `/opt/claude-runner/tasks/<name>/total` — this tells the runner there are 2 steps total (planning + implementation)
6. Write result.json:
   ```json
   {"status": "done", "outcome": "planning_complete", "message": "Plan created and committed"}
   ```

## Step 2 — Implementation Phase

When `step=2`:

1. Find the plan file: look in `docs/superpowers/plans/` for the `.md` file matching the spec basename (e.g. spec `2026-03-28-foo-design.md` → plan `2026-03-28-foo.md`)
2. Use the `superpowers:executing-plans` skill to execute **all tasks** in the plan
3. After all tasks are complete, write result.json:
   ```json
   {"status": "done", "outcome": "all_steps_complete", "message": "All plan tasks implemented"}
   ```

If you get stuck at any point, write:
```json
{"status": "stuck", "outcome": "stuck", "message": "<reason>"}
```
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/claude-runner/files/commands/implement.md
git commit -m "feat(runner): add implement skill command"
```

---

### Task 4: review-pr.md and handle-pr-comments.md skill files

**Files:**
- Create: `ansible/roles/claude-runner/files/commands/review-pr.md`
- Create: `ansible/roles/claude-runner/files/commands/handle-pr-comments.md`

- [ ] **Step 1: Create `files/commands/review-pr.md`**

```markdown
You are reviewing a pull request on a Gitea repository.

**Arguments:** $ARGUMENTS

Parse these key=value pairs:
- `pr`: PR number to review
- `repo`: absolute path to the local repository
- `step`: current step (always 1 for this skill)
- `total`: total steps (always 1 for this skill)
- `name`: runner instance name

**Result file:** `/opt/claude-runner/tasks/<name>/result.json`

## Rules

- Work fully autonomously — no asking for approval.
- If you cannot continue, write a plain-text description to `/opt/claude-runner/tasks/<name>/stuck`, then write `result.json` with `"status": "stuck"` and stop.
- Always write `result.json` as your **final action**.

## Review Process

1. Change to the repo directory: `cd <repo>`
2. Get PR details: `tea pr view <pr>`
3. Check out the PR branch: `tea pr checkout <pr>` (this fetches and checks out the PR branch)
4. Examine the changed files: `git diff main...HEAD --stat` and review the actual diffs
5. Look for: correctness, test coverage, security issues, adherence to spec (if a spec file is linked in the PR description)
6. Make a decision:
   - **approved**: The changes look good and are ready to merge
   - **changes_required**: There are issues that must be addressed

7. Write result.json with your decision:

   If approved:
   ```json
   {"status": "done", "outcome": "approved", "message": "<brief summary of what was approved>"}
   ```

   If changes required:
   ```json
   {"status": "done", "outcome": "changes_required", "message": "<specific description of what needs to change>"}
   ```

   If you cannot complete the review:
   ```json
   {"status": "stuck", "outcome": "stuck", "message": "<reason>"}
   ```
```

- [ ] **Step 2: Create `files/commands/handle-pr-comments.md`**

```markdown
You are resolving review comments on a pull request.

**Arguments:** $ARGUMENTS

Parse these key=value pairs:
- `pr`: PR number
- `repo`: absolute path to the local repository
- `step`: current step (always 1 for this skill)
- `total`: total steps (always 1 for this skill)
- `name`: runner instance name

**Result file:** `/opt/claude-runner/tasks/<name>/result.json`

## Rules

- Work fully autonomously — no asking for approval.
- If you cannot continue, write a plain-text description to `/opt/claude-runner/tasks/<name>/stuck`, then write `result.json` with `"status": "stuck"` and stop.
- Always write `result.json` as your **final action**.

## Resolution Process

1. Change to the repo directory: `cd <repo>`
2. Get PR details and review comments: `tea pr view <pr>` and `tea issue comment list <pr>`
3. Check out the PR branch: `tea pr checkout <pr>`
4. Read through all review comments and understand what changes are requested
5. Implement all requested changes in the codebase
6. Run any applicable tests to verify the fixes
7. Commit the changes: `git add -p` and `git commit -m "fix: address PR review comments"`
8. Push the updated branch: `git push`
9. Write result.json:

   If all comments resolved:
   ```json
   {"status": "done", "outcome": "resolved", "message": "All review comments addressed and pushed"}
   ```

   If you cannot resolve all comments:
   ```json
   {"status": "stuck", "outcome": "stuck", "message": "<specific issue that blocked resolution>"}
   ```
```

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/files/commands/review-pr.md
git add ansible/roles/claude-runner/files/commands/handle-pr-comments.md
git commit -m "feat(runner): add review-pr and handle-pr-comments skill commands"
```

---

### Task 5: Rewrite run.sh.j2

**Files:**
- Modify: `ansible/roles/claude-runner/templates/run.sh.j2`

This is a complete rewrite. The new `run.sh` is a pure orchestration loop with no domain logic.

- [ ] **Step 1: Write the new run.sh.j2**

Replace the entire contents of `ansible/roles/claude-runner/templates/run.sh.j2`:

```bash
#!/bin/bash
# Managed by Ansible — do not edit directly.
# Source: ansible/roles/claude-runner/templates/run.sh.j2
set -uo pipefail

REPO_PATH="${1:?Usage: run.sh <repo-path> <name>}"
NAME="${2:?Usage: run.sh <repo-path> <name>}"

BASE_DIR="{{ claude_runner_base_dir }}"
TASK_DIR="${BASE_DIR}/tasks/${NAME}"
LOG_FILE="${BASE_DIR}/logs/${NAME}.log"
QUEUE_FILE="${TASK_DIR}/queue"
RESULT_FILE="${TASK_DIR}/result.json"
STEP_FILE="${TASK_DIR}/step"
TOTAL_FILE="${TASK_DIR}/total"
BRANCH_FILE="${TASK_DIR}/branch"
STUCK_FILE="${TASK_DIR}/stuck"
DONE_FILE="${TASK_DIR}/done"
SKILLS_CONF="${BASE_DIR}/skills.conf"

mkdir -p "$TASK_DIR"
cd "$REPO_PATH"

# Sleep until rate limit resets if the last Claude invocation was rate-limited.
check_rate_limit() {
    local resets_at
    resets_at=$(tail -n 100 "$LOG_FILE" | grep -o '{"type":"rate_limit_event"[^}]*}' | tail -1 | jq -re '.rate_limit_info.resetsAt' 2>/dev/null || true)
    if [[ -n "$resets_at" ]]; then
        local now wait
        now=$(date +%s)
        wait=$(( resets_at - now + 60 ))
        if [[ $wait -gt 0 ]]; then
            echo "$(date -Iseconds) Rate limited — sleeping ${wait}s until reset" | tee -a "$LOG_FILE"
            sleep "$wait"
        fi
        return 0
    fi
    return 1
}

# Read a lifecycle hook value from skills.conf.
# Usage: read_hook <skill> <hook_key>
# Prints the hook string (empty if not found).
read_hook() {
    local skill="$1"
    local hook_key="$2"
    [[ -f "$SKILLS_CONF" ]] || return 0
    awk -v section="[${skill}]" -v key="${hook_key}" '
        /^\[/ { in_section = ($0 == section) }
        in_section && /^[^#[:space:]]/ {
            n = index($0, "=")
            if (n > 0 && substr($0, 1, n-1) == key) {
                print substr($0, n+1)
                exit
            }
        }
    ' "$SKILLS_CONF"
}

# Dispatch a lifecycle hook string.
# Variables in the hook string are expanded from current env before execution.
# The first word is the action script name; remaining words are its arguments.
dispatch_hook() {
    local hook_str="$1"
    # Use eval to expand $VAR references within the hook string
    # then split into action + args via set --
    eval "set -- $hook_str" 2>/dev/null || {
        echo "$(date -Iseconds) Hook parse error: ${hook_str}" | tee -a "$LOG_FILE"
        return 1
    }
    local action="$1"; shift
    local action_script="${BASE_DIR}/actions/${action}"
    if [[ ! -x "$action_script" ]]; then
        echo "$(date -Iseconds) Unknown action: ${action}" | tee -a "$LOG_FILE"
        return 1
    fi
    echo "$(date -Iseconds) Dispatching: ${action} $*" | tee -a "$LOG_FILE"
    "$action_script" "$@" 2>&1 | tee -a "$LOG_FILE"
}

while true; do
    # Halt if fully done — exit 0 prevents systemd restart
    if [[ -f "$DONE_FILE" ]]; then
        echo "$(date -Iseconds) DONE: queue complete. Clear with: claude-runner clear-done ${NAME}" | tee -a "$LOG_FILE"
        exit 0
    fi

    # Halt if stuck — requires human intervention
    if [[ -f "$STUCK_FILE" ]]; then
        echo "$(date -Iseconds) STUCK: $(cat "$STUCK_FILE"). Clear with: claude-runner clear-stuck ${NAME}" | tee -a "$LOG_FILE"
        sleep 60
        continue
    fi

    # Check queue
    if [[ ! -f "$QUEUE_FILE" ]] || ! grep -q '[^[:space:]]' "$QUEUE_FILE" 2>/dev/null; then
        echo "$(date -Iseconds) Queue is empty. Marking done." | tee -a "$LOG_FILE"
        echo "All queued skills complete." > "$DONE_FILE"
        exit 0
    fi

    # Read top of queue: format is "skill [arg1 arg2 ...]"
    CURRENT_LINE=$(head -1 "$QUEUE_FILE")
    SKILL="${CURRENT_LINE%% *}"
    if [[ "$CURRENT_LINE" == *" "* ]]; then
        SKILL_ARGS="${CURRENT_LINE#* }"
    else
        SKILL_ARGS=""
    fi

    # Read step/total for this skill invocation
    STEP=$(cat "$STEP_FILE" 2>/dev/null || echo 1)
    TOTAL=$(cat "$TOTAL_FILE" 2>/dev/null || echo 1)

    # Generate branch name if not set
    if [[ ! -f "$BRANCH_FILE" ]]; then
        echo "claude-runner/${NAME}" > "$BRANCH_FILE"
    fi
    BRANCH=$(cat "$BRANCH_FILE")

    # Export SKILL_ARGS key=value pairs as uppercase env vars for hook dispatch
    while IFS='=' read -r k v; do
        [[ -z "$k" ]] && continue
        local_k=$(echo "$k" | tr '[:lower:]' '[:upper:]')
        export "${local_k}=${v}"
    done < <(echo "$SKILL_ARGS" | tr ' ' '\n' | grep '=')
    export STATUS="" OUTCOME="" MESSAGE="" STEP TOTAL NAME REPO_PATH

    # Remove stale result.json from previous invocation
    rm -f "$RESULT_FILE"

    # Invoke Claude with the skill
    echo "$(date -Iseconds) Running /${SKILL} step=${STEP}/${TOTAL} name=${NAME}" | tee -a "$LOG_FILE"
    claude --print "/${SKILL} ${SKILL_ARGS} step=${STEP} total=${TOTAL} branch=${BRANCH} name=${NAME}" \
        --verbose --output-format stream-json --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE"

    if check_rate_limit; then continue; fi

    # Read result.json
    if [[ ! -f "$RESULT_FILE" ]]; then
        echo "$(date -Iseconds) No result.json written by skill. Retrying in 30s." | tee -a "$LOG_FILE"
        sleep 30
        continue
    fi

    STATUS=$(jq -re '.status' "$RESULT_FILE" 2>/dev/null || echo "")
    OUTCOME=$(jq -re '.outcome' "$RESULT_FILE" 2>/dev/null || echo "")
    MESSAGE=$(jq -re '.message' "$RESULT_FILE" 2>/dev/null || echo "")
    export STATUS OUTCOME MESSAGE

    if [[ "$STATUS" == "stuck" ]]; then
        echo "$(date -Iseconds) STUCK at step ${STEP}/${TOTAL}: ${MESSAGE}" | tee -a "$LOG_FILE"
        echo "$MESSAGE" > "$STUCK_FILE"
        HOOK=$(read_hook "$SKILL" "on_stuck")
        [[ -n "$HOOK" ]] && dispatch_hook "$HOOK"
        continue
    fi

    if [[ "$STATUS" == "done" ]]; then
        echo "$(date -Iseconds) Step ${STEP}/${TOTAL} done: ${MESSAGE}" | tee -a "$LOG_FILE"

        # Re-read total — skill may have updated it (e.g. implement writes total=2 on step 1)
        TOTAL=$(cat "$TOTAL_FILE" 2>/dev/null || echo 1)
        export TOTAL

        if [[ "$STEP" -lt "$TOTAL" ]]; then
            echo $((STEP + 1)) > "$STEP_FILE"
            continue
        fi

        # All steps for this skill complete — dispatch lifecycle hooks
        HOOK=""
        if [[ -n "$OUTCOME" ]]; then
            HOOK=$(read_hook "$SKILL" "on_result.${OUTCOME}")
        fi
        if [[ -z "$HOOK" ]]; then
            HOOK=$(read_hook "$SKILL" "on_success")
        fi
        if [[ -n "$HOOK" ]]; then
            dispatch_hook "$HOOK"
        fi

        # Pop queue (remove first line)
        sed -i '1d' "$QUEUE_FILE"

        # Reset step/total/branch for the next skill
        rm -f "$STEP_FILE" "$TOTAL_FILE" "$BRANCH_FILE"

        # Check if queue is now empty
        if ! grep -q '[^[:space:]]' "$QUEUE_FILE" 2>/dev/null; then
            echo "$(date -Iseconds) All skills in queue complete." | tee -a "$LOG_FILE"
            echo "All queued skills complete." > "$DONE_FILE"
            exit 0
        fi

        echo "$(date -Iseconds) Advancing to next skill in queue." | tee -a "$LOG_FILE"
        continue
    fi

    # Unknown status
    echo "$(date -Iseconds) Unknown result status '${STATUS}' — retrying in 30s." | tee -a "$LOG_FILE"
    sleep 30
done
```

- [ ] **Step 2: Verify the template is syntactically valid bash (after stripping Jinja)**

Run:
```bash
sed 's/{{[^}]*}}/"placeholder"/g; s/{%[^%]*%}//g' \
    ansible/roles/claude-runner/templates/run.sh.j2 | bash -n && echo "Syntax OK"
```

Expected: `Syntax OK`

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/run.sh.j2
git commit -m "feat(runner): rewrite run.sh as pure queue orchestrator with result.json and hook dispatch"
```

---

### Task 6: Rewrite claude-runner.j2 CLI

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

Key changes:
- `add` and `add-instruction` removed; replaced by `add skill <skill> <name> <repo-path> [args...]`
- `cmd_remove` updated for new per-instance subdirectory structure
- `cmd_list` updated to show queue depth + current skill per name
- `cmd_list_stuck`, `cmd_clear_stuck`, `cmd_list_done`, `cmd_clear_done` updated for subdirectory structure
- `TASK_DIR` variable now points to the base `tasks/` dir; per-name dir is `${TASK_DIR}/${name}`

- [ ] **Step 1: Write the new claude-runner.j2**

Replace the entire contents of `ansible/roles/claude-runner/templates/claude-runner.j2`:

```jinja2
#!/bin/bash
# claude-runner — manage Claude Code loop runner instances
# Managed by Ansible — do not edit directly.
# Source: ansible/roles/claude-runner/templates/claude-runner.j2

set -euo pipefail

REPOS_CONF="{{ claude_runner_base_dir }}/repos.conf"
ENV_DIR="{{ claude_runner_base_dir }}/env"
TASK_DIR="{{ claude_runner_base_dir }}/tasks"
LOG_DIR="{{ claude_runner_base_dir }}/logs"
ACCOUNTS_CONF="{{ claude_runner_base_dir }}/accounts.conf"

{% raw %}
usage() {
    cat <<EOF
Usage: claude-runner <command> [args]

Commands:
  add skill <skill> <name> <repo-path> [key=val ...]
                                     Queue a skill for a named runner instance
  remove <name>                      Stop and remove a runner
  list                               Show all runners, queue depth, and active skill
  status <name>                      Tail the journal for a runner
  logs <name>                        Tail the log file with human-readable output
  list-stuck                         Show all runners with a stuck token
  clear-stuck <name>                 Remove the stuck token for a runner
  list-done                          Show all runners that have signalled completion
  clear-done <name>                  Remove the done file and restart the runner
  add-account <name> <config-dir>    Register a Claude account config dir
  remove-account <name>              Deregister a Claude account
  list-accounts                      Show all registered Claude accounts

Skills:
  implement  <name> <repo-path> spec=<path>
  review-pr  <name> <repo-path> pr=<number>
  handle-pr-comments <name> <repo-path> pr=<number>

Examples:
  claude-runner add skill implement homelab /home/claude/src/homelab spec=docs/superpowers/specs/2026-03-28-foo-design.md
  claude-runner add skill review-pr homelab /home/claude/src/homelab pr=42
EOF
    exit 1
}

# Register repo + create env file if not already registered.
# Sets claude_config_dir if exactly one account is registered.
_ensure_registered() {
    local name="$1"
    local repo_path="$2"

    if grep -q "^${name}=" "$REPOS_CONF" 2>/dev/null; then
        return 0  # already registered
    fi

    echo "${name}=${repo_path}" >> "$REPOS_CONF"
    printf 'REPO_PATH=%s\n' "$repo_path" > "${ENV_DIR}/${name}"
    chmod 644 "${ENV_DIR}/${name}"

    # Auto-assign account if exactly one is registered
    if [[ -f "$ACCOUNTS_CONF" ]]; then
        mapfile -t account_lines < <(grep -v '^[[:space:]]*#' "$ACCOUNTS_CONF" | grep -v '^[[:space:]]*$')
        if [[ ${#account_lines[@]} -eq 1 ]]; then
            local config_dir="${account_lines[0]#*=}"
            echo "CLAUDE_CONFIG_DIR=${config_dir}" >> "${ENV_DIR}/${name}"
        fi
    fi

    systemctl daemon-reload
    systemctl enable "claude-runner@${name}.service"
    echo "Registered claude-runner@${name} for ${repo_path}"
}

cmd_add_skill() {
    if [[ "${1:-}" != "skill" ]]; then
        echo "Error: expected 'add skill <skill> <name> <repo-path> [args...]'" >&2
        exit 1
    fi
    shift  # consume "skill"

    local skill="${1:?add skill requires a skill name (implement|review-pr|handle-pr-comments)}"
    local name="${2:?add skill requires an instance name}"
    local repo_path="${3:?add skill requires a repo-path}"
    shift 3
    local skill_args="$*"

    # Validate repo path exists and is a git repo
    if [[ ! -d "$repo_path" ]]; then
        echo "Error: repo-path does not exist: $repo_path" >&2
        exit 1
    fi
    if ! git -C "$repo_path" rev-parse --git-dir >/dev/null 2>&1; then
        echo "Error: not a git repository: $repo_path" >&2
        exit 1
    fi

    repo_path="$(realpath "$repo_path")"

    # Skill-specific validation
    case "$skill" in
        implement)
            local spec_path
            spec_path="$(printf '%s\n' $skill_args | grep -oP '^spec=\K.*' || true)"
            if [[ -z "$spec_path" ]]; then
                echo "Error: implement requires spec=<path>" >&2
                exit 1
            fi
            if [[ ! -f "$spec_path" ]]; then
                echo "Error: spec file not found: $spec_path" >&2
                exit 1
            fi
            ;;
        review-pr|handle-pr-comments)
            local pr_num
            pr_num="$(printf '%s\n' $skill_args | grep -oP '^pr=\K.*' || true)"
            if [[ -z "$pr_num" ]]; then
                echo "Error: ${skill} requires pr=<number>" >&2
                exit 1
            fi
            if ! tea pr view "$pr_num" --repo-path "$repo_path" >/dev/null 2>&1; then
                echo "Error: PR #${pr_num} not found on Gitea (tea pr view ${pr_num} failed)" >&2
                exit 1
            fi
            ;;
        *)
            echo "Error: unknown skill '${skill}'. Valid: implement, review-pr, handle-pr-comments" >&2
            exit 1
            ;;
    esac

    # Register repo and env if needed
    _ensure_registered "$name" "$repo_path"

    # Create per-instance task directory
    local task_instance_dir="${TASK_DIR}/${name}"
    mkdir -p "$task_instance_dir"
    chmod 775 "$task_instance_dir"

    # Append to queue
    local queue_file="${task_instance_dir}/queue"
    if [[ -n "$skill_args" ]]; then
        echo "${skill} ${skill_args}" >> "$queue_file"
    else
        echo "${skill}" >> "$queue_file"
    fi
    chmod 664 "$queue_file"

    # Start or restart service
    if systemctl is-active --quiet "claude-runner@${name}.service" 2>/dev/null; then
        echo "Runner claude-runner@${name} already active; skill appended to queue."
    else
        systemctl start "claude-runner@${name}.service"
        echo "Started claude-runner@${name}"
    fi
    echo "Queued: ${skill} ${skill_args:+(${skill_args})}"
}

cmd_remove() {
    local name="${1:?remove requires a name}"

    if systemctl is-active --quiet "claude-runner@${name}.service" 2>/dev/null; then
        systemctl stop "claude-runner@${name}.service"
    fi
    systemctl disable "claude-runner@${name}.service" 2>/dev/null || true

    sed -i "/^${name}=/d" "$REPOS_CONF"
    rm -f "${ENV_DIR}/${name}"
    rm -rf "${TASK_DIR}/${name}"

    echo "Removed claude-runner@${name}"
}

cmd_add_account() {
    local name="${1:?add-account requires a name}"
    local config_dir="${2:?add-account requires a config-dir}"

    if [[ ! -d "$config_dir" ]]; then
        echo "Error: directory does not exist: $config_dir" >&2
        exit 1
    fi

    config_dir="$(realpath "$config_dir")"

    if grep -q "^${name}=" "$ACCOUNTS_CONF" 2>/dev/null; then
        echo "Error: account '${name}' already exists" >&2
        exit 1
    fi

    echo "${name}=${config_dir}" >> "$ACCOUNTS_CONF"
    echo "Registered account '${name}' -> ${config_dir}"
}

cmd_remove_account() {
    local name="${1:?remove-account requires a name}"

    if ! grep -q "^${name}=" "$ACCOUNTS_CONF" 2>/dev/null; then
        echo "Error: account '${name}' not found" >&2
        exit 1
    fi

    local config_dir
    config_dir=$(grep "^${name}=" "$ACCOUNTS_CONF" | cut -d= -f2-)

    while IFS='=' read -r inst_name _inst_path; do
        [[ -z "$inst_name" || "$inst_name" == \#* ]] && continue
        local env_file="${ENV_DIR}/${inst_name}"
        if grep -q "^CLAUDE_CONFIG_DIR=${config_dir}$" "$env_file" 2>/dev/null; then
            echo "Warning: claude-runner@${inst_name} was using this account — CLAUDE_CONFIG_DIR cleared"
            sed -i '/^CLAUDE_CONFIG_DIR=/d' "$env_file"
        fi
    done < "$REPOS_CONF"

    sed -i "/^${name}=/d" "$ACCOUNTS_CONF"
    echo "Removed account '${name}'"
}

cmd_list_accounts() {
    if [[ ! -s "$ACCOUNTS_CONF" ]]; then
        echo "No accounts registered. Use: claude-runner add-account <name> <config-dir>"
        return
    fi

    printf "%-20s %s\n" "NAME" "CONFIG DIR"
    printf "%-20s %s\n" "----" "----------"

    while IFS='=' read -r name config_dir; do
        [[ -z "$name" || "$name" == \#* ]] && continue
        printf "%-20s %s\n" "$name" "$config_dir"
    done < "$ACCOUNTS_CONF"
}

cmd_list_done() {
    local found=0
    printf "%-20s %s\n" "NAME" "SUMMARY"
    printf "%-20s %s\n" "----" "-------"
    for done_dir in "${TASK_DIR}"/*/; do
        [[ -d "$done_dir" ]] || continue
        local done_file="${done_dir}done"
        [[ -f "$done_file" ]] || continue
        local name
        name=$(basename "$done_dir")
        local summary
        summary=$(head -1 "$done_file")
        printf "%-20s %s\n" "$name" "$summary"
        found=1
    done
    [[ $found -eq 0 ]] && echo "No completed runners."
}

cmd_clear_done() {
    local name="${1:?clear-done requires a name}"
    local done_file="${TASK_DIR}/${name}/done"

    if [[ ! -f "$done_file" ]]; then
        echo "Error: no done file found for '${name}'" >&2
        exit 1
    fi

    rm "$done_file"
    echo "Cleared done file for '${name}'"
    systemctl restart "claude-runner@${name}.service"
    echo "Restarted claude-runner@${name}"
}

cmd_list_stuck() {
    local found=0
    printf "%-20s %s\n" "NAME" "REASON"
    printf "%-20s %s\n" "----" "------"
    for stuck_dir in "${TASK_DIR}"/*/; do
        [[ -d "$stuck_dir" ]] || continue
        local stuck_file="${stuck_dir}stuck"
        [[ -f "$stuck_file" ]] || continue
        local name
        name=$(basename "$stuck_dir")
        local reason
        reason=$(head -1 "$stuck_file")
        printf "%-20s %s\n" "$name" "$reason"
        found=1
    done
    [[ $found -eq 0 ]] && echo "No stuck runners."
}

cmd_clear_stuck() {
    local name="${1:?clear-stuck requires a name}"
    local stuck_file="${TASK_DIR}/${name}/stuck"

    if [[ ! -f "$stuck_file" ]]; then
        echo "Error: no stuck token found for '${name}'" >&2
        exit 1
    fi

    rm "$stuck_file"
    echo "Cleared stuck token for '${name}'"
    echo "Restart the runner: systemctl restart claude-runner@${name}.service"
}

cmd_list() {
    if [[ ! -s "$REPOS_CONF" ]]; then
        echo "No repos configured. Use: claude-runner add skill <skill> <name> <repo-path> [args...]"
        return
    fi

    printf "%-20s %-35s %-10s %-8s %-6s %s\n" "NAME" "PATH" "STATUS" "STEP" "QUEUE" "CURRENT SKILL"
    printf "%-20s %-35s %-10s %-8s %-6s %s\n" "----" "----" "------" "----" "-----" "-------------"

    while IFS='=' read -r name path; do
        [[ -z "$name" || "$name" == \#* ]] && continue

        local task_instance_dir="${TASK_DIR}/${name}"
        local status step total step_col queue_depth current_skill

        if [[ -f "${task_instance_dir}/done" ]]; then
            status="done"
        elif [[ -f "${task_instance_dir}/stuck" ]]; then
            status="stuck"
        else
            status="$(systemctl is-active "claude-runner@${name}.service" 2>/dev/null || echo inactive)"
        fi

        step=$(cat "${task_instance_dir}/step" 2>/dev/null || echo "-")
        total=$(cat "${task_instance_dir}/total" 2>/dev/null || echo "-")
        if [[ "$step" == "-" ]]; then
            step_col="-"
        else
            step_col="${step}/${total}"
        fi

        queue_depth=0
        current_skill="-"
        if [[ -f "${task_instance_dir}/queue" ]]; then
            queue_depth=$(grep -c '[^[:space:]]' "${task_instance_dir}/queue" 2>/dev/null || echo 0)
            if [[ $queue_depth -gt 0 ]]; then
                local first_line
                first_line=$(head -1 "${task_instance_dir}/queue")
                current_skill="${first_line%% *}"
            fi
        fi

        printf "%-20s %-35s %-10s %-8s %-6s %s\n" \
            "$name" "$path" "$status" "$step_col" "$queue_depth" "$current_skill"
    done < "$REPOS_CONF"
}

cmd_status() {
    local name="${1:?status requires a name}"
    exec journalctl -u "claude-runner@${name}.service" -f --output short-iso
}

cmd_logs() {
    local name="${1:?logs requires a name}"
    local log_file="${LOG_DIR}/${name}.log"

    if [[ ! -f "$log_file" ]]; then
        echo "No log file found for '${name}': ${log_file}" >&2
        exit 1
    fi

    tail -f "$log_file" | while IFS= read -r line; do
        local type
        if type=$(printf '%s' "$line" | jq -re '.type' 2>/dev/null); then
            case "$type" in
                assistant)
                    printf '%s' "$line" | jq -r '
                        .message.content[]? |
                        if .type == "text" then .text
                        elif .type == "tool_use" then "\n[tool: \(.name)] \(.input | tostring | .[0:200])"
                        else empty
                        end
                    ' 2>/dev/null || true
                    ;;
                tool_result)
                    printf '%s' "$line" | jq -r '
                        "[result] \(if .is_error then "ERROR " else "" end)\(.content | if type == "string" then . else tostring end | .[0:200])"
                    ' 2>/dev/null || true
                    ;;
                result)
                    printf '%s' "$line" | jq -r '
                        "[session] turns=\(.num_turns) cost=$\(.cost_usd // 0)"
                    ' 2>/dev/null || true
                    ;;
            esac
        else
            printf '%s\n' "$line"
        fi
    done
}

case "${1:-}" in
    add)             shift; cmd_add_skill "$@" ;;
    remove)          shift; cmd_remove "$@" ;;
    list)            cmd_list ;;
    status)          shift; cmd_status "$@" ;;
    logs)            shift; cmd_logs "$@" ;;
    list-stuck)      cmd_list_stuck ;;
    clear-stuck)     shift; cmd_clear_stuck "$@" ;;
    list-done)       cmd_list_done ;;
    clear-done)      shift; cmd_clear_done "$@" ;;
    add-account)     shift; cmd_add_account "$@" ;;
    remove-account)  shift; cmd_remove_account "$@" ;;
    list-accounts)   cmd_list_accounts ;;
    *)               usage ;;
esac
{% endraw %}
```

- [ ] **Step 2: Verify bash syntax**

Run:
```bash
sed 's/{{[^}]*}}/"placeholder"/g; /^{%/d; s/{%[^%]*%}//g' \
    ansible/roles/claude-runner/templates/claude-runner.j2 | bash -n && echo "Syntax OK"
```

Expected: `Syntax OK`

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat(runner): replace add/add-instruction with 'add skill' command, update list/remove for new task dir structure"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task that implements it |
|---|---|
| `run.sh` has no domain logic — only orchestration | Task 5 |
| Skills are reusable Claude custom commands in `~/.claude/commands/` | Tasks 1, 3, 4 |
| Lifecycle hooks configured in `skills.conf` per skill type | Tasks 1, 5 |
| Hook dispatch order: `on_result.<outcome>` → `on_success` → `on_stuck` | Task 5 |
| Named queue: `tasks/<name>/queue`, one entry per line | Tasks 5, 6 |
| `result.json` format: `status`, `outcome`, `message` | Tasks 3, 4, 5 |
| Action scripts: `create_pr`, `notify_user`, `post_gitea_comment`, `chain_skill` | Task 2 |
| `add skill implement` with `spec=` validation | Task 6 |
| `add skill review-pr` with `pr=` + tea validation | Task 6 |
| `add skill handle-pr-comments` with `pr=` + tea validation | Task 6 |
| `list` shows queue depth and current skill | Task 6 |
| `chain_skill` prepends to queue | Task 2 |
| Per-instance task subdirectories | Tasks 5, 6 |
| Ansible installs commands, actions, skills.conf | Task 1 |

### Migration note

Existing runner instances use flat task files (`tasks/<name>.step` etc.) and the old `add-instruction` workflow. These are **not automatically migrated**. After deployment, existing instances must be cleared with `claude-runner remove <name>` and re-added with `claude-runner add skill implement <name> <repo-path> spec=<path>`. Document this in the PR description.

### Placeholder scan

No TBD or TODO markers present. All code blocks are complete. Variable names are consistent across tasks (`SKILL`, `SKILL_ARGS`, `TASK_DIR`, `QUEUE_FILE`, `RESULT_FILE`).
