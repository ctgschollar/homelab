# Fix 3 — Concurrent Tier-1 Shell Execution Gate

**Date:** 2026-03-24
**Scope:** `agent/` directory only
**PR:** standalone

---

## Problem

`_handle_tool_calls` in `agent/agent/agent.py` classifies each tool call into
`tier1_blocks` or `mutating_blocks` before any tool has executed. All
`tier1_blocks` are then dispatched concurrently via `asyncio.gather`. Because
`run_shell` is tagged `"agent"` in `tool_tiers`, the model self-classifies its
own shell calls. If the agent includes multiple `run_shell` calls in a single
API response and assigns them all tier 1, they execute in parallel — the second
shell command fires before the first has returned a result. The agent cannot
reason about the output of command A before issuing command B; both are
in-flight simultaneously.

This is unsafe even when the individual commands are genuinely read-only.
Arbitrary shell execution fanning out in parallel produces interleaved side
effects and bypasses the sequential reasoning the agent relies on.

---

## Design

### 1. `_handle_tool_calls` split (`agent.py`)

Move the `_exec_tier1` inner function definition outside the `if tier1_blocks:`
guard, to the top of `_handle_tool_calls` before the separation loop. This
makes it available to both the shell-sequential loop and the safe-concurrent
gather without re-definition.

After the existing loop that populates `tier1_blocks` and `mutating_blocks`,
add a second-pass split of `tier1_blocks` into two lists:

```python
tier1_shell_blocks = [b for b in tier1_blocks if b.name == "run_shell"]
tier1_safe_blocks  = [b for b in tier1_blocks if b.name != "run_shell"]
```

The existing `if tier1_blocks:` block (which contained the `_exec_tier1`
definition and the `asyncio.gather` call) is removed entirely. The new code
structure that replaces it is:

```python
# split phase
tier1_shell_blocks = [b for b in tier1_blocks if b.name == "run_shell"]
tier1_safe_blocks  = [b for b in tier1_blocks if b.name != "run_shell"]

# Phase 1: sequential shell
for b in tier1_shell_blocks:
    bid, res = await _exec_tier1(b)
    results[bid] = res

# Phase 2: concurrent safe (asyncio.gather)
if tier1_safe_blocks:
    gathered = await asyncio.gather(*[_exec_tier1(b) for b in tier1_safe_blocks])
    for bid, res in gathered:
        results[bid] = res

# Phase 3: mutating (unchanged)
for block in mutating_blocks:
    ...
```

The inner helper `_exec_tier1` is otherwise unchanged. Execution proceeds in
three phases, in this order:

**Phase 1 — tier-1 shell blocks (sequential)**

Iterate over `tier1_shell_blocks` with a plain `for` loop. Await each call
individually before moving to the next:

```python
for b in tier1_shell_blocks:
    bid, res = await _exec_tier1(b)
    results[bid] = res
```

No semaphore. No gather. One call at a time, results written to `results`
immediately. If `tier1_shell_blocks` is empty, this phase is skipped.

**Phase 2 — tier-1 safe blocks (concurrent, unchanged)**

If `tier1_safe_blocks` is non-empty, use `asyncio.gather` exactly as the
current code does for all tier-1 blocks:

```python
if tier1_safe_blocks:
    gathered = await asyncio.gather(*[_exec_tier1(b) for b in tier1_safe_blocks])
    for bid, res in gathered:
        results[bid] = res
```

**Phase 3 — mutating blocks (sequential, unchanged)**

Existing `for block in mutating_blocks` loop runs after both tier-1 phases.
No change.

**Result ordering**

`results` is a `dict[str, str]` keyed by `block.id`. The final return
statement reconstructs the list by iterating over `tool_use_blocks` (the
original ordered list of all tool-use blocks from the API response). This
ordering is already implemented and requires no change. Results appear in
API-response order regardless of which phase populated each entry.

### 2. `max_concurrent_shell` config (`SafetyConfig` + `config.yaml`)

**`config_schema.py` — `SafetyConfig`**

Add one field to the `SafetyConfig` model:

```python
max_concurrent_shell: int = Field(ge=1, default=1)
```

Placement: after `log_agent_tier_reasoning`, before any future fields.

**`safety.py` — `SafetyPolicy`**

Expose the value as an instance attribute in `SafetyPolicy.__init__`:

```python
self.max_concurrent_shell: int = config.safety.max_concurrent_shell
```

This mirrors the existing pattern for `self.global_safe_mode` and
`self.log_agent_tier_reasoning`.

**`agent.py` — `HomelabAgent`**

`HomelabAgent` already holds a `self._safety: SafetyPolicy` reference.
`_handle_tool_calls` reads `self._safety.max_concurrent_shell` to determine
execution behaviour:

```python
if self._safety.max_concurrent_shell == 1:
    # sequential loop (default)
    for b in tier1_shell_blocks:
        bid, res = await _exec_tier1(b)
        results[bid] = res
else:
    # max_concurrent_shell > 1: still sequential for now
    # Future: replace with asyncio.Semaphore(self._safety.max_concurrent_shell)
    for b in tier1_shell_blocks:
        bid, res = await _exec_tier1(b)
        results[bid] = res
```

At this revision, both branches are identical sequential loops. The config
option is wired and validated but the semaphore-based implementation is
deferred. The field documents the operator's intent and reserves the config
namespace for a future upgrade without a breaking schema change.

**`config.yaml`**

Add `max_concurrent_shell` inside the `safety:` block, immediately after
`log_agent_tier_reasoning`:

```yaml
  # Maximum number of tier-1 run_shell calls that may execute concurrently.
  # Default 1 (sequential). Values > 1 are accepted but currently still run
  # sequentially — semaphore-based parallelism is reserved for a future release.
  max_concurrent_shell: 1
```

### 3. Execution order and result ordering

The full execution sequence for a response containing mixed tier-1 tool calls
is:

1. Tier classification loop (iterates `tool_use_blocks` in order, populates
   `tier1_blocks` and `mutating_blocks`, writes `resolved_map`).
2. Split `tier1_blocks` into `tier1_shell_blocks` and `tier1_safe_blocks`.
3. **Phase 1 (shell sequential)** — sequential loop over `tier1_shell_blocks`;
   each result written to `results[b.id]` before the next call begins.
4. **Phase 2 (safe concurrent)** — `asyncio.gather` over `tier1_safe_blocks`;
   read-only tools run in parallel as before.
5. **Phase 3 (mutating sequential)** — sequential loop over `mutating_blocks`;
   approval flow unchanged.
6. Return list reconstructed from `tool_use_blocks` order, looking up each
   `b.id` in `results`.

If a response contains only shell blocks (no `tier1_safe_blocks`), Phase 2 is
skipped. If a response contains only safe-read blocks (no
`tier1_shell_blocks`), Phase 1 is skipped. Both phases may be empty
simultaneously if all blocks are mutating.

---

## Files changed

| File | Change |
|---|---|
| `agent/agent/agent.py` | Split `tier1_blocks` in `_handle_tool_calls`; add sequential phase for `tier1_shell_blocks`; read `self._safety.max_concurrent_shell` |
| `agent/agent/config_schema.py` | Add `max_concurrent_shell: int = Field(ge=1, default=1)` to `SafetyConfig` |
| `agent/agent/safety.py` | Add `self.max_concurrent_shell = config.safety.max_concurrent_shell` to `SafetyPolicy.__init__` |
| `agent/config.yaml` | Add `max_concurrent_shell: 1` under `safety:` |
| `agent/tests/test_fix3_shell_gate.py` | New file — unit tests (see Tests section) |

No other files are touched. Tier classification logic in `SafetyPolicy.resolve_tier` is unchanged. The `_exec_tier1` inner function is unchanged. The mutating-block approval flow is unchanged.

---

## Tests

File: `agent/tests/test_fix3_shell_gate.py`

All tests are unit tests using `pytest` and `unittest.mock`. No real tools,
no real shell commands, no network I/O. `_exec_tier1` is mocked by patching
`HomelabAgent._tools.execute` with an `AsyncMock`.

**Test 1 — Two tier-1 `run_shell` calls execute sequentially**

Construct a fake API response with two `tool_use` blocks, both `name="run_shell"`,
both resolved to tier 1. The mock for `execute` records the order in which it
is called (e.g. by appending to a `call_order` list inside a side-effect
function). Assert that:
- `execute` was called exactly twice.
- The first call used the first block's input; the second call used the
  second block's input.
- The second call did not start before the first call returned — verified by
  confirming the first result was written to `results` before the second
  `execute` call was made (achievable by capturing state in the side-effect).

**Test 2 — One tier-1 `run_shell` + one tier-1 `read_logs` — both execute,
shell first**

Construct a response with one `run_shell` block (tier 1) and one `read_logs`
block (tier 1). Assert that:
- Both `execute` calls complete.
- The `run_shell` call is initiated in Phase 1 (shell sequential) and
  the `read_logs` call in Phase 2 (safe concurrent).
- The `run_shell` call's `await` completes before the `read_logs` gather is
  initiated. This can be verified with a sequential mock that asserts no
  concurrent execution occurs.

**Test 3 — Two tier-1 non-shell calls execute concurrently**

Construct a response with two tier-1 `read_logs` blocks. Both land in
`tier1_safe_blocks`. Use `unittest.mock.patch("agent.agent.agent.asyncio.gather", wraps=asyncio.gather)`
to intercept the gather call while still executing it. After `_handle_tool_calls`
returns, assert that `asyncio.gather` was called exactly once and that the
call received two coroutine arguments — confirming the gather path was taken
rather than the sequential loop. This approach is preferred over timing-based
assertions because it is deterministic and does not require `asyncio.sleep`
delays in the mock.

**Test 4 — Results appear in original `tool_use_block` order**

Construct a response with three blocks: `run_shell` (tier 1), `read_logs`
(tier 1), `run_shell` (tier 1). The mocks return distinct strings. Assert
that the returned `tool_results` list contains the three results in the order
`[shell_result_1, read_logs_result, shell_result_2]`, matching the original
block order, regardless of the phase in which each was executed.

**Test 5 — `max_concurrent_shell` is read from `SafetyPolicy`**

Construct a `SafetyConfig` with `max_concurrent_shell=3`. Instantiate
`SafetyPolicy` with a config that includes it. Assert
`safety_policy.max_concurrent_shell == 3`. This verifies the attribute
assignment in `__init__` and that `Field(ge=1)` does not reject a value of 3.

**Test 6 — `max_concurrent_shell=1` default is enforced by Pydantic**

Construct `SafetyConfig` without specifying `max_concurrent_shell`. Assert
the default value is `1`. Separately, attempt to construct `SafetyConfig`
with `max_concurrent_shell=0` and assert a `ValidationError` is raised (due
to `ge=1`).

---

## Out of scope

- Semaphore-based concurrency for `max_concurrent_shell > 1`. The field is
  wired and validated; actual parallel capping via `asyncio.Semaphore` is a
  future enhancement.
- Any change to how mutating (tier 2/3) blocks are dispatched.
- Any change to tier classification logic in `SafetyPolicy`.
- Any change to the `run_shell` tool implementation or its default tier tag
  (`"agent"` in `tool_tiers`).
- Cross-tier interaction (e.g. a tier-1 shell block completing before a
  tier-2 block is evaluated). Tier-2/3 blocks always execute after all tier-1
  phases, which is the existing behaviour and unchanged here.
- Logging changes. The `log_action_taken` call inside `_exec_tier1` is
  unchanged; sequential execution means log entries will naturally appear in
  sequential order, but no explicit ordering guarantee is added to the log
  schema.
