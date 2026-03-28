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
