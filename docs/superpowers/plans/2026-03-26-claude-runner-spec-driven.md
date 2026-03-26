# Claude Runner Spec-Driven Workflow + PR Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the claude-runner ansible role so the runner accepts a spec only (generating its own plan at runtime), works on a dedicated git branch, and creates a Gitea PR when done.

**Architecture:** Three files change. `tasks/main.yml` gains `gh` CLI installation. `run.sh.j2` gains a planning pass (branch creation + Claude invocation to write the plan) inserted before the existing step sequencer, plus PR creation when all steps complete. `claude-runner.j2` loses the plan selection menu from `add-instruction` and gains spec-ref state tracking, planning state reset, and spec filename validation.

**Tech Stack:** Ansible, Bash, Jinja2 templates, systemd, gh CLI

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `ansible/roles/claude-runner/tasks/main.yml` | Modify | Add gh CLI apt key, repo, and package |
| `ansible/roles/claude-runner/templates/run.sh.j2` | Modify | Add SPEC_REF_FILE/BRANCH_FILE vars; remove pre-loop START_SHA write; insert planning pass; replace all-done exit with PR creation |
| `ansible/roles/claude-runner/templates/claude-runner.j2` | Modify | Rewrite cmd_add_instruction (remove plan menu, add spec-ref write + state reset + validation); update cmd_remove and cmd_list |

---

## Task 1: Install gh CLI in the ansible role

**Files:**
- Modify: `ansible/roles/claude-runner/tasks/main.yml`

- [ ] **Step 1: Add gh CLI tasks after the `Install system dependencies` apt task**

After the existing `Install system dependencies` task block, insert:

```yaml
- name: Add GitHub CLI apt signing key
  get_url:
    url: https://cli.github.com/packages/githubcli-archive-keyring.gpg
    dest: /usr/share/keyrings/githubcli-archive-keyring.gpg
    mode: '0644'

- name: Add GitHub CLI apt repository
  apt_repository:
    repo: "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main"
    filename: github-cli
    state: present

- name: Install gh CLI
  apt:
    name: gh
    state: present
    update_cache: yes
```

- [ ] **Step 2: Verify ansible syntax**

```bash
ansible-playbook --syntax-check ansible/deploy-claude-runner.yml
```

Expected: `playbook: ansible/deploy-claude-runner.yml` with no errors.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/tasks/main.yml
git commit -m "feat: install gh CLI in claude-runner ansible role"
```

---

## Task 2: Update run.sh.j2 — new state variables and remove pre-loop SHA write

**Files:**
- Modify: `ansible/roles/claude-runner/templates/run.sh.j2`

- [ ] **Step 1: Add SPEC_REF_FILE and BRANCH_FILE variable declarations**

After the existing `START_SHA_FILE` declaration line:
```bash
START_SHA_FILE="${TASK_DIR}/${NAME}.start-sha"
```

Add:
```bash
SPEC_REF_FILE="${TASK_DIR}/${NAME}.spec-ref"
BRANCH_FILE="${TASK_DIR}/${NAME}.branch"
```

- [ ] **Step 2: Remove the pre-loop START_SHA_FILE write block**

Remove these lines entirely (they appear just before `while true;`):
```bash
# Record the starting commit once — used to build the progress log on every run
if [[ ! -f "$START_SHA_FILE" ]]; then
    git rev-parse HEAD > "$START_SHA_FILE"
fi
```

The planning pass now owns this write (after branch creation), so it must not be pre-empted here.

- [ ] **Step 3: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/run.sh.j2
```

Expected: no errors (Jinja2 `{{ }}` warnings are acceptable and harmless).

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/templates/run.sh.j2
git commit -m "refactor: add SPEC_REF_FILE/BRANCH_FILE vars; remove pre-loop START_SHA write"
```

---

## Task 3: Update run.sh.j2 — insert planning pass

**Files:**
- Modify: `ansible/roles/claude-runner/templates/run.sh.j2`

- [ ] **Step 1: Insert the planning pass block**

Locate the `if [[ ! -f "$TASK_FILE" ]]; then` block. The planning pass goes **immediately after** this block (after its `continue`), **before** the `STEP=$(cat "$STEP_FILE" ...)` line.

Insert this block:

```bash
    # Planning pass: generate plan if .total does not exist yet
    if [[ ! -f "$TOTAL_FILE" ]]; then
        if [[ ! -f "$SPEC_REF_FILE" ]]; then
            echo "$(date -Iseconds) No spec-ref file for ${NAME}. Re-run: claude-runner add-instruction ${NAME}" | tee -a "$LOG_FILE"
            sleep 30
            continue
        fi

        SPEC_REF=$(cat "$SPEC_REF_FILE")
        PLAN_NAME="${SPEC_REF%-design.md}.md"
        EXPECTED_PLAN="${REPO_PATH}/docs/superpowers/plans/${PLAN_NAME}"

        # One-time setup: create branch and record start SHA
        if [[ ! -f "$BRANCH_FILE" ]]; then
            BRANCH="claude-runner/${NAME}"
            git checkout -B "$BRANCH"
            echo "$BRANCH" > "$BRANCH_FILE"
            git rev-parse HEAD > "$START_SHA_FILE"
        fi

        {
            cat "$TASK_FILE"
            printf '\n\n## Your job — PLANNING PHASE\n\n'
            printf 'You are in the planning phase. Do not write any implementation code yet.\n\n'
            printf '1. Read the spec above carefully.\n'
            printf '2. Invoke the `superpowers:writing-plans` skill to create an implementation plan.\n'
            printf '3. The writing-plans skill will save the plan to docs/superpowers/plans/ — let it use its default naming.\n'
            printf '4. Commit the plan file with: git add docs/superpowers/plans/ && git commit -m "plan: %s"\n' "$NAME"
            printf '5. Stop. Do not implement anything.\n'
        } | claude --print --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE"

        if [[ -f "$EXPECTED_PLAN" ]]; then
            cp "$EXPECTED_PLAN" "${TASK_DIR}/${NAME}.plan-1.md"
            echo "1" > "$TOTAL_FILE"
            echo "1" > "$STEP_FILE"
            echo "$(date -Iseconds) Planning complete. Plan: ${PLAN_NAME}" | tee -a "$LOG_FILE"
        else
            echo "$(date -Iseconds) Planning pass did not produce expected plan file: ${PLAN_NAME}. Retrying." | tee -a "$LOG_FILE"
            sleep 30
        fi
        continue
    fi
```

- [ ] **Step 2: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/run.sh.j2
```

Expected: no errors.

- [ ] **Step 3: Verify loop ordering by reading the file**

Open `ansible/roles/claude-runner/templates/run.sh.j2` and confirm the loop body order is:
1. `[[ -f "$DONE_FILE" ]]` check
2. `[[ -f "$STUCK_FILE" ]]` check
3. `[[ ! -f "$TASK_FILE" ]]` check
4. `[[ ! -f "$TOTAL_FILE" ]]` — planning pass (newly added)
5. `STEP=$(cat "$STEP_FILE" ...)` — existing step sequencer

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/templates/run.sh.j2
git commit -m "feat: add planning pass to run.sh (branch creation + writing-plans invocation)"
```

---

## Task 4: Update run.sh.j2 — replace all-done exit with PR creation

**Files:**
- Modify: `ansible/roles/claude-runner/templates/run.sh.j2`

- [ ] **Step 1: Replace the all-steps-done exit block**

Find this existing block inside the `if [[ -f "$STEP_DONE_FILE" ]]; then` branch:

```bash
        if [[ "$STEP" -ge "$TOTAL" ]]; then
            echo "$(date -Iseconds) All ${TOTAL} steps complete." | tee -a "$LOG_FILE"
            echo "All ${TOTAL} steps completed." > "$DONE_FILE"
            exit 0
```

Replace it with:

```bash
        if [[ "$STEP" -ge "$TOTAL" ]]; then
            echo "$(date -Iseconds) All ${TOTAL} steps complete." | tee -a "$LOG_FILE"
            BRANCH=$(cat "$BRANCH_FILE")
            git push -u origin "$BRANCH" 2>&1 | tee -a "$LOG_FILE"
            gh pr create \
                --title "feat: ${NAME}" \
                --base main \
                --head "$BRANCH" \
                --body "Automated implementation of spec: $(cat "$SPEC_REF_FILE")" \
                2>&1 | tee -a "$LOG_FILE"
            echo "All ${TOTAL} steps completed." > "$DONE_FILE"
            exit 0
```

- [ ] **Step 2: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/run.sh.j2
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/run.sh.j2
git commit -m "feat: create Gitea PR on completion instead of direct main commit"
```

---

## Task 5: Update claude-runner.j2 — rewrite cmd_add_instruction

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

- [ ] **Step 1: Replace the entire cmd_add_instruction function**

The function currently runs from `cmd_add_instruction() {` through its closing `}`. Replace the entire function with:

```bash
cmd_add_instruction() {
    local name="${1:?add-instruction requires a name}"

    if ! grep -q "^${name}=" "$REPOS_CONF" 2>/dev/null; then
        echo "Error: '${name}' is not registered. Use: claude-runner add ${name} <path>" >&2
        exit 1
    fi

    local task_file="${TASK_DIR}/${name}.md"
    local env_file="${ENV_DIR}/${name}"
    local repo_path
    repo_path=$(grep "^REPO_PATH=" "$env_file" 2>/dev/null | cut -d= -f2-)

    # --- Spec selection ---
    local spec_file=""
    local spec_rel=""
    local spec_dir="${repo_path}/docs/superpowers/specs"
    if [[ -d "$spec_dir" ]]; then
        mapfile -t spec_files < <(find "$spec_dir" -maxdepth 1 -name "*.md" | sort)
        if [[ ${#spec_files[@]} -gt 0 ]]; then
            echo ""
            echo "Select spec file:"
            local i=1
            for f in "${spec_files[@]}"; do
                printf "  %d) %s\n" "$i" "$(basename "$f")"
                ((i++))
            done
            read -rp "> " spec_idx
            if ! [[ "$spec_idx" =~ ^[0-9]+$ ]] || (( spec_idx < 1 || spec_idx > ${#spec_files[@]} )); then
                echo "Error: invalid selection" >&2; exit 1
            fi
            spec_file="${spec_files[$((spec_idx-1))]}"
            local spec_basename
            spec_basename="$(basename "$spec_file")"
            if [[ "$spec_basename" != *-design.md ]]; then
                echo "Error: spec file must end in '-design.md' (got: ${spec_basename})" >&2
                echo "Spec files must follow the naming convention: YYYY-MM-DD-<name>-design.md" >&2
                exit 1
            fi
            spec_rel="docs/superpowers/specs/${spec_basename}"
        else
            echo "Warning: no spec files found in ${spec_dir} — skipping spec"
        fi
    else
        echo "Warning: ${spec_dir} not found — skipping spec selection"
    fi

    # --- Python? ---
    echo ""
    local uses_python="n"
    read -rp "Does this project use Python? (y/n): " uses_python

    # --- Account selection ---
    local claude_config_dir=""
    if [[ -f "$ACCOUNTS_CONF" ]]; then
        mapfile -t account_lines < <(grep -v '^[[:space:]]*#' "$ACCOUNTS_CONF" | grep -v '^[[:space:]]*$')
        if [[ ${#account_lines[@]} -gt 1 ]]; then
            echo ""
            echo "Select Claude account:"
            local i=1
            for line in "${account_lines[@]}"; do
                printf "  %d) %s\n" "$i" "${line%%=*}"
                ((i++))
            done
            read -rp "> " acct_idx
            if ! [[ "$acct_idx" =~ ^[0-9]+$ ]] || (( acct_idx < 1 || acct_idx > ${#account_lines[@]} )); then
                echo "Error: invalid selection" >&2; exit 1
            fi
            local selected_line="${account_lines[$((acct_idx-1))]}"
            claude_config_dir="${selected_line#*=}"
        elif [[ ${#account_lines[@]} -eq 1 ]]; then
            claude_config_dir="${account_lines[0]#*=}"
        fi
    fi

    # --- Reset planning state ---
    rm -f "${TASK_DIR}/${name}.total"
    rm -f "${TASK_DIR}/${name}.step"
    rm -f "${TASK_DIR}/${name}.start-sha"
    rm -f "${TASK_DIR}/${name}.branch"
    rm -f "${TASK_DIR}/${name}".plan-*.md
    rm -f "${TASK_DIR}/${name}".step-*.done

    # --- Write task file (rules + spec) ---
    cat > "$task_file" <<HEADER
# Task: ${name}

## Rules — read these first, they apply to every step

- Work autonomously. Do not ask for permission or human approval at any point.
- Use your own discretion on all decisions.
- If you reach a point where you cannot continue for any reason, write a plain-text
  description of why to:
  ${TASK_DIR}/${name}.stuck
  Then stop immediately — do not loop or retry.
- When the current step is complete, write a one-line summary to:
  ${TASK_DIR}/${name}.step-N.done
  where N is the step number shown in the "Current step" header below.
  The runner will advance to the next step automatically.
  Do not attempt steps beyond the one shown — stop after writing the done file.
HEADER

    if [[ "$uses_python" == "y" || "$uses_python" == "Y" ]]; then
        cat >> "$task_file" <<'PYTHON'
- Always use `hatch run` for all Python commands:
  `hatch run pytest`, `hatch run python`, etc.
  Never use bare `python`, `python3`, or `pip` directly.
PYTHON
    fi

    if [[ -n "$spec_file" && -f "$spec_file" ]]; then
        {
            printf '\n\n## Spec\n\nSource: %s\n\n---\n\n' "$spec_rel"
            cat "$spec_file"
            printf '\n\n---\n'
        } >> "$task_file"
    fi

    chmod 644 "$task_file"

    # --- Write spec-ref ---
    if [[ -n "$spec_file" ]]; then
        echo "$(basename "$spec_file")" > "${TASK_DIR}/${name}.spec-ref"
        chmod 644 "${TASK_DIR}/${name}.spec-ref"
    fi

    # --- Update env file with selected account ---
    if [[ -n "$claude_config_dir" ]]; then
        sed -i '/^CLAUDE_CONFIG_DIR=/d' "$env_file"
        echo "CLAUDE_CONFIG_DIR=${claude_config_dir}" >> "$env_file"
    fi

    echo ""
    echo "Instruction set for claude-runner@${name}"
    echo "Restart the runner to apply: systemctl restart claude-runner@${name}.service"
}
```

- [ ] **Step 2: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/claude-runner.j2
```

Expected: no errors (Jinja2 `{{ }}` warnings inside `{% raw %}` blocks are acceptable).

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat: rewrite add-instruction — spec-only, with spec-ref write and state reset"
```

---

## Task 6: Update claude-runner.j2 — cmd_remove and cmd_list

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

- [ ] **Step 1: Add spec-ref and branch file cleanup to cmd_remove**

In the `cmd_remove` function, after the existing `rm -f "${TASK_DIR}/${name}.start-sha"` line, add:

```bash
    rm -f "${TASK_DIR}/${name}.spec-ref"
    rm -f "${TASK_DIR}/${name}.branch"
```

- [ ] **Step 2: Update INSTRUCTION column logic in cmd_list**

In `cmd_list`, find:

```bash
        local instruction
        if [[ -f "${TASK_DIR}/${name}.md" ]]; then
            instruction="set"
        else
            instruction="none"
        fi
```

Replace with:

```bash
        local instruction
        if [[ -f "${TASK_DIR}/${name}.spec-ref" || -f "${TASK_DIR}/${name}.md" ]]; then
            instruction="set"
        else
            instruction="none"
        fi
```

- [ ] **Step 3: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/claude-runner.j2
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat: update cmd_remove and cmd_list for spec-ref and branch state files"
```

---

## Task 7: Final verification

- [ ] **Step 1: Full ansible syntax check**

```bash
ansible-playbook --syntax-check ansible/deploy-claude-runner.yml
```

Expected: no errors.

- [ ] **Step 2: Full bash syntax check on both templates**

```bash
bash -n ansible/roles/claude-runner/templates/run.sh.j2
bash -n ansible/roles/claude-runner/templates/claude-runner.j2
```

Expected: no errors from either file.

- [ ] **Step 3: Confirm all new state files are covered in cmd_remove**

Read `ansible/roles/claude-runner/templates/claude-runner.j2` and verify `cmd_remove` deletes all of: `.md`, `.stuck`, `.done`, `.step`, `.total`, `.start-sha`, `.plan-*.md`, `.step-*.done`, `.spec-ref`, `.branch`.

- [ ] **Step 4: Confirm loop ordering in run.sh.j2**

Read `ansible/roles/claude-runner/templates/run.sh.j2` and confirm the while-loop body order:
1. `.done` check → `exit 0`
2. `.stuck` check → `sleep 60 / continue`
3. No task file → `sleep 30 / continue`
4. No `.total` → planning pass → `continue`
5. `STEP / TOTAL / PLAN_FILE` reads → existing step sequencer

- [ ] **Step 5: Note operator prerequisites**

The following manual steps are required after deploying the ansible role — they are not automated. Verify the operator has been informed:

1. Authenticate gh CLI as the `claude` user:
   ```bash
   sudo -u claude gh auth login --hostname <gitea-hostname> --git-protocol https
   ```
2. Add `GH_HOST=<gitea-hostname>` to each runner's env file:
   ```bash
   echo "GH_HOST=<gitea-hostname>" >> /opt/claude-runner/env/<name>
   ```
   Without this, `gh pr create` will target github.com instead of Gitea.

- [ ] **Step 6: Commit plan and spec**

```bash
git add docs/superpowers/plans/2026-03-26-claude-runner-spec-driven.md
git commit -m "docs: add implementation plan for claude-runner spec-driven workflow"
```
