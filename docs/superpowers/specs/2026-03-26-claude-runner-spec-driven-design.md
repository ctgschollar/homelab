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
                    → git checkout -B claude-runner/<name> (force-create; safe because branch state was wiped by add-instruction reset)
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

Authentication is performed manually by the operator after deployment. Since the repo is on a self-hosted Gitea instance, `gh` must be pointed at that host. **Authentication must be performed as `claude_user`** (the user the systemd service runs as), not as root:
```
sudo -u claude gh auth login --hostname <gitea-hostname> --git-protocol https
```
Gitea 1.19+ exposes a GitHub-compatible REST API that `gh` can use. The Gitea instance must have API compatibility enabled (it is on by default).

The operator must also add `GH_HOST=<gitea-hostname>` to each runner's env file (`/opt/claude-runner/env/<name>`) so that `gh pr create` targets the correct host. The systemd unit already loads this file via `EnvironmentFile=`, so no service unit changes are needed.

**Git push authentication:** `git push` uses a separate credential mechanism from `gh`. The `claude_user` must have push access configured for the repository — either via SSH key registered in Gitea, or via HTTPS credentials stored in git's credential helper. This is a pre-existing operational requirement; the runner cannot push or create a PR without it.

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

**Spec embedding:** the spec file is read from `${repo_path}/docs/superpowers/specs/${spec_basename}` (absolute path, constructed from the registered `REPO_PATH` and the spec basename stored in `.spec-ref`). Its full contents are appended verbatim to the task file at `add-instruction` time.

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

<full contents of ${repo_path}/docs/superpowers/specs/${spec_basename} embedded here>

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

**Loop ordering:** The planning-pass block must be inserted **before** the existing `PLAN_FILE` existence check. The current loop checks `[[ ! -f "$PLAN_FILE" ]]` and sleeps if absent — if the planning pass check came after this, it would never be reached. The correct order in the loop body is:

1. `.done` check
2. `.stuck` check
3. task file check
4. **planning pass** (new — when `.total` absent)
5. existing step sequencer (STEP/TOTAL/PLAN_FILE reads and prompt)

**`.spec-ref` guard:** At the start of the planning pass, if `$SPEC_REF_FILE` does not exist (e.g. old-style task file written before this feature), log an error and sleep rather than letting `cat` fail:

```bash
if [[ ! -f "$SPEC_REF_FILE" ]]; then
    echo "$(date -Iseconds) No spec-ref file for ${NAME}. Re-run: claude-runner add-instruction ${NAME}" | tee -a "$LOG_FILE"
    sleep 30
    continue
fi
```

**Planning pass (when `.total` does not exist):**

The branch creation and start-SHA recording happen only once — before the first planning invocation. They are guarded by the absence of `$BRANCH_FILE` so that retries (if the planning invocation fails to produce the expected plan) do not re-checkout or overwrite the start SHA.

The existing pre-loop `START_SHA_FILE` write (`if [[ ! -f "$START_SHA_FILE" ]]; then ...`) **must be removed** from `run.sh.j2`. The planning pass is now the sole writer of `$START_SHA_FILE`, and it must write it after the branch checkout so the SHA is on the correct branch.

```bash
# One-time setup: create branch and record start SHA
if [[ ! -f "$BRANCH_FILE" ]]; then
    BRANCH="claude-runner/${NAME}"
    git checkout -B "$BRANCH"
    echo "$BRANCH" > "$BRANCH_FILE"
    git rev-parse HEAD > "$START_SHA_FILE"
fi

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

**Spec filename convention:** spec files MUST follow the `YYYY-MM-DD-<name>-design.md` naming convention. The `superpowers:writing-plans` skill saves its output to `docs/superpowers/plans/YYYY-MM-DD-<name>.md` — i.e. the same date and name prefix, without the `-design` suffix. The plan filename derivation (`${SPEC_REF%-design.md}.md`) depends exactly on this convention. `add-instruction` must validate that the selected spec filename ends in `-design.md` and print an error and exit if it does not.

**Single-file plan:** the writing-plans skill produces one plan file per spec. `.total` is therefore hardcoded to `1`. The entire implementation is one step.

**PR creation — replaces the existing "all steps done" exit block:**

The current code (to be replaced):
```bash
echo "$(date -Iseconds) All ${TOTAL} steps complete." | tee -a "$LOG_FILE"
echo "All ${TOTAL} steps completed." > "$DONE_FILE"
exit 0
```

Replacement:
```bash
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

---

### `claude-runner.j2` — `cmd_remove`

Add cleanup of new state files:

```bash
rm -f "${TASK_DIR}/${name}.spec-ref"
rm -f "${TASK_DIR}/${name}.branch"
```

---

### `claude-runner.j2` — `cmd_list`

The `STEP` column remains unchanged. The `INSTRUCTION` column condition changes from:
```bash
if [[ -f "${TASK_DIR}/${name}.md" ]]; then
```
to:
```bash
if [[ -f "${TASK_DIR}/${name}.spec-ref" || -f "${TASK_DIR}/${name}.md" ]]; then
```
This preserves backward compatibility with old-style task files that have `.md` but no `.spec-ref`.

---

## Files Changed

| File | Change |
|------|--------|
| `ansible/roles/claude-runner/tasks/main.yml` | Add `gh` CLI apt repo + install |
| `ansible/roles/claude-runner/templates/run.sh.j2` | Add planning pass, branch creation, PR creation; remove pre-loop `START_SHA_FILE` write |
| `ansible/roles/claude-runner/templates/claude-runner.j2` | Remove plan menu from `add-instruction`; add spec-ref write + state reset; update `cmd_remove`; update `cmd_list` |

---

## Out of Scope

- No changes to the systemd unit template
- No changes to `defaults/main.yml`
- No changes to `handlers/main.yml`
- No changes to `agent/` code
- `gh` authentication is manual: operator runs `gh auth login --hostname <gitea-hostname>` and adds `GH_HOST=<gitea-hostname>` to each runner's env file post-deploy
