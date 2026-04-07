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
3. **Do not push or create a PR** — the runner handles this via its `create_pr` hook after the skill completes. If `finishing-a-development-branch` or any other skill attempts to create a PR, skip that step.
4. After all tasks are complete, write result.json:
   ```json
   {"status": "done", "outcome": "all_steps_complete", "message": "All plan tasks implemented"}
   ```

If you get stuck at any point, write:
```json
{"status": "stuck", "outcome": "stuck", "message": "<reason>"}
```