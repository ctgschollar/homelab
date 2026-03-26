# Design: Claude Runner — Spec-Driven Workflow + PR Creation

**Date:** 2026-03-26
**Scope:** `ansible/roles/claude-runner/`
**Status:** Approved

---

## Overview

Two improvements to the claude-runner:

1. **Spec-driven workflow** — `add-instruction` now accepts a spec only (no pre-written plans). The runner generates the plan autonomously during a planning pass, then implements it via the existing step sequencer.
2. **PR creation** — the runner works on a dedicated branch and creates a Gitea PR when all steps are complete, rather than committing directly to `main`.

The `docs/superpowers/plans/` directory is no longer an input to the runner; it becomes an output (the runner writes the generated plan there, which appears in the PR diff for human review).

---

## Flow

```
add-instruction <name>
  → select spec (docs/superpowers/specs/*.md)
  → ask Python y/n
  → ask account (if >1 registered)
  → write tasks/<name>.md (rules + embedded spec)
  → write tasks/<name>.spec-ref (spec basename, e.g. 2026-03-26-fix1-design.md)
  → reset planning state (delete .total, .step, .plan-*.md, .step-*.done, .start-sha, .branch)
  → print restart reminder

run.sh loop:
  [.done exists]  → log + exit 0 (unchanged)
  [.stuck exists] → log + sleep 60 (unchanged)
  [no task file]  → log + sleep 30 (unchanged)
  [no .total]     → PLANNING PASS
                    → git checkout -b claude-runner/<name>
                    → write branch name to tasks/<name>.branch
                    → run Claude: spec + instructions to invoke writing-plans skill
                    → after Claude exits: find expected plan file, copy to tasks/<name>.plan-1.md, write 1 to tasks/<name>.total
  [.total exists] → EXISTING STEP SEQUENCER (unchanged)
  [all steps done]→ git push -u origin <branch>
                    → gh pr create --title "feat: <name>" --base main --body "..."
                    → write tasks/<name>.done, exit 0
```

---

## Component Changes

### `tasks/main.yml`

Add GitHub CLI installation after the existing apt task:

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

Authentication is performed manually by the operator (`gh auth login`) after deployment.

---

### `claude-runner.j2` — `cmd_add_instruction`

**Remove:** plan selection menu, writing of `.plan-N.md` files, `.total` and `.step` initialisation.

**Add:**
- Write spec basename to `tasks/<name>.spec-ref`
- Reset planning state on re-instruction:
  ```bash
  rm -f "${TASK_DIR}/${name}.total"
  rm -f "${TASK_DIR}/${name}.step"
  rm -f "${TASK_DIR}/${name}.start-sha"
  rm -f "${TASK_DIR}/${name}.branch"
  rm -f "${TASK_DIR}/${name}".plan-*.md
  rm -f "${TASK_DIR}/${name}".step-*.done
  ```

**Task file structure** (written by `add-instruction`):

```markdown
# Task: <name>

## Rules
- Work autonomously. Do not ask for permission or human approval at any point.
- Use your own discretion on all decisions.
- If you reach a point where you cannot continue, write a plain-text description to:
  /opt/claude-runner/tasks/<name>.stuck
  Then stop immediately — do not loop or retry.
- When the current step is complete, write a one-line summary to:
  /opt/claude-runner/tasks/<name>.step-N.done
  where N is the step number shown in the "Current step" header.
  Do not attempt steps beyond the one shown — stop after writing the done file.
[IF PYTHON]
- Always use `hatch run` for all Python commands.
[END IF PYTHON]

## Spec

Source: docs/superpowers/specs/<spec-basename>

---

<full spec contents embedded here>

---
```

---

### `run.sh.j2` — Planning pass

**New state variables:**

```bash
SPEC_REF_FILE="${TASK_DIR}/${NAME}.spec-ref"
BRANCH_FILE="${TASK_DIR}/${NAME}.branch"
```

**Plan filename derivation:**

```bash
SPEC_REF=$(cat "$SPEC_REF_FILE")
PLAN_NAME="${SPEC_REF%-design.md}.md"
EXPECTED_PLAN="${REPO_PATH}/docs/superpowers/plans/${PLAN_NAME}"
```

**Planning pass (when `.total` does not exist):**

```bash
# Create and record the working branch
BRANCH="claude-runner/${NAME}"
git checkout -B "$BRANCH"
echo "$BRANCH" > "$BRANCH_FILE"

# Record start SHA for progress log
git rev-parse HEAD > "$START_SHA_FILE"

# Build planning prompt
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

# Check for generated plan
if [[ -f "$EXPECTED_PLAN" ]]; then
    cp "$EXPECTED_PLAN" "${TASK_DIR}/${NAME}.plan-1.md"
    echo "1" > "${TASK_DIR}/${NAME}.total"
    echo "1" > "${TASK_DIR}/${NAME}.step"
    echo "$(date -Iseconds) Planning complete. Plan: ${PLAN_NAME}" | tee -a "$LOG_FILE"
else
    echo "$(date -Iseconds) Planning pass did not produce expected plan file: ${PLAN_NAME}. Retrying." | tee -a "$LOG_FILE"
    sleep 30
fi
```

**PR creation (when all steps done, before writing `.done`):**

```bash
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

---

### `claude-runner.j2` — `cmd_remove`

Add cleanup of new state files:

```bash
rm -f "${TASK_DIR}/${name}.spec-ref"
rm -f "${TASK_DIR}/${name}.branch"
```

---

### `claude-runner.j2` — `cmd_list`

The `STEP` column remains unchanged. The `INSTRUCTION` column now shows `set` if `tasks/<name>.spec-ref` exists (instead of `tasks/<name>.md`), since the spec-ref file is a better indicator that `add-instruction` has run.

---

## Files Changed

| File | Change |
|------|--------|
| `ansible/roles/claude-runner/tasks/main.yml` | Add `gh` CLI apt repo + install |
| `ansible/roles/claude-runner/templates/run.sh.j2` | Add planning pass, branch creation, PR creation |
| `ansible/roles/claude-runner/templates/claude-runner.j2` | Remove plan menu from `add-instruction`; add spec-ref write + state reset; update `cmd_remove`; update `cmd_list` |

---

## Out of Scope

- No changes to the systemd unit template
- No changes to `defaults/main.yml`
- No changes to `handlers/main.yml`
- No changes to `agent/` code
- `gh` authentication is manual (operator runs `gh auth login` post-deploy)
