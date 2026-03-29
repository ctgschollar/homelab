# Claude Runner: PR Watcher & Review Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PR title humanization, a repo watcher service that detects "changes requested" PRs and queues `handle-pr-comments` on the companion runner, and a redesigned `handle-pr-comments` skill that reads pre-classified work from watcher state rather than calling the Gitea API.

**Architecture:** Three enhancements to the claude-runner Ansible role: (1) `create_pr` derives its PR title from the branch name by stripping prefixes and reformatting; (2) a new `watch.sh` daemon polls Gitea reviews every 60s, classifies new comments into `to_implement`/`to_answer`, writes `details.json`, and queues the companion runner; (3) `handle-pr-comments` reads `details.json` and writes `replied/<id>` response files—all Gitea interaction is delegated to the new `resolve_watch` action script that posts a consolidated comment and re-requests reviews.

**Tech Stack:** Bash, Ansible/Jinja2, systemd, Gitea CLI (`tea`), Gitea REST API (curl), jq

---

## File Map

**Create:**
- `ansible/roles/claude-runner/templates/watch.sh.j2` — watcher poll loop (needs `{{ claude_runner_base_dir }}` template)
- `ansible/roles/claude-runner/templates/claude-watcher@.service.j2` — systemd service for the watcher
- `ansible/roles/claude-runner/files/actions/resolve_watch` — posts consolidated comment, updates state, cleans up lock

**Modify:**
- `ansible/roles/claude-runner/files/actions/create_pr` — derive title from branch name
- `ansible/roles/claude-runner/files/commands/handle-pr-comments.md` — read `details.json`, write `replied/<id>`, no Gitea calls
- `ansible/roles/claude-runner/templates/skills.conf.j2` — `handle-pr-comments` on_success → `resolve_watch pr=$PR`
- `ansible/roles/claude-runner/templates/claude-runner.j2` — add `add-watcher`, `remove-watcher`, `list-watchers`
- `ansible/roles/claude-runner/tasks/main.yml` — deploy new files

---

### Task 1: PR Title Humanization

**Files:**
- Modify: `ansible/roles/claude-runner/files/actions/create_pr`

- [ ] **Step 1: Rewrite `create_pr` with title derivation**

Full replacement for `ansible/roles/claude-runner/files/actions/create_pr`:

```bash
#!/bin/bash
# create_pr — push current branch and open a PR on Gitea
# Usage: create_pr --base <branch>
# Env: REPO_PATH (set by runner env file), NAME (instance name)
set -euo pipefail

BASE_DIR="${CLAUDE_RUNNER_BASE_DIR:-/opt/claude-runner}"

BASE_BRANCH="main"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE_BRANCH="$2"; shift 2 ;;
        *) echo "create_pr: unknown arg: $1" >&2; exit 1 ;;
    esac
done

BRANCH_FILE="${BASE_DIR}/tasks/${NAME}/branch"
if [[ ! -f "$BRANCH_FILE" ]]; then
    echo "create_pr: no branch file found at $BRANCH_FILE" >&2
    exit 1
fi
BRANCH=$(cat "$BRANCH_FILE")

if [[ "$BRANCH" == "$BASE_BRANCH" ]]; then
    echo "create_pr: head branch equals base branch (${BRANCH}). Did you mean to use base=main?" >&2
    exit 1
fi

# Derive a human-readable PR title from the branch name:
#   1. Strip leading <prefix>/ (e.g. "claude-runner/")
#   2. Strip leading YYYY-MM-DD- date prefix
#   3. Replace - and _ with spaces
#   4. Capitalise first letter
_derive_title() {
    local branch="$1"
    local slug="${branch#*/}"
    slug=$(echo "$slug" | sed 's/^[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}-//')
    slug="${slug//-/ }"
    slug="${slug//_/ }"
    echo "${slug^}"
}

TITLE="feat: $(_derive_title "$BRANCH")"

cd "${REPO_PATH:?REPO_PATH not set}"

git push -u origin "$BRANCH"

tea pr create \
    --title "$TITLE" \
    --base "$BASE_BRANCH" \
    --head "$BRANCH" \
    --description "Automated implementation by claude-runner instance: ${NAME}"

echo "PR created: branch=${BRANCH} base=${BASE_BRANCH} title=${TITLE}"
```

- [ ] **Step 2: Verify shell syntax**

```bash
bash -n ansible/roles/claude-runner/files/actions/create_pr
```
Expected: no output (no errors)

- [ ] **Step 3: Verify title derivation examples**

```bash
bash -c '
_derive_title() {
    local branch="$1"
    local slug="${branch#*/}"
    slug=$(echo "$slug" | sed '"'"'s/^[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}-//'"'"')
    slug="${slug//-/ }"
    slug="${slug//_/ }"
    echo "${slug^}"
}
echo "feat: $(_derive_title "claude-runner/homelab")"
echo "feat: $(_derive_title "claude-runner/2026-03-29-gitea-runner")"
echo "feat: $(_derive_title "claude-runner/add-dns-entries")"
echo "feat: $(_derive_title "feature/my_cool_feature")"
'
```
Expected:
```
feat: Homelab
feat: Gitea runner
feat: Add dns entries
feat: My cool feature
```

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/files/actions/create_pr
git commit -m "feat: derive PR title from branch name instead of instance name"
```

---

### Task 2: `handle-pr-comments` Skill Redesign

**Files:**
- Modify: `ansible/roles/claude-runner/files/commands/handle-pr-comments.md`

The skill no longer calls the Gitea API. It reads pre-classified work from `details.json` and writes response files to `replied/<id>`.

- [ ] **Step 1: Rewrite `handle-pr-comments.md`**

Full replacement for `ansible/roles/claude-runner/files/commands/handle-pr-comments.md`:

```markdown
You are resolving review comments on a pull request.

**Arguments:** $ARGUMENTS

Parse these key=value pairs:
- `pr`: PR number
- `step`: current step (always 1 for this skill)
- `total`: total steps (always 1 for this skill)
- `name`: runner instance name
- `repo`: absolute path to the local repository

**Result file:** `/opt/claude-runner/tasks/<name>/result.json`

## Rules

- Work fully autonomously — no asking for approval.
- Only mark stuck if you cannot push the branch (auth failure or unresolvable merge conflict). Insufficient context for a specific comment is handled by writing a clarifying question — never by getting stuck.
- Always write `result.json` as your **final action**.
- **No Gitea API calls.** The watcher has classified the work; `resolve_watch` handles all Gitea interaction.

## Resolution Process

1. Read `$CLAUDE_RUNNER_BASE_DIR/tasks/$NAME/watch/$PR/details.json`
2. Extract `branch` from `details.json` and check out that branch:
   ```bash
   git -C <repo> checkout <branch>
   ```
3. **For each entry in `to_implement` (identified by `id`):**
   - Implement the requested change in the codebase
   - If insufficient context, write a clarifying question as the response (do not get stuck)
   - Write response text to `$CLAUDE_RUNNER_BASE_DIR/tasks/$NAME/watch/$PR/replied/<id>`
   - Response text is plain text only — no quoting, no author attribution
4. **For each entry in `to_answer` (identified by `id`):**
   - Write the answer text to `$CLAUDE_RUNNER_BASE_DIR/tasks/$NAME/watch/$PR/replied/<id>`
5. If any code was changed, commit and push:
   ```bash
   git -C <repo> add -A
   git -C <repo> commit -m "fix: address PR review comments"
   git -C <repo> push
   ```
   Skip commit entirely if no files were changed.
6. Write result.json:

   All comments addressed:
   ```json
   {"status": "done", "outcome": "resolved", "message": "All review comments addressed"}
   ```

   Cannot push (only valid stuck condition):
   ```json
   {"status": "stuck", "outcome": "stuck", "message": "<push failure details>"}
   ```

## Response File Examples

For a successfully implemented change (`replied/1001`):
```
Implemented.
```

For a clarifying question (`replied/1002`):
```
I don't have enough context to implement this safely — could you clarify whether
this change should also apply to the staging config?
```
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/claude-runner/files/commands/handle-pr-comments.md
git commit -m "feat: redesign handle-pr-comments to read details.json and write replied/ files"
```

---

### Task 3: `resolve_watch` Action Script

**Files:**
- Create: `ansible/roles/claude-runner/files/actions/resolve_watch`

Called as the `on_success` hook for `handle-pr-comments`. Reads `replied/<id>` files, posts a consolidated Gitea comment, updates `posted_comments.json`, re-requests reviews, removes the lock.

- [ ] **Step 1: Create `resolve_watch`**

Write the following to `ansible/roles/claude-runner/files/actions/resolve_watch`:

```bash
#!/bin/bash
# resolve_watch — post consolidated review response and clean up watcher state
# Usage: resolve_watch pr=<number>
# Env: CLAUDE_RUNNER_BASE_DIR, NAME, REPO_PATH
set -euo pipefail

PR_NUM=""
for arg in "$@"; do
    case "$arg" in
        pr=*) PR_NUM="${arg#pr=}" ;;
        *) echo "resolve_watch: unknown arg: $arg" >&2; exit 1 ;;
    esac
done

if [[ -z "$PR_NUM" ]]; then
    echo "resolve_watch: requires pr=<number>" >&2
    exit 1
fi

BASE_DIR="${CLAUDE_RUNNER_BASE_DIR:-/opt/claude-runner}"
WATCH_DIR="${BASE_DIR}/tasks/${NAME}/watch/${PR_NUM}"
DETAILS_FILE="${WATCH_DIR}/details.json"
REPLIED_DIR="${WATCH_DIR}/replied"
POSTED_FILE="${WATCH_DIR}/posted_comments.json"

if [[ ! -f "$DETAILS_FILE" ]]; then
    echo "resolve_watch: no details.json at ${DETAILS_FILE}" >&2
    exit 1
fi

if [[ ! -d "$REPLIED_DIR" ]] || [[ -z "$(ls -A "$REPLIED_DIR" 2>/dev/null)" ]]; then
    echo "resolve_watch: no replied files in ${REPLIED_DIR}" >&2
    exit 1
fi

# Build consolidated comment body from replied/ files.
# Format per section:
#   > @author: original body
#
#   response text
#
# Sections separated by ---
_build_comment() {
    local first=true
    echo "## Review Response"
    echo ""
    for replied_file in "$REPLIED_DIR"/*; do
        [[ -f "$replied_file" ]] || continue
        local comment_id response_text author body
        comment_id=$(basename "$replied_file")
        response_text=$(cat "$replied_file")
        author=$(jq -r --arg id "$comment_id" '
            (.to_implement + .to_answer)[] |
            select(.id == ($id | tonumber)) | .author
        ' "$DETAILS_FILE" 2>/dev/null || echo "unknown")
        body=$(jq -r --arg id "$comment_id" '
            (.to_implement + .to_answer)[] |
            select(.id == ($id | tonumber)) | .body
        ' "$DETAILS_FILE" 2>/dev/null || echo "")
        if [[ "$first" != "true" ]]; then
            echo ""
            echo "---"
            echo ""
        fi
        first=false
        echo "> @${author}: ${body}"
        echo ""
        echo "${response_text}"
    done
}

COMMENT=$(_build_comment)

# Post consolidated comment to Gitea
cd "${REPO_PATH:?REPO_PATH not set}"
tea issue comment "$PR_NUM" --body "$COMMENT"
echo "resolve_watch: posted consolidated comment on PR #${PR_NUM}"

# Update posted_comments.json — append IDs of all replied comments
posted_ids=$(cat "$POSTED_FILE" 2>/dev/null || echo '[]')
for replied_file in "$REPLIED_DIR"/*; do
    [[ -f "$replied_file" ]] || continue
    comment_id=$(basename "$replied_file")
    posted_ids=$(echo "$posted_ids" | jq --arg id "$comment_id" '. + [($id | tonumber)]')
done
echo "$posted_ids" > "$POSTED_FILE"
echo "resolve_watch: updated posted_comments.json"

# Re-request reviews from all reviewers who requested changes
mapfile -t reviewers < <(jq -r '.reviewers_requested_changes[]' "$DETAILS_FILE" 2>/dev/null || true)

if [[ ${#reviewers[@]} -gt 0 ]]; then
    TEA_CONFIG="${HOME}/.config/tea/config.yml"
    if [[ ! -f "$TEA_CONFIG" ]]; then
        echo "resolve_watch: warning: tea config not found, skipping review re-request" >&2
    else
        GITEA_URL=$(grep -A10 'logins:' "$TEA_CONFIG" | grep '^\s*url:' | head -1 | sed 's/.*url:[[:space:]]*//')
        GITEA_TOKEN=$(grep -A10 'logins:' "$TEA_CONFIG" | grep '^\s*token:' | head -1 | sed 's/.*token:[[:space:]]*//')

        REMOTE_URL=$(git remote get-url origin)
        OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*[:/]([^/]+/[^/]+)(\.git)?$|\1|')
        OWNER="${OWNER_REPO%%/*}"
        REPO_NAME="${OWNER_REPO#*/}"

        reviewers_json=$(printf '%s\n' "${reviewers[@]}" | jq -Rs '[split("\n")[] | select(length > 0)]')

        curl -sf -X POST \
            "${GITEA_URL}/api/v1/repos/${OWNER}/${REPO_NAME}/pulls/${PR_NUM}/requested_reviewers" \
            -H "Authorization: token ${GITEA_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "{\"reviewers\": ${reviewers_json}}" > /dev/null
        echo "resolve_watch: re-requested reviews from: ${reviewers[*]}"
    fi
fi

# Cleanup: remove lock, details.json, replied/ — leave posted_comments.json intact
rm -f "${WATCH_DIR}/lock"
rm -f "${WATCH_DIR}/details.json"
rm -rf "${WATCH_DIR}/replied"
echo "resolve_watch: cleanup complete for PR #${PR_NUM}"
```

- [ ] **Step 2: Make executable and verify syntax**

```bash
chmod +x ansible/roles/claude-runner/files/actions/resolve_watch
bash -n ansible/roles/claude-runner/files/actions/resolve_watch
```
Expected: no output from bash -n (no syntax errors)

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/files/actions/resolve_watch
git commit -m "feat: add resolve_watch action to post consolidated review response and clean up"
```

---

### Task 4: Update `skills.conf.j2`

**Files:**
- Modify: `ansible/roles/claude-runner/templates/skills.conf.j2`

- [ ] **Step 1: Update `[handle-pr-comments]` hooks**

In `ansible/roles/claude-runner/templates/skills.conf.j2`, replace:
```ini
[handle-pr-comments]
on_success=notify_user "PR comments resolved"
on_stuck=notify_user "could not resolve comments: $MESSAGE"
```

With:
```ini
[handle-pr-comments]
on_success=resolve_watch pr=$PR
on_stuck=notify_user "could not resolve PR $PR comments: $MESSAGE"
```

- [ ] **Step 2: Verify the change**

```bash
grep -A3 '\[handle-pr-comments\]' ansible/roles/claude-runner/templates/skills.conf.j2
```
Expected:
```
[handle-pr-comments]
on_success=resolve_watch pr=$PR
on_stuck=notify_user "could not resolve PR $PR comments: $MESSAGE"
```

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/skills.conf.j2
git commit -m "feat: update handle-pr-comments to call resolve_watch on success"
```

---

### Task 5: Watcher Script `watch.sh.j2`

**Files:**
- Create: `ansible/roles/claude-runner/templates/watch.sh.j2`

The main poll loop. Runs every 60 seconds, checks each open PR for "changes requested" reviews, classifies new comments, writes `details.json`, creates a lock, and queues `handle-pr-comments`.

- [ ] **Step 1: Create `watch.sh.j2`**

Write the following to `ansible/roles/claude-runner/templates/watch.sh.j2`:

```bash
#!/bin/bash
# claude-watcher — poll a Gitea repo for PRs with changes requested
# Managed by Ansible — do not edit directly.
# Source: ansible/roles/claude-runner/templates/watch.sh.j2
set -euo pipefail

REPO_PATH="${1:?Usage: watch.sh <repo-path> <name>}"
NAME="${2:?Usage: watch.sh <repo-path> <name>}"

BASE_DIR="{{ claude_runner_base_dir }}"
TASK_DIR="${BASE_DIR}/tasks/${NAME}"
WATCH_DIR="${TASK_DIR}/watch"
LOG_FILE="${BASE_DIR}/logs/${NAME}-watcher.log"

mkdir -p "$WATCH_DIR"
cd "${REPO_PATH}"

{% raw %}
log() {
    echo "$(date -Iseconds) [watcher/${NAME}] $*" | tee -a "$LOG_FILE"
}

# Read a value from tea's config YAML (first login entry).
_tea_config_val() {
    local key="$1"
    local tea_config="${HOME}/.config/tea/config.yml"
    [[ -f "$tea_config" ]] || { echo ""; return; }
    grep -A20 'logins:' "$tea_config" | grep "^\s*${key}:" | head -1 | sed "s/.*${key}:[[:space:]]*//"
}

# Extract owner/repo slug from git remote origin.
_owner_repo() {
    local remote
    remote=$(git remote get-url origin)
    echo "$remote" | sed -E 's|.*[:/]([^/]+/[^/]+)(\.git)?$|\1|'
}

log "Watcher started for: ${REPO_PATH}"

while true; do
    open_prs_json=$(tea pr list --state open --output json 2>/dev/null || echo '[]')
    pr_count=$(echo "$open_prs_json" | jq 'length')
    log "Polling: ${pr_count} open PR(s)"

    GITEA_URL=$(_tea_config_val "url")
    GITEA_TOKEN=$(_tea_config_val "token")
    OWNER_REPO=$(_owner_repo)
    OWNER="${OWNER_REPO%%/*}"
    REPO_NAME="${OWNER_REPO#*/}"

    if [[ -z "$GITEA_URL" || -z "$GITEA_TOKEN" ]]; then
        log "ERROR: cannot read Gitea credentials from tea config — check ~/.config/tea/config.yml"
        sleep 60
        continue
    fi

    while read -r pr_num; do
        [[ -z "$pr_num" ]] && continue

        # Check lock first (cheapest check)
        lock_file="${WATCH_DIR}/${pr_num}/lock"
        if [[ -f "$lock_file" ]]; then
            log "PR #${pr_num}: locked (runner processing), skipping"
            continue
        fi

        # Fetch review state via Gitea API
        reviews_json=$(curl -sf \
            "${GITEA_URL}/api/v1/repos/${OWNER}/${REPO_NAME}/pulls/${pr_num}/reviews" \
            -H "Authorization: token ${GITEA_TOKEN}" \
            2>/dev/null || echo '[]')

        changes_count=$(echo "$reviews_json" | jq '[.[] | select(.state == "REQUEST_CHANGES")] | length')
        if [[ "$changes_count" -eq 0 ]]; then
            log "PR #${pr_num}: no changes requested, skipping"
            continue
        fi

        # Collect reviewers who requested changes
        reviewers_json=$(echo "$reviews_json" | \
            jq '[.[] | select(.state == "REQUEST_CHANGES") | .user.login]')

        # Get PR branch name
        pr_json=$(tea pr view "$pr_num" --output json 2>/dev/null || echo '{}')
        pr_branch=$(echo "$pr_json" | jq -r '.head.ref // empty')

        # Fetch all comments on the PR issue
        comments_json=$(tea issue comment list "$pr_num" --output json 2>/dev/null || echo '[]')

        # Read already-posted comment IDs (default to empty array)
        posted_file="${WATCH_DIR}/${pr_num}/posted_comments.json"
        posted_ids=$(cat "$posted_file" 2>/dev/null || echo '[]')

        # Filter out already-posted comments; classify remainder
        to_implement=$(echo "$comments_json" | jq --argjson posted "$posted_ids" '
            [.[] |
             select(.id as $id | $posted | map(. == $id) | any | not) |
             select(.body | test("^question:"; "i") | not) |
             {id: .id, author: .user.login, body: .body}]
        ')
        to_answer=$(echo "$comments_json" | jq --argjson posted "$posted_ids" '
            [.[] |
             select(.id as $id | $posted | map(. == $id) | any | not) |
             select(.body | test("^question:"; "i")) |
             {id: .id, author: .user.login, body: .body}]
        ')

        implement_count=$(echo "$to_implement" | jq 'length')
        answer_count=$(echo "$to_answer" | jq 'length')

        if [[ "$implement_count" -eq 0 && "$answer_count" -eq 0 ]]; then
            log "PR #${pr_num}: no new comments to process"
            continue
        fi

        log "PR #${pr_num}: ${implement_count} to implement, ${answer_count} to answer — queuing runner"

        # Write details.json and create replied/ directory
        mkdir -p "${WATCH_DIR}/${pr_num}/replied"
        jq -n \
            --argjson pr "$pr_num" \
            --arg branch "$pr_branch" \
            --arg captured_at "$(date -Iseconds)" \
            --argjson reviewers_requested_changes "$reviewers_json" \
            --argjson to_implement "$to_implement" \
            --argjson to_answer "$to_answer" \
            '{
                pr: $pr,
                branch: $branch,
                captured_at: $captured_at,
                reviewers_requested_changes: $reviewers_requested_changes,
                to_implement: $to_implement,
                to_answer: $to_answer
            }' > "${WATCH_DIR}/${pr_num}/details.json"

        # Create lock and queue the skill
        touch "$lock_file"

        queue_file="${TASK_DIR}/queue"
        echo "handle-pr-comments pr=${pr_num}" >> "$queue_file"
        chmod 664 "$queue_file"

        # Clear done file if present (runner may have finished a previous queue)
        rm -f "${TASK_DIR}/done"

        # Start companion runner if not already active
        if ! systemctl is-active --quiet "claude-runner@${NAME}.service" 2>/dev/null; then
            systemctl start "claude-runner@${NAME}.service" 2>/dev/null || \
                log "Warning: could not start claude-runner@${NAME} (may need sudoers rule)"
        fi

    done < <(echo "$open_prs_json" | jq -r '.[].number')

    sleep 60
done
{% endraw %}
```

- [ ] **Step 2: Verify Jinja2 raw block structure**

```bash
grep -n '{%' ansible/roles/claude-runner/templates/watch.sh.j2
```
Expected: lines showing only `{% raw %}` and `{% endraw %}` (plus `{% raw %}` wrapping the shell body).

- [ ] **Step 3: Verify bash syntax on the shell portions**

```bash
# Strip Jinja2 directives and check bash syntax
sed '/^{%/d; s/{{ claude_runner_base_dir }}/\/opt\/claude-runner/g' \
    ansible/roles/claude-runner/templates/watch.sh.j2 | bash -n
```
Expected: no output (no syntax errors)

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/templates/watch.sh.j2
git commit -m "feat: add watch.sh watcher script to poll Gitea for changes-requested PRs"
```

---

### Task 6: Watcher Systemd Service Template

**Files:**
- Create: `ansible/roles/claude-runner/templates/claude-watcher@.service.j2`

Mirrors `claude-runner@.service.j2` but runs `watch.sh` and uses a longer restart delay (30s) since the watcher is a continuous poll loop.

- [ ] **Step 1: Create `claude-watcher@.service.j2`**

Write the following to `ansible/roles/claude-runner/templates/claude-watcher@.service.j2`:

```ini
[Unit]
Description=Claude Code PR watcher for %i
After=network-online.target
Wants=network-online.target

[Service]
User={{ claude_user }}
Environment=PATH={{ claude_user_home }}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile={{ claude_runner_base_dir }}/env/%i
ExecStart={{ claude_runner_base_dir }}/watch.sh ${REPO_PATH} %i
Restart=always
RestartSec=30
RestartPreventExitStatus=0
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Verify the template**

```bash
grep -c 'claude_runner_base_dir\|claude_user' ansible/roles/claude-runner/templates/claude-watcher@.service.j2
```
Expected: `4` (four Jinja2 variable references)

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-watcher@.service.j2
git commit -m "feat: add claude-watcher@ systemd service template"
```

---

### Task 7: Watcher CLI Commands in `claude-runner.j2`

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

Add `add-watcher <name> <repo-path>`, `remove-watcher <name>`, and `list-watchers` commands to the CLI.

- [ ] **Step 1: Add watcher commands to usage string**

In `claude-runner.j2`, locate the usage function. After the `add-account` and `list-accounts` lines in the help text, add:

```
  add-watcher <name> <repo-path>     Start watching for <name>'s companion runner (runner must exist)
  remove-watcher <name>              Stop and remove the watcher for <name>
  list-watchers                      Show all registered watchers and their status
```

- [ ] **Step 2: Add the three command functions**

Insert these functions after `cmd_list_accounts` and before the final `case` dispatch block:

```bash
cmd_add_watcher() {
    local name="${1:?add-watcher requires a name}"
    local repo_path="${2:-}"  # validated indirectly via companion runner's env file

    # Companion runner must be registered
    if ! grep -q "^${name}=" "$REPOS_CONF" 2>/dev/null; then
        echo "Error: no runner instance named '${name}'. Register a runner with 'add skill' first." >&2
        exit 1
    fi

    local env_file="${ENV_DIR}/${name}"
    if [[ ! -f "$env_file" ]]; then
        echo "Error: env file not found at ${env_file}" >&2
        exit 1
    fi

    systemctl daemon-reload
    systemctl enable "claude-watcher@${name}.service"
    systemctl start "claude-watcher@${name}.service"
    echo "Started claude-watcher@${name}"
}

cmd_remove_watcher() {
    local name="${1:?remove-watcher requires a name}"

    if systemctl is-active --quiet "claude-watcher@${name}.service" 2>/dev/null; then
        systemctl stop "claude-watcher@${name}.service"
    fi
    systemctl disable "claude-watcher@${name}.service" 2>/dev/null || true
    echo "Removed claude-watcher@${name}"
}

cmd_list_watchers() {
    local found=0
    printf "%-20s %-10s\n" "NAME" "STATUS"
    printf "%-20s %-10s\n" "----" "------"

    while IFS='=' read -r name _path; do
        [[ -z "$name" || "$name" == \#* ]] && continue
        local svc="claude-watcher@${name}.service"
        if systemctl list-unit-files "$svc" --no-legend 2>/dev/null | grep -q "$svc"; then
            local status
            status=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
            printf "%-20s %-10s\n" "$name" "$status"
            found=1
        fi
    done < "$REPOS_CONF"

    [[ $found -eq 0 ]] && echo "No watchers registered."
}
```

- [ ] **Step 3: Add dispatch cases**

In the `case "${1:-}"` block at the bottom of the file, add these three lines:

```bash
    add-watcher)     shift; cmd_add_watcher "$@" ;;
    remove-watcher)  shift; cmd_remove_watcher "$@" ;;
    list-watchers)   cmd_list_watchers ;;
```

- [ ] **Step 4: Verify syntax by checking the non-Jinja2 portions**

```bash
sed '/^{%/d; /{{/d' ansible/roles/claude-runner/templates/claude-runner.j2 | bash -n
```
Expected: no output (no syntax errors)

- [ ] **Step 5: Confirm all three new commands appear in case block**

```bash
grep -E 'add-watcher|remove-watcher|list-watchers' ansible/roles/claude-runner/templates/claude-runner.j2 | grep -v 'cmd_\|#\|echo\|Error\|printf'
```
Expected: three dispatch lines in the case block.

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat: add add-watcher, remove-watcher, list-watchers CLI commands"
```

---

### Task 8: Ansible Deployment Tasks

**Files:**
- Modify: `ansible/roles/claude-runner/tasks/main.yml`

Deploy the new watcher script, watcher service, and `resolve_watch` action.

- [ ] **Step 1: Add watcher script install task**

After the "Install systemd template unit" task (the `claude-runner@.service` task), insert:

```yaml
- name: Install watcher script
  template:
    src: watch.sh.j2
    dest: "{{ claude_runner_base_dir }}/watch.sh"
    owner: "{{ claude_user }}"
    group: "{{ claude_user }}"
    mode: '0755'

- name: Install watcher systemd template unit
  template:
    src: claude-watcher@.service.j2
    dest: /etc/systemd/system/claude-watcher@.service
    owner: root
    group: root
    mode: '0644'
  notify: reload systemd
```

- [ ] **Step 2: Add `resolve_watch` to the action scripts loop**

In the "Install action scripts" task, update the `loop` list to include `resolve_watch`:

```yaml
  loop:
    - create_pr
    - notify_user
    - post_gitea_comment
    - chain_skill
    - resolve_watch
```

- [ ] **Step 3: Verify YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('ansible/roles/claude-runner/tasks/main.yml')); print('YAML OK')"
```
Expected: `YAML OK`

- [ ] **Step 4: Verify new items appear in tasks file**

```bash
grep -E 'watch\.sh|claude-watcher|resolve_watch' ansible/roles/claude-runner/tasks/main.yml
```
Expected: at least three matching lines (watch.sh template, claude-watcher service, resolve_watch in loop).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/claude-runner/tasks/main.yml
git commit -m "feat: deploy watch.sh, claude-watcher@.service, and resolve_watch via Ansible"
```

---

## Self-Review Checklist

### Spec Coverage

| Spec requirement | Covered by |
|---|---|
| PR title derived from branch name | Task 1 |
| Strip `<prefix>/` prefix | Task 1 step 1 (`${branch#*/}`) |
| Strip `YYYY-MM-DD-` date prefix | Task 1 step 1 (`sed`) |
| Replace `-`/`_` with spaces | Task 1 step 1 |
| Capitalise first letter | Task 1 step 1 (`${slug^}`) |
| `watch.sh` poll loop every 60s | Task 5 (`sleep 60`) |
| Fetch open PRs via `tea pr list --state open` | Task 5 |
| Check review state (changes requested) via Gitea API | Task 5 (curl `/reviews`) |
| Skip if lock present | Task 5 (lock_file check) |
| Fetch comments via `tea issue comment list` | Task 5 |
| Filter by `posted_comments.json` | Task 5 (jq filter) |
| Classify `question:` → to_answer | Task 5 (jq `test("^question:"; "i")`) |
| Classify untagged → to_implement | Task 5 |
| Skip if both lists empty | Task 5 (implement_count + answer_count check) |
| Write `details.json` schema | Task 5 (jq -n) |
| Create `lock` file | Task 5 (`touch "$lock_file"`) |
| Queue `handle-pr-comments pr=<n>` | Task 5 (append to queue) |
| Start companion runner if not active | Task 5 (systemctl start) |
| Lock created on queue | Task 5 |
| Lock removed by `resolve_watch` | Task 3 (`rm -f lock`) |
| `add-watcher` CLI command | Task 7 |
| `remove-watcher` CLI command | Task 7 |
| `list-watchers` CLI command | Task 7 |
| `add-watcher` validates companion runner exists | Task 7 (grep REPOS_CONF) |
| `claude-watcher@<name>.service` systemd template | Task 6 |
| `resolve_watch` reads `replied/` files | Task 3 |
| `resolve_watch` builds consolidated comment | Task 3 (`_build_comment`) |
| Consolidated comment format (`> @author: body`) | Task 3 |
| Sections separated by `---` | Task 3 |
| `resolve_watch` posts via `tea issue comment` | Task 3 |
| `resolve_watch` updates `posted_comments.json` | Task 3 |
| `resolve_watch` re-requests reviews via Gitea API | Task 3 (curl POST) |
| `resolve_watch` removes lock, details.json, replied/ | Task 3 |
| `posted_comments.json` survives cleanup | Task 3 (only lock/details/replied removed) |
| `skills.conf` on_success → `resolve_watch pr=$PR` | Task 4 |
| `skills.conf` on_stuck → notify_user with PR number | Task 4 |
| Skill reads `details.json` from watcher state dir | Task 2 |
| Skill checks out PR branch from `details.json` | Task 2 |
| Skill writes `replied/<id>` for each comment | Task 2 |
| Skill commits and pushes code changes | Task 2 |
| Skill skips commit if no code changed | Task 2 |
| Stuck only on push failure | Task 2 |
| No Gitea API calls in skill | Task 2 |
| Ansible deploys watch.sh | Task 8 |
| Ansible deploys claude-watcher@.service | Task 8 |
| Ansible deploys resolve_watch action | Task 8 |

All spec requirements covered.
