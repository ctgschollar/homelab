# Fix 3 — Concurrent Shell Gate

**Date:** 2026-03-24
**Scope:** `agent/agent/tools.py`, `agent/tests/`
**PR:** standalone (one PR per fix)

---

## Problem

Within a single agent turn, tier-1 tool calls are gathered concurrently via
`asyncio.gather` in `_handle_tool_calls`. The `run_shell` tool can resolve to
tier-1 when the agent proposes tier-1 and no pattern guard fires. This means
multiple `run_shell` calls in a single response can execute simultaneously —
including multiple concurrent SSH sessions to the same or different nodes.

Concurrent shell executions are risky even for "read-only" commands:

- Two SSH sessions to the same node can observe inconsistent state midway
  through each other's execution.
- Side-effect-free commands can still exhaust SSH connection limits.
- If a pattern guard fires on one command and elevates it to tier-2, the
  remaining tier-1 calls still race against the approval wait.
- The shell command tier guards prevent individual dangerous commands from
  being tier-1, but do not prevent safe commands from piling up.

The prerequisite work (shell command pattern guards) is already in main. This
fix adds the second layer: a **gate** that serializes shell executions.

---

## Design

### 1. `_shell_gate` semaphore in `ToolExecutor`

A `asyncio.Semaphore(1)` created in `ToolExecutor.__init__`:

```python
self._shell_gate = asyncio.Semaphore(1)
```

### 2. Wrap `_tool_run_shell`

```python
async def _tool_run_shell(self, inp: dict) -> str:
    command = inp["command"]
    node = inp.get("node")

    if node:
        args = [
            "ssh",
            "-i", self._ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{self._ssh_user}@{node}",
            command,
        ]
    else:
        args = ["bash", "-c", command]

    async with self._shell_gate:
        return await self._run_subprocess(args, timeout=300, stream=True)
```

The gate wraps the entire subprocess execution (including the stream timeout),
not just the subprocess launch. This ensures the semaphore is held for the
full duration of the command, preventing a second command from starting before
the first finishes.

### 3. No config changes

The gate is hardcoded to `Semaphore(1)` (one shell command at a time). No
config option is added:

- The primary risk is concurrent mutations, and even "read-only" commands
  can have ordering dependencies.
- Configurable concurrency can be added later if a need emerges; starting
  conservative is safer.

### 4. `_tool_run_ansible_playbook` is NOT gated

Ansible is tier-2 by default and never enters the concurrent tier-1 gather,
so it cannot race with `run_shell` under normal conditions. The gate is
intentionally limited to `run_shell` to keep the change minimal.

---

## Tests — `agent/tests/test_fix3_concurrent_shell_gate.py`

All tests use `unittest.mock` and `asyncio` directly — no live subprocesses.

### `test_shell_gate_serializes_concurrent_calls`

Start two concurrent `_tool_run_shell` coroutines (both patched to record
their start/finish times). Assert the second call does not start until the
first finishes (i.e., start₂ ≥ finish₁).

### `test_shell_gate_releases_after_normal_completion`

Run one shell call to completion. Assert the semaphore value is back to 1
afterward (i.e., the gate was released).

### `test_shell_gate_releases_after_exception`

Patch `_run_subprocess` to raise an exception. Assert the semaphore value
is back to 1 after the exception propagates. This verifies that the
`async with` statement releases the lock even on error paths.

### `test_shell_gate_local_and_ssh_both_gated`

Run one local command and one SSH command concurrently. Assert they are
serialized (same ordering check as the first test).

---

## Files changed

| File | Change |
|------|--------|
| `agent/agent/tools.py` | Add `self._shell_gate = asyncio.Semaphore(1)` in `__init__`; wrap subprocess call in `_tool_run_shell` with `async with self._shell_gate` |
| `agent/tests/test_fix3_concurrent_shell_gate.py` | New — four serialization tests |

---

## Out of scope

- No config changes
- No changes to `run_ansible_playbook`
- No changes to tier resolution logic (shell pattern guards are already in main)
- No changes to `_handle_tool_calls` in `agent.py`
