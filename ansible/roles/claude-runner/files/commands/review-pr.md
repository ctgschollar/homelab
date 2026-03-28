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
