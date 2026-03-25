# Claude Runner Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the claude-runner ansible role with shared tool installation, interactive task setup with superpower spec/plan selection, multi-account Claude config, git-commit progress tracking, and stuck token support.

**Architecture:** A new `install-basic-tools.yml` playbook handles server prerequisites; the runner role focuses solely on the runner service. The `run.sh.j2` loop gains stuck-token halting and git-progress resume injection. The `claude-runner.j2` CLI gains interactive `add-instruction`, account management, and stuck token management commands.

**Tech Stack:** Ansible, Bash, Jinja2 templates, systemd

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `ansible/install-basic-tools.yml` | Create | Install curl, wget, git, vim, python3-pip, pipx, hatch on target servers |
| `ansible/roles/claude-runner/tasks/main.yml` | Modify | Add `accounts.conf` creation task |
| `ansible/roles/claude-runner/templates/run.sh.j2` | Modify | Add stuck-token check, git-progress resume, remove trailing sleep |
| `ansible/roles/claude-runner/templates/claude-runner.j2` | Modify | Add `ACCOUNTS_CONF` var; add `add-account`, `remove-account`, `list-accounts`, `list-stuck`, `clear-stuck` commands; rewrite `add-instruction` as interactive |

---

## Task 1: Create `ansible/install-basic-tools.yml`

**Files:**
- Create: `ansible/install-basic-tools.yml`

- [ ] **Step 1: Write the playbook**

```yaml
---
- name: Install basic tools on target servers
  hosts: all
  become: true

  vars:
    target_user: claude
    target_user_home: /home/claude

  tasks:
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

    - name: Install hatch via pipx for target user
      become_user: "{{ target_user }}"
      command: pipx install hatch
      environment:
        HOME: "{{ target_user_home }}"
      args:
        creates: "{{ target_user_home }}/.local/bin/hatch"
```

- [ ] **Step 2: Verify ansible syntax**

```bash
ansible-playbook --syntax-check ansible/install-basic-tools.yml
```

Expected: `playbook: ansible/install-basic-tools.yml` with no errors.

- [ ] **Step 3: Commit**

```bash
git add ansible/install-basic-tools.yml
git commit -m "feat: add install-basic-tools playbook (curl, wget, git, vim, pip, pipx, hatch)"
```

---

## Task 2: Update `tasks/main.yml` — remove `pipx` from apt list and add `accounts.conf` creation

**Files:**
- Modify: `ansible/roles/claude-runner/tasks/main.yml`

- [ ] **Step 1: Remove `pipx` from the existing `Install system dependencies` apt task**

The runner role's apt task currently lists `nodejs`, `npm`, `expect`. If `pipx` is present there, remove it — it is now handled by `install-basic-tools.yml`. The apt task should only contain packages specific to running claude-code:

```yaml
- name: Install system dependencies
  apt:
    name:
      - nodejs
      - npm
      - expect
    state: present
    update_cache: yes
```

- [ ] **Step 2: Add `accounts.conf` creation task after the `tasks` directory creation task**

Add this block after the existing `Create tasks directory` task:

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

- [ ] **Step 4: Verify syntax**

```bash
ansible-playbook --syntax-check ansible/deploy-claude-runner.yml
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/claude-runner/tasks/main.yml
git commit -m "feat: remove pipx from runner apt list; add accounts.conf creation task"
```

---

## Task 3: Update `run.sh.j2` — stuck check, git progress resume, remove sleep

**Files:**
- Modify: `ansible/roles/claude-runner/templates/run.sh.j2`

- [ ] **Step 1: Replace the entire file with the new version**

```bash
#!/bin/bash
# Managed by Ansible — do not edit directly.
# Source: ansible/roles/claude-runner/templates/run.sh.j2

REPO_PATH="${1:?Usage: run.sh <repo-path> <name>}"
NAME="${2:?Usage: run.sh <repo-path> <name>}"
TASK_FILE="{{ claude_runner_base_dir }}/tasks/${NAME}.md"
STUCK_FILE="{{ claude_runner_base_dir }}/tasks/${NAME}.stuck"
LOG_FILE="{{ claude_runner_base_dir }}/logs/${NAME}.log"

cd "$REPO_PATH"

while true; do
    # Halt if a stuck token exists — requires human intervention to clear
    if [[ -f "$STUCK_FILE" ]]; then
        echo "$(date -Iseconds) STUCK: $(cat "$STUCK_FILE"). Clear with: claude-runner clear-stuck ${NAME}" | tee -a "$LOG_FILE"
        sleep 60
        continue
    fi

    if [[ ! -f "$TASK_FILE" ]]; then
        echo "$(date -Iseconds) No task file found. Use: claude-runner add-instruction ${NAME}" | tee -a "$LOG_FILE"
        sleep 30
        continue
    fi

    # Resume context: inject last [PROGRESS] commit message as a prefix
    LAST_PROGRESS=$(git -C "$REPO_PATH" log --oneline 2>/dev/null | grep '\[PROGRESS' | head -1)
    if [[ -n "$LAST_PROGRESS" ]]; then
        RESUME_PREFIX="RESUME CONTEXT: Last completed progress: ${LAST_PROGRESS}\nContinue from the next incomplete step in the plan.\n\n"
    else
        RESUME_PREFIX=""
    fi

    { printf "%b" "$RESUME_PREFIX"; cat "$TASK_FILE"; } | claude --print --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE"
done
```

- [ ] **Step 2: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/run.sh.j2
```

Expected: no output (no errors). Note: Jinja2 `{{ }}` tokens cause minor warnings — these are expected and harmless; they resolve at Ansible render time.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/run.sh.j2
git commit -m "feat: add stuck token check and git progress resume to run.sh"
```

---

## Task 4: Add account management commands to `claude-runner.j2`

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

- [ ] **Step 1: Add `ACCOUNTS_CONF` variable at the top of the script, after the existing variable declarations**

After the line `TASK_DIR="{{ claude_runner_base_dir }}/tasks"`, add:

```bash
ACCOUNTS_CONF="{{ claude_runner_base_dir }}/accounts.conf"
```

- [ ] **Step 2: Add the three account functions before `cmd_list`**

```bash
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

    sed -i "/^${name}=/d" "$ACCOUNTS_CONF"

    # Clear CLAUDE_CONFIG_DIR from any env files that used this account
    while IFS='=' read -r inst_name _inst_path; do
        [[ -z "$inst_name" || "$inst_name" == \#* ]] && continue
        local env_file="${ENV_DIR}/${inst_name}"
        if grep -q "^CLAUDE_CONFIG_DIR=${config_dir}$" "$env_file" 2>/dev/null; then
            echo "Warning: claude-runner@${inst_name} was using this account — CLAUDE_CONFIG_DIR cleared"
            sed -i '/^CLAUDE_CONFIG_DIR=/d' "$env_file"
        fi
    done < "$REPOS_CONF"

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
```

- [ ] **Step 3: Wire up the new commands in the `case` statement at the bottom**

In the existing `case "${1:-}" in` block, add before `*)`:

```bash
    add-account)     shift; cmd_add_account "$@" ;;
    remove-account)  shift; cmd_remove_account "$@" ;;
    list-accounts)   cmd_list_accounts ;;
```

- [ ] **Step 4: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/claude-runner.j2
```

Expected: no errors (Jinja2 `{{ }}` warnings are acceptable).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat: add account management commands to claude-runner CLI"
```

---

## Task 5: Add stuck token commands to `claude-runner.j2`

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

- [ ] **Step 1: Add `cmd_list_stuck` and `cmd_clear_stuck` before `cmd_list`**

```bash
cmd_list_stuck() {
    local found=0

    printf "%-20s %s\n" "NAME" "REASON"
    printf "%-20s %s\n" "----" "------"

    for stuck_file in "${TASK_DIR}"/*.stuck; do
        [[ -f "$stuck_file" ]] || continue
        local name
        name=$(basename "$stuck_file" .stuck)
        local reason
        reason=$(cat "$stuck_file")
        printf "%-20s %s\n" "$name" "$reason"
        found=1
    done

    if [[ $found -eq 0 ]]; then
        echo "No stuck runners."
    fi
}

cmd_clear_stuck() {
    local name="${1:?clear-stuck requires a name}"
    local stuck_file="${TASK_DIR}/${name}.stuck"

    if [[ ! -f "$stuck_file" ]]; then
        echo "Error: no stuck token found for '${name}'" >&2
        exit 1
    fi

    rm "$stuck_file"
    echo "Cleared stuck token for '${name}'"
    echo "Restart the runner: systemctl restart claude-runner@${name}.service"
}
```

- [ ] **Step 2: Wire up in `case` statement**

Add before `*)`:

```bash
    list-stuck)      cmd_list_stuck ;;
    clear-stuck)     shift; cmd_clear_stuck "$@" ;;
```

- [ ] **Step 3: Verify bash syntax**

```bash
bash -n ansible/roles/claude-runner/templates/claude-runner.j2
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat: add list-stuck and clear-stuck commands to claude-runner CLI"
```

---

## Task 6: Rewrite `cmd_add_instruction` with interactive flow

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

- [ ] **Step 1: Replace the existing `cmd_add_instruction` function with the new interactive version**

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
            spec_file="${spec_files[$((spec_idx-1))]}"
            spec_rel="docs/superpowers/specs/$(basename "$spec_file")"
        else
            echo "Warning: no spec files found in ${spec_dir} — skipping spec"
        fi
    else
        echo "Warning: ${spec_dir} not found — skipping spec selection"
    fi

    # --- Plan selection ---
    local selected_plans=()
    local plan_dir="${repo_path}/docs/superpowers/plans"
    if [[ -d "$plan_dir" ]]; then
        mapfile -t plan_files < <(find "$plan_dir" -maxdepth 1 -name "*.md" | sort)
        if [[ ${#plan_files[@]} -gt 0 ]]; then
            echo ""
            echo "Select plans (comma-separated numbers, in execution order):"
            local i=1
            for f in "${plan_files[@]}"; do
                printf "  %d) %s\n" "$i" "$(basename "$f")"
                ((i++))
            done
            read -rp "> " plan_selection
            IFS=',' read -ra plan_indices <<< "$plan_selection"
            for idx in "${plan_indices[@]}"; do
                idx="${idx// /}"
                selected_plans+=("${plan_files[$((idx-1))]}")
            done
        else
            echo "Warning: no plan files found in ${plan_dir} — skipping plans"
        fi
    else
        echo "Warning: ${plan_dir} not found — skipping plan selection"
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
            local selected_line="${account_lines[$((acct_idx-1))]}"
            claude_config_dir="${selected_line#*=}"
        elif [[ ${#account_lines[@]} -eq 1 ]]; then
            claude_config_dir="${account_lines[0]#*=}"
        fi
    fi

    # --- Write task file ---
    local total_steps=${#selected_plans[@]}
    [[ $total_steps -eq 0 ]] && total_steps=1

    # Header + rules (unquoted heredoc: Jinja2 {{ }} resolved at deploy time,
    # bash $vars resolved at runtime; backticks escaped with \` to avoid
    # command substitution)
    cat > "$task_file" <<HEADER
# Task: ${name}

## Rules — read these first, they apply to every step

- Work autonomously. Do not ask for permission or human approval at any point.
- Use your own discretion on all decisions.
- After completing each plan step, commit all changes with a message starting with
  \`[PROGRESS N/T]\` where N is the completed step number and T is the total number
  of steps, followed by a brief description of what was completed.
  Example: \`[PROGRESS 2/${total_steps}] Completed: feature name\`
- If you reach a point where you cannot continue for any reason, write a plain-text
  description of why to:
  {{ claude_runner_base_dir }}/tasks/${name}.stuck
  Then stop immediately — do not loop or retry.
HEADER

    # Python rule (single-quoted heredoc: backticks safe, no variable expansion needed)
    if [[ "$uses_python" == "y" || "$uses_python" == "Y" ]]; then
        cat >> "$task_file" <<'PYTHON'
- Always use `hatch run` for all Python commands:
  `hatch run pytest`, `hatch run python`, etc.
  Never use bare `python`, `python3`, or `pip` directly.
PYTHON
    fi

    # Spec section
    if [[ -n "$spec_file" && -f "$spec_file" ]]; then
        {
            printf '\n## Spec\n\nSource: %s\n\n---\n\n' "$spec_rel"
            cat "$spec_file"
            printf '\n\n---\n'
        } >> "$task_file"
    fi

    # Plans section
    if [[ ${#selected_plans[@]} -gt 0 ]]; then
        printf '\n## Plans — execute in this order\n' >> "$task_file"
        local step=1
        for plan_file in "${selected_plans[@]}"; do
            {
                printf '\n### Step %d of %d: %s\n\n---\n\n' \
                    "$step" "${#selected_plans[@]}" "$(basename "$plan_file")"
                cat "$plan_file"
                printf '\n\n---\n'
            } >> "$task_file"
            ((step++))
        done
    fi

    chmod 644 "$task_file"

    # Update env file with selected account
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

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat: rewrite add-instruction as interactive flow with spec/plan/account selection"
```

---

## Task 7: Update usage block and finalise `case` statement

**Files:**
- Modify: `ansible/roles/claude-runner/templates/claude-runner.j2`

- [ ] **Step 1: Replace the `usage()` function with the updated version**

```bash
usage() {
    cat <<EOF
Usage: claude-runner <command> [args]

Commands:
  add <name> <path>                    Register a repo and start its runner service
  remove <name>                        Stop and remove a runner
  add-instruction <name>               Interactively set the task for a runner
  list                                 Show all runners and their service status
  status <name>                        Tail the journal for a runner
  list-stuck                           Show all runners with a stuck token
  clear-stuck <name>                   Remove the stuck token for a runner
  add-account <name> <config-dir>      Register a Claude account config dir
  remove-account <name>                Deregister a Claude account
  list-accounts                        Show all registered Claude accounts
EOF
    exit 1
}
```

- [ ] **Step 2: Verify the complete `case` statement has all commands wired up**

The final `case` block should look like:

```bash
case "${1:-}" in
    add)             shift; cmd_add "$@" ;;
    remove)          shift; cmd_remove "$@" ;;
    add-instruction) shift; cmd_add_instruction "$@" ;;
    list)            cmd_list ;;
    status)          shift; cmd_status "$@" ;;
    list-stuck)      cmd_list_stuck ;;
    clear-stuck)     shift; cmd_clear_stuck "$@" ;;
    add-account)     shift; cmd_add_account "$@" ;;
    remove-account)  shift; cmd_remove_account "$@" ;;
    list-accounts)   cmd_list_accounts ;;
    *)               usage ;;
esac
```

- [ ] **Step 3: Final syntax check of the whole file**

```bash
bash -n ansible/roles/claude-runner/templates/claude-runner.j2
```

Expected: no errors.

- [ ] **Step 4: Run a full ansible syntax check of the deploy playbook**

```bash
ansible-playbook --syntax-check ansible/deploy-claude-runner.yml
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/claude-runner/templates/claude-runner.j2
git commit -m "feat: update usage block and wire all new commands in claude-runner CLI"
```

---

## Final verification

- [ ] Confirm all new files exist:
  ```bash
  ls ansible/install-basic-tools.yml
  ls ansible/roles/claude-runner/templates/
  ```

- [ ] Confirm the plan doc and spec doc are committed:
  ```bash
  git log --oneline -8
  ```

- [ ] Run full syntax check one more time:
  ```bash
  ansible-playbook --syntax-check ansible/deploy-claude-runner.yml
  ansible-playbook --syntax-check ansible/install-basic-tools.yml
  ```
