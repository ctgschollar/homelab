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
