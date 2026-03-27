# Agent Fix Implementation Plan

Six issues identified in the design review. Each section is self-contained and
can be handed to a Claude Code session independently. Tackle them in the order
listed — fixes 1 and 3 share a touch point in `safety.py` / `agent.py` and are
easier to reason about together before moving on.

---

## Fix 1 — `run_shell` command pattern guards

### Problem

`run_shell` is tagged `"agent"` in `tool_tiers`, so the model proposes its own
safety tier. Nothing in `SafetyPolicy` validates that proposal against the
actual command string. The incident log shows the agent classifying `git push`
and credential config reads as tier 1 ("read-only-equivalent"). Any sufficiently
plausible-sounding `agent_reasoning` string will pass through unchanged.

### Files to change

- `agent/agent/safety.py`
- `agent/config.yaml`
- `agent/agent/prompts.py` (update tier guidance to match new behaviour)

### Steps

1. **Add a denylist of dangerous command patterns to `safety.py`.**

   Define a module-level list of compiled regex patterns:

   ```python
   import re

   _SHELL_FORCE_TIER3: list[re.Pattern] = [
       re.compile(r'\brm\s+-rf?\b'),
       re.compile(r'\bmkfs\b'),
       re.compile(r'\bdd\b.*\bof='),
       re.compile(r'\bparted\b'),
       re.compile(r'\bfdisk\b'),
       re.compile(r'\bwipefs\b'),
       re.compile(r'\bshred\b'),
       re.compile(r'\btruncate\b'),
       re.compile(r'>\s*/dev/'),
   ]

   _SHELL_FORCE_TIER2: list[re.Pattern] = [
       re.compile(r'\bsystemctl\b.*(restart|stop|start|disable|enable)'),
       re.compile(r'\bdocker\b.*(rm|rmi|prune|kill)'),
       re.compile(r'\breboot\b'),
       re.compile(r'\bpoweroff\b'),
       re.compile(r'\bshutdown\b'),
       re.compile(r'\biptables\b'),
       re.compile(r'\bufw\b.*(delete|disable|reset)'),
       re.compile(r'\bpasswd\b'),
       re.compile(r'\busermod\b'),
       re.compile(r'\bchmod\b\s+[0-7]*7'),   # world-writable
       re.compile(r'\bchown\b'),
       re.compile(r'\bgit\s+push\b'),
       re.compile(r'\bgit\s+reset\b'),
       re.compile(r'\bgit\s+config\b'),
       re.compile(r'\bcrontab\b'),
       re.compile(r'\bsed\b.*-i'),            # in-place file edit
       re.compile(r'\bawk\b.*>'),             # awk writing to file
       re.compile(r'\bwget\b.*-O\b'),         # writing downloaded content
       re.compile(r'\bcurl\b.*(-o\b|-O\b|--output)'),
   ]
   ```

   These lists should be configurable — see step 3.

2. **Add a `_check_shell_command(command: str, agent_proposed_tier: int) -> int`
   method to `SafetyPolicy`.**

   Logic:
   - If any `_SHELL_FORCE_TIER3` pattern matches → return 3, regardless of agent
     proposal.
   - If any `_SHELL_FORCE_TIER2` pattern matches → return `max(2,
     agent_proposed_tier)` (never lower than what the agent proposed, never below
     2).
   - Otherwise → return `agent_proposed_tier`.

3. **Call `_check_shell_command` inside `_base_tier`** when the configured tier
   is `"agent"` and `tool_name == "run_shell"`. Replace the raw
   `agent_proposed_tier` passthrough with the guarded result.

4. **Add a `safety.shell_command_guards` section to `config.yaml`** so the
   operator can add patterns at runtime without touching code:

   ```yaml
   safety:
     shell_command_guards:
       force_tier3:
         - 'rm\s+-rf?'
         - 'mkfs'
         # ... etc
       force_tier2:
         - 'systemctl.*(restart|stop)'
         # ... etc
   ```

   In `SafetyPolicy.__init__`, load these lists and compile them, then merge
   with the hardcoded defaults (hardcoded defaults always present; config adds
   to them, never replaces).

5. **Log when a pattern guard overrides the agent's proposed tier.** Add an
   `override_reason: "shell_pattern_guard"` field to the `tier_reasoning` log
   entry when this happens.

6. **Update `TIER_RULES` in `prompts.py`** to document that pattern guards exist
   and will silently escalate tier proposals. The agent should know why it
   sometimes sees tier-2/3 behaviour for commands it classified as tier 1.

---

## Fix 2 — Slack listener signature verification (already partially implemented)

### Problem

`SlackClient.verify_signature` exists and is called in both `/slack/events` and
`/slack/interactions`. However, when `slack.configured` is false (no bot token
set), verification is skipped entirely. The listener still binds to `0.0.0.0`
and accepts any POST that contains a known plan ID format. Plan IDs are 4 hex
chars (65,536 possibilities), low enough to brute-force on a LAN.

### Files to change

- `agent/agent/agent.py` (the `build_approval_app` function and listener startup)
- `agent/config.yaml`

### Steps

1. **Verify `signing_secret` independently of `bot_token`.** The `configured`
   property currently checks `bool(self._token)`. Add a separate
   `signature_verification_enabled` property that checks `bool(self._secret) and
   not self._secret.startswith("${")`. Update both endpoint handlers to use this
   property for the signature check guard.

2. **Enforce that if the listener is started, signature verification must be
   enabled, OR the listener must bind to localhost only.**

   In `agent.py`, before starting the approval listener, check:
   - If `signing_secret` is not configured AND `host == "0.0.0.0"` → log a
     prominent warning and force `host = "127.0.0.1"`.
   - Print a startup warning visible at the terminal: `[bold red]WARNING: Slack
     signing secret not configured — approval listener restricted to
     localhost[/bold red]`.

3. **Increase plan ID entropy.** Change `secrets.token_hex(2)` (4 chars) to
   `secrets.token_hex(4)` (8 chars). Update log schema comments and any tests
   that reference the old format.

4. **Add `signing_secret` to the startup validation** (see Fix 5) so a missing
   secret surfaces at boot, not silently at runtime.

5. **Document the Slack app configuration requirement** in `README.md`: the
   signing secret must be set; the Events API and Interactivity endpoints must
   be configured in the Slack app settings to POST to
   `https://<host>/slack/events` and `https://<host>/slack/interactions`
   respectively.

---

## Fix 3 — Concurrent tier-1 execution gate

### Problem

`_handle_tool_calls` separates tool calls into `tier1_blocks` and
`mutating_blocks`, then fires all tier-1 calls concurrently with
`asyncio.gather`. Tier classification happens before any result is seen. If the
agent requests multiple `run_shell` calls in one API response and self-classifies
them all as tier 1, they all execute in parallel with no inter-result reasoning.

### Files to change

- `agent/agent/agent.py`

### Steps

1. **Cap concurrent tier-1 `run_shell` calls at 1.** Any other tier-1 tool
   (reads, log fetches, prometheus checks) can remain concurrent — they're
   genuinely idempotent. But `run_shell` — even when genuinely read-only — is
   arbitrary shell execution and should not fan out in parallel.

   In `_handle_tool_calls`, after separating `tier1_blocks`, split further:

   ```python
   tier1_shell_blocks = [b for b in tier1_blocks if b.name == "run_shell"]
   tier1_safe_blocks  = [b for b in tier1_blocks if b.name != "run_shell"]
   ```

   - `tier1_safe_blocks` → gathered concurrently as today.
   - `tier1_shell_blocks` → executed **sequentially**, one at a time, results
     collected individually before the next fires.

2. **Add a config option `safety.max_concurrent_shell: int` (default 1)** so
   the operator can increase this deliberately once they trust the agent's
   classification. Wire it through `SafetyPolicy` and pass it into
   `_handle_tool_calls`.

3. **Do not change the gather behaviour for non-shell tier-1 tools.** Concurrent
   reads across multiple nodes are fine and useful for diagnostic speed.

---

## Fix 4 — History trimming / action context loss

### Problem

`_trim_history` drops the oldest turn-pairs when `len(history) > MAX_HISTORY_TURNS * 2`. After trimming, the agent has no visibility into what actions it already executed in this session. In a long incident — monitor alert → diagnose → deploy → re-deploy → investigate — the agent may re-diagnose and re-propose actions it already ran.

### Files to change

- `agent/agent/agent.py`
- `agent/agent/prompts.py`

### Steps

1. **Inject a "prior actions" summary into the system prompt on every API call.**

   `build_system_prompt` already returns a static string. Change the call site
   in `_api_create` to pass in a `prior_actions: list[dict]` argument. Build a
   compact summary from the `action_log` for the current session (defined as
   entries since agent startup, i.e. filter by `ts >= self._started_at`).

   Format as a fenced block appended after the existing prompt sections:

   ```
   ## Actions taken this session
   - 14:03 docker_stack_deploy(jellyfin) → Stack jellyfin deployed successfully [tier 2]
   - 14:05 run_shell(df -h, dks01) → [result truncated] [tier 1]
   ```

   Cap at the 20 most recent entries. If no actions have been taken, omit the
   section entirely.

2. **Read from `action.log` at call time, not from memory.** The log is the
   source of truth. Read the last N `action_taken` entries from the JSONL file,
   filter to the current session by timestamp, and format them. This ensures the
   summary survives history trimming, agent restarts mid-session, and concurrent
   writes from the monitor.

3. **Set `self._started_at = datetime.now(timezone.utc)` in `__init__`** so
   "current session" is well-defined.

4. **Keep `_trim_history` as-is** — it is still needed to keep the API message
   list from growing unbounded. The system prompt summary is the complement, not
   a replacement.

5. **Update `build_system_prompt` signature** to accept `prior_actions:
   list[dict] | None = None` and render them only when provided.

---

## Fix 5 — Duplicate config key + startup validation

### Problem

`config.yaml` contains `commit_config_updates` twice under `tool_tiers` — once
as tier 2, once as tier 3. Python dict parsing silently takes the last value
(tier 3). There is no startup check that catches this or any other config
inconsistency.

### Files to change

- `agent/config.yaml`
- `agent/agent/config_schema.py` (new file)
- `agent/agent/agent.py` (`load_config` call site)
- `agent/cli.py`
- `agent/config_cli.py`
- `requirements.txt`

### Steps

1. **Fix the duplicate key in `config.yaml`** — remove the tier-2 entry for
   `commit_config_updates`, leaving only the tier-3 entry. Add a comment
   explaining why it is tier 3.

2. **Add `pydantic>=2.0` to `requirements.txt`.**

3. **Create `agent/agent/config_schema.py`** containing a full Pydantic v2 model
   tree for the config. Example structure:

   ```python
   from typing import Literal
   from pydantic import BaseModel, Field, field_validator, model_validator
   from pathlib import Path

   TierValue = Literal[1, 2, 3, "agent"]

   class AnthropicConfig(BaseModel):
       api_key: str
       model: str
       input_cost_per_mtok: float = 3.0
       output_cost_per_mtok: float = 15.0

   class SlackConfig(BaseModel):
       bot_token: str
       signing_secret: str = ""
       channel: str
       veto_window_seconds: int = Field(gt=0, default=300)

   class SwarmConfig(BaseModel):
       nodes: list[str] = Field(min_length=1)
       ssh_key: str
       ssh_user: str

       @field_validator("ssh_key")
       @classmethod
       def ssh_key_exists(cls, v: str) -> str:
           if not v.startswith("${") and not Path(v).exists():
               # Warning only — key may not be deployed yet
               import warnings
               warnings.warn(f"ssh_key path does not exist: {v}")
           return v

   class SafetyConfig(BaseModel):
       global_safe_mode: bool = True
       safe_mode_resources: dict = Field(default_factory=dict)
       tool_tiers: dict[str, TierValue] = Field(default_factory=dict)
       log_agent_tier_reasoning: bool = True
       shell_command_guards: dict[str, list[str]] = Field(default_factory=dict)
       max_concurrent_shell: int = Field(ge=1, default=1)

   class MonitorConfig(BaseModel):
       poll_interval: int = Field(gt=0, default=30)

   class ApprovalListenerConfig(BaseModel):
       host: str = "0.0.0.0"
       port: int = Field(ge=1024, le=65535, default=8765)

   class AgentConfig(BaseModel):
       anthropic: AnthropicConfig
       slack: SlackConfig
       swarm: SwarmConfig
       safety: SafetyConfig
       monitor: MonitorConfig
       approval_listener: ApprovalListenerConfig
       # ... remaining sections

       @model_validator(mode="after")
       def signing_secret_warning(self) -> "AgentConfig":
           s = self.slack.signing_secret
           if not s or s.startswith("${"):
               import warnings
               warnings.warn(
                   "slack.signing_secret is not set — approval listener will be "
                   "restricted to localhost (see Fix 2)"
               )
           return self
   ```

   Add validators for every constraint that was previously undocumented. Pydantic
   will raise `ValidationError` on type mismatches, missing required fields, and
   out-of-range values automatically.

4. **Update `load_config` in `cli.py`** (or wherever it currently lives) to
   parse the raw YAML dict through `AgentConfig.model_validate(raw)`. Catch
   `pydantic.ValidationError`, print each error with a `[bold red]CONFIG
   ERROR:[/bold red]` prefix, and exit with code 1.

   ```python
   from pydantic import ValidationError
   from agent.config_schema import AgentConfig

   def load_config(path: str) -> AgentConfig:
       raw = _load_and_substitute_yaml(path)
       try:
           return AgentConfig.model_validate(raw)
       except ValidationError as e:
           for err in e.errors():
               loc = " → ".join(str(x) for x in err["loc"])
               console.print(f"[bold red]CONFIG ERROR:[/bold red] {loc}: {err['msg']}")
           sys.exit(1)
   ```

5. **Replace all `config["key"]["subkey"]` dict access throughout the codebase**
   with attribute access on the `AgentConfig` object (e.g.
   `config.safety.global_safe_mode`). This is the main mechanical work of this
   fix — do it file by file: `agent.py`, `safety.py`, `tools.py`, `monitor.py`,
   `slack.py`, `cli.py`, `config_cli.py`.

6. **Update `config_cli.py`** so that after writing a change with `ruamel.yaml`,
   it re-parses and re-validates the result through `AgentConfig.model_validate`.
   If validation fails, abort the write and print the error. This catches
   operator mistakes at the point of edit rather than at the next agent startup.

7. **Add `validate` as a `config_cli.py` command**:

   ```
   python config_cli.py validate
   ```

   Loads and validates the config, prints any errors or warnings, exits 0 if
   clean and 1 if errors. Warnings (missing signing secret, missing SSH key path)
   are printed but do not affect exit code.

---

## Fix 6 — Monitor: recover all stacks, with operator-controlled muting

### Problem

`monitor.watched_stacks` implies intentional scoping but the health check
actually runs against all swarm services regardless. The config key is
misleading and the behaviour undocumented. More importantly, there is no
mechanism to suppress a persistently failing stack that the agent cannot fix —
it will trigger the agent loop on every poll indefinitely.

### Desired behaviour

- Monitor every service in the swarm. No config list required.
- Any degraded service triggers autonomous recovery via the agent loop.
- If a stack cannot be fixed, the agent proposes muting it to Slack. The
  operator approves or denies. A muted stack is silently skipped on future
  polls.
- If a muted stack recovers on its own, it is automatically unmuted and normal
  monitoring resumes.

### Files to change

- `agent/config.yaml`
- `agent/agent/monitor.py`
- `agent/agent/agent.py` (new `mute_stack` tool)
- `agent/agent/tools.py` (register `mute_stack` tool)
- `agent/IMPLEMENTATION_PLAN.md`

### Steps

1. **Remove `watched_stacks` from `config.yaml` and from `MonitorConfig` in
   `config_schema.py`** (Fix 5). The monitor no longer needs a stack list.
   `poll_interval` remains.

2. **Add a `MuteStore` class to `monitor.py`** — a simple persistent store for
   muted stacks, backed by a JSON file (e.g. `muted_stacks.json` alongside
   `action.log`):

   ```python
   @dataclass
   class MuteEntry:
       stack: str
       muted_at: datetime
       reason: str          # free text from the agent's proposal

   class MuteStore:
       def __init__(self, path: str) -> None: ...
       def mute(self, stack: str, reason: str) -> None: ...
       def unmute(self, stack: str) -> None: ...
       def is_muted(self, stack: str) -> bool: ...
       def all_muted(self) -> list[MuteEntry]: ...
   ```

   Use a `asyncio.Lock` for writes. The file is read at startup and written on
   every mute/unmute operation.

3. **Update `MonitorDaemon.__init__`** to accept a `MuteStore` reference.

4. **Update the health check loop in `MonitorDaemon`**:
   - Remove all `watched_stacks` filtering — iterate over all replicated
     services as before.
   - Before emitting a `service_down` event, call `mute_store.is_muted(stack_name)`.
     If muted → skip silently (no Slack, no queue event, no log entry).
   - Auto-unmute on recovery: in the `service_recovered` path, after logging,
     call `mute_store.unmute(stack_name)`. If the stack was previously muted,
     log an additional `stack_unmuted` event and notify Slack that monitoring
     has resumed.

5. **Add a `mute_stack` tool** to `tools.py` and register it in
   `TOOL_DEFINITIONS`:

   ```python
   {
       "name": "mute_stack",
       "description": (
           "Propose muting a stack that cannot be recovered. Posts a mute proposal "
           "to Slack for operator approval. If approved, the stack is suppressed "
           "from future monitor alerts until it recovers on its own."
       ),
       "input_schema": {
           "type": "object",
           "properties": {
               "stack": {"type": "string", "description": "Stack name to mute (e.g. 'jellyfin')."},
               "reason": {"type": "string", "description": "Why the stack cannot be recovered."},
           },
           "required": ["stack", "reason"],
       },
   }
   ```

   Assign `mute_stack` tier 3 in `config.yaml` and in `_DEFAULT_TIERS` in
   `safety.py` — muting is irreversible until recovery, so it always requires
   explicit operator approval.

   The tool implementation calls `mute_store.mute(stack, reason)` only after
   the tier-3 approval flow completes.

6. **Update `BEHAVIOUR_RULES` in `prompts.py`** to instruct the agent:
   - After N consecutive failed recovery attempts for the same stack (suggested
     default: 3), call `mute_stack` with a clear explanation.
   - Do not retry a stack indefinitely — escalate to muting if recovery is not
     progressing.

7. **Expose mute state in the `/status` REPL command** — list any currently
   muted stacks alongside their mute timestamps and reasons.

8. **Add `action_log` entries** for `stack_muted` and `stack_unmuted` events,
   consistent with the existing schema.

   ```json
   {"event": "stack_muted", "stack": "jellyfin", "reason": "GPU node offline, constraint unsatisfiable", "ts": "..."}
   {"event": "stack_unmuted", "stack": "jellyfin", "reason": "auto: service recovered", "ts": "..."}
   ```

---

## Testing notes

Each fix should be verified in order:

- **Fix 1:** Test that `run_shell(command="rm -rf /tmp/test", ...)` with
  `agent_proposed_tier=1` resolves to tier 3. Test that `git push` resolves to
  at least tier 2. Test that `df -h` with tier 1 passes through unchanged.
- **Fix 2:** Test that starting the listener without a signing secret binds to
  127.0.0.1, not 0.0.0.0. Test that a request without a valid signature returns
  403 when the secret is configured.
- **Fix 3:** Confirm that two concurrent `run_shell` tier-1 calls in a single
  API response execute sequentially, not in parallel (add a timing assertion or
  mock).
- **Fix 4:** Confirm the system prompt includes prior `action_taken` entries
  from the current session after a history trim occurs.
- **Fix 5:** Pass a dict with an invalid tier value (e.g. `tool_tiers:
  {run_shell: 5}`) to `AgentConfig.model_validate` and confirm a
  `ValidationError` is raised. Pass a port of 80 and confirm rejection. Confirm
  startup exits with code 1 on any validation error.
- **Fix 6:** Confirm a degraded service whose stack is in `MuteStore` is
  silently skipped (no queue event, no Slack). Confirm that when that service
  recovers, `MuteStore.is_muted` returns False and a `stack_unmuted` log entry
  is written. Confirm `mute_stack` tool is tier 3 and requires explicit
  approval before `MuteStore.mute` is called.