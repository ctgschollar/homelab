# Homelab Agent — Implementation Plan

> Hand this document to a Claude Code session. It contains everything needed to
> build the project from scratch. Do not deviate from the architecture described
> here without flagging it.

---

## Goal

Build a CLI-based autonomous sysadmin agent for a Docker Swarm homelab. Two
operational modes:

1. **Interactive REPL** — the user types questions and commands; the agent
   reasons and acts.
2. **Daemon mode** — runs headlessly, monitors service health, auto-recovers
   failures, notifies Slack.

Both modes share the same agent loop and event queue. The monitor daemon and the
CLI are just two different producers feeding the same consumer.

---

## Project Structure

```
homelab-agent/
├── agent/
│   ├── __init__.py
│   ├── agent.py        # agentic loop + Anthropic API calls
│   ├── tools.py        # tool definitions + implementations
│   ├── safety.py       # SafetyPolicy — tier resolution + safe mode enforcement
│   ├── monitor.py      # background health-check daemon
│   ├── slack.py        # Slack webhook client
│   └── prompts.py      # system prompt builder
├── cli.py              # main entrypoint (REPL, daemon, single-shot)
├── config_cli.py       # separate entrypoint for editing config
├── config.yaml         # runtime config (${ENV_VAR} substitution supported)
├── action.log          # append-only structured action log (JSONL)
├── requirements.txt
└── README.md
```

---

## Dependencies

```
anthropic>=0.40.0
docker>=7.0.0
fastapi
pyyaml>=6.0
ruamel.yaml>=0.18.0
rich>=13.0.0
```

---

## config.yaml

Supports `${ENV_VAR}` substitution via regex at load time so secrets never live
in the file. The `config_cli.py` tool reads, modifies, and rewrites this file —
it must preserve comments and structure using `ruamel.yaml` (not plain `pyyaml`).

```yaml
anthropic:
  api_key: "${ANTHROPIC_API_KEY}"
  model: "claude-sonnet-4-20250514"

slack:
  webhook_url: "${SLACK_WEBHOOK_URL}"
  channel: "#homelab-alerts"
  # How long (seconds) to wait for APPROVE/STOP before a tier-2 plan times out.
  # Timeout = cancelled, not executed.
  veto_window_seconds: 300

docker:
  socket: "unix:///var/run/docker.sock"

swarm:
  nodes:
    - dks01.schollar.dev
    - dks02.schollar.dev
    - dks03.schollar.dev
    - dks04.schollar.dev
    - dks05.schollar.dev
  ssh_key: "/root/.ssh/ansible_ssh_key"
  ssh_user: "root"

ansible:
  repo_path: "/opt/homelab"
  inventory: "/opt/homelab/ansible/inventory.yml"

monitor:
  poll_interval: 30
  watched_stacks:
    - traefik
    - monitoring
    - postgres
    - coredns

# -----------------------------------------------------------------------
# Safety policy
# -----------------------------------------------------------------------
safety:
  # Global safe mode. When true, ALL actions behave like tier 3 regardless
  # of their tool tier tag or the agent's own determination. The agent will
  # diagnose and propose a plan, post it to Slack, and wait for APPROVE
  # before executing anything.
  # Recommended: start with true, disable once you trust the agent's
  # categorisation.
  global_safe_mode: true

  # Per-resource safe mode. Any action whose primary target matches a value
  # in these lists is forced to tier-3 behaviour, even when global_safe_mode
  # is false. Matched as exact prefix against service name, stack name, or
  # node hostname.
  safe_mode_resources:
    stacks: []       # e.g. ["traefik", "monitoring"]
    services: []     # e.g. ["traefik_traefik"]
    nodes: []        # e.g. ["dks01.schollar.dev"]

  # Explicit tier tag per tool. Valid values:
  #   1       — act immediately, notify Slack after
  #   2       — post plan to Slack, wait for veto window, then act
  #   3       — always ask, never act autonomously
  #   "agent" — agent uses its own judgement (reasoning logged if
  #             log_agent_tier_reasoning is true)
  #
  # These overrides are evaluated after safe mode — safe mode always wins.
  tool_tiers:
    docker_service_list:     1       # read-only
    docker_service_inspect:  1       # read-only
    read_logs:               1       # read-only
    read_file:               1       # read-only
    get_prometheus_alerts:   1       # read-only
    slack_notify:            1       # always allowed
    docker_service_scale:    2
    docker_stack_deploy:     2
    run_ansible_playbook:    2
    run_shell:               "agent"
    write_file:              3

  # When a tool is tagged "agent" and the agent selects a tier, log its
  # reasoning in the action log. Toggle with: python config_cli.py log-reasoning on|off
  log_agent_tier_reasoning: true

# -----------------------------------------------------------------------
 Action log
# -----------------------------------------------------------------------
action_log:
  path: "./action.log"

# -----------------------------------------------------------------------
# Slack approval listener
# -----------------------------------------------------------------------
approval_listener:
  # aiohttp server that receives Slack outgoing webhook POSTs with
  # APPROVE <plan_id> or STOP <plan_id>
  host: "0.0.0.0"
  port: 8765
```

---

## Infrastructure Context (bake into system prompt)

Put all of this in `prompts.py` as a constant — do not fetch it at runtime.

**Nodes:**
- Proxmox cluster: `prx01`–`prx05` at `192.168.3.101`–`.105`
- Docker Swarm VMs: `dks01`–`dks05` at `192.168.3.70`–`.74`
- `prx01` has NVIDIA RTX 3050 (GPU passthrough to media VM)
- `prx05` has 2×14TB RAID 0 for media storage

**Swarm service placement constraints:**
- `node.labels.traefik == true` → traefik (3 replicas)
- `node.labels.media == true` → jellyfin, immich, radarr, sonarr, qbittorrent,
  jellyseerr, xteve, lazylibarian, calibre-web, audiobookshelf
- `node.labels.media == true` AND `node.labels.gpu == true` → jellyfin
- `node.labels.metrics == true` → prometheus, grafana, alertmanager, pve-exporter
- `node.labels.linstor == true` → postgres, hedgedoc, jellyseerr
- `node.labels.registry == true` → registry
- `node.role == manager` → portainer, homepage, coredns, prometheus

**Storage:**
- LINSTOR volumes: driver `linbit/linstor-docker-volume`, pools `pool_ssd` /
  `pool_hdd`, 2 replicas standard
- CephFS: `/mnt/cephfs-configs/<service>/.env` (service secrets),
  `/mnt/shared/` (media library)
- Proxmox Backup Server backs up `/var/lib/` on all dks nodes nightly

**Networking:**
- All services on `traefik-net` external overlay network
- Domain: `*.schollar.dev`, SSL via Cloudflare DNS-01
- Traffic: Internet → Cloudflare Tunnel → Traefik → services
- Tailscale: `100.83.70.76` = Traefik via Tailscale
- CoreDNS on port 53 (LAN), port 5353 (Tailscale)

**Compose files** live at `/opt/homelab/<stack_name>/docker-compose.yaml`.

**Monitoring:** Prometheus + Grafana, blackbox exporter, Alertmanager at
`http://alertmanager:9093`.

---

## Autonomy Tiers

Enforced by `SafetyPolicy` in code and reinforced in the system prompt. The
code enforcement is authoritative.

| Tier | Behaviour |
|------|-----------|
| 1 | Act immediately, write to action log, notify Slack after |
| 2 | Post plan to Slack with plan ID, wait `veto_window_seconds` for `APPROVE <id>` or `STOP <id>`. Timeout → cancel and log. |
| 3 | Post plan to Slack with plan ID, wait indefinitely for `APPROVE <id>`. No timeout — must receive explicit approval. `STOP <id>` → cancel and log. |

Safe mode forces all tiers to tier-3 behaviour. The action log records the
original resolved tier alongside a `safe_mode_active: true` flag so you can
audit whether the agent was categorising correctly before granting autonomy.

**Tier resolution order** (highest priority first):
1. `global_safe_mode: true` → tier-3 behaviour, log original tier + `safe_mode_active: true`
2. Target resource in `safe_mode_resources` → tier-3 behaviour, same logging
3. Tool has explicit value in `tool_tiers` config and it is `1`, `2`, or `3` → use it
4. Tool is `"agent"` in `tool_tiers` → agent determines tier, logs reasoning if `log_agent_tier_reasoning: true`
5. Tool has a hardcoded default tier in its definition → use that

---

## safety.py — SafetyPolicy

Single place where all tier resolution happens. `agent.py` and `tools.py` never
implement tier logic — they always call `SafetyPolicy`.

```python
@dataclass
class ResolvedTier:
    tier: int                     # effective tier after all overrides (1, 2, or 3)
    safe_mode_active: bool        # true if safe mode forced the tier up
    original_tier: int | None     # tier before safe mode override
    agent_reasoning: str | None   # set when tool is "agent"-discretion

class SafetyPolicy:
    def __init__(self, config: dict): ...

    def resolve_tier(
        self,
        tool_name: str,
        target_resource: str | None,    # service/stack/node being acted on
        agent_proposed_tier: int | None,  # only when tool tagged "agent"
        agent_reasoning: str | None,      # only when tool tagged "agent"
    ) -> ResolvedTier: ...
```

`resolve_tier` is pure and synchronous — no I/O, no side effects.

---

## action.log

Append-only JSONL file (one JSON object per line). Written by a dedicated
`ActionLogger` class. Never truncated by the agent. Use `asyncio.Lock` to
serialise concurrent writes.

### Schema by event type

**`action_taken`** — tool executed (tier 1, or tier 2/3 after approval)
```json
{
  "ts": "2025-03-23T14:05:01Z",
  "event": "action_taken",
  "tool": "docker_stack_deploy",
  "input": {"stack_name": "jellyfin"},
  "outcome": "Stack jellyfin deployed successfully",
  "tier": 1,
  "safe_mode_active": false,
  "trigger": "monitor:service_down"
}
```

**`plan_proposed`** — tier 2/3 plan posted to Slack, awaiting approval
```json
{
  "ts": "2025-03-23T14:05:01Z",
  "event": "plan_proposed",
  "plan_id": "plan-a3f2",
  "tool": "run_ansible_playbook",
  "input": {"playbook": "linstor-backup/playbook.yml"},
  "plan_text": "Run linstor backup playbook on all nodes to restore backup config",
  "tier": 2,
  "safe_mode_active": false,
  "trigger": "cli:user_message"
}
```

**`plan_approved`**
```json
{
  "ts": "2025-03-23T14:08:34Z",
  "event": "plan_approved",
  "plan_id": "plan-a3f2",
  "tool": "run_ansible_playbook",
  "approved_by": "slack:APPROVE"
}
```

**`plan_cancelled`**
```json
{
  "ts": "2025-03-23T14:10:01Z",
  "event": "plan_cancelled",
  "plan_id": "plan-a3f2",
  "tool": "run_ansible_playbook",
  "reason": "timeout"
}
```

`reason` is `"timeout"` or `"slack:STOP"`.

**`tier_reasoning`** — emitted when tool is `"agent"`-discretion and
`log_agent_tier_reasoning` is true
```json
{
  "ts": "2025-03-23T14:05:00Z",
  "event": "tier_reasoning",
  "tool": "run_shell",
  "agent_proposed_tier": 2,
  "reasoning": "Command reads disk usage across multiple nodes — diagnostic only, but involves SSH to 5 nodes. Treating as tier 2.",
  "safe_mode_active": false,
  "effective_tier": 2
}
```

**`monitor_alert`**
```json
{
  "ts": "2025-03-23T13:58:10Z",
  "event": "monitor_alert",
  "service": "jellyfin_jellyfin",
  "running": 0,
  "desired": 1,
  "last_error": "no suitable node (constraint node.labels.gpu)"
}
```

**`monitor_recovered`**
```json
{
  "ts": "2025-03-23T14:05:45Z",
  "event": "monitor_recovered",
  "service": "jellyfin_jellyfin",
  "down_duration_seconds": 455
}
```

---

## Event Queue

`asyncio.Queue`. Two producers, one consumer.

```python
@dataclass
class Event:
    source: str    # "monitor" | "cli"
    type: str      # "service_down" | "service_recovered" | "user_message"
    data: dict
    timestamp: datetime
```

---

## monitor.py

`MonitorDaemon` runs as an `asyncio.Task`. Has its own reference to
`ActionLogger` and writes `monitor_alert` / `monitor_recovered` entries
directly — these do not go through the agent loop.

**State tracking:** `dict[str, datetime]` of known-down services. Emit
`service_down` event only on first detection per outage. Emit
`service_recovered` when service returns.

**Health check logic:**
1. `docker.services.list()`
2. For each replicated service with `desired > 0`:
   - Count tasks where `Status.State == "running"` AND `DesiredState == "running"`
   - `running < desired` → degraded
3. Skip `desired == 0` (intentionally stopped)
4. Skip global-mode services for now

Wrap entire poll in try/except — a Docker daemon restart must not crash the
monitor.

---

## agent.py

### Agentic loop (inner)

```
while iterations < MAX_ITERATIONS (15):
    call Anthropic API (model, max_tokens=4096, system, messages, tools)
    append response to history
    print text blocks to terminal
    if stop_reason == "end_turn": break
    if stop_reason == "tool_use":
        for each tool_use block:
            resolved = safety_policy.resolve_tier(tool, resource, agent_tier, reasoning)
            if log_agent_tier_reasoning and resolved.agent_reasoning:
                action_logger.log_tier_reasoning(...)
            if resolved.tier == 1:
                result = await tools.execute(tool, input)
                action_logger.log_action_taken(...)
            elif resolved.tier in (2, 3):
                result = await handle_approval_flow(tool, input, resolved)
        collect all results, append as role="user" tool_results
```

Read-only tier-1 calls within a single turn can be gathered with
`asyncio.gather`. Mutating calls (tier 2/3) are sequential and gated.

### Approval flow

Shared by tier 2 and tier 3. Difference: tier 2 has a timeout, tier 3 does not.

1. Generate a short plan ID: `plan-{4 random hex chars}`
2. Format plan text: tool name, inputs, plain-English description of what will happen
3. `slack.notify_plan(plan_id, plan_text, veto_seconds)` — include the plan ID
   prominently in the message so the user can reference it
4. `action_logger.log_plan_proposed(...)`
5. Register `plan_id` in `PendingApprovals` (a shared `dict[str, asyncio.Future]`)
6. `await asyncio.wait_for(future, timeout)` — tier 2 uses `veto_window_seconds`,
   tier 3 uses `None` (no timeout)
7. On approval: execute tool, log `plan_approved` + `action_taken`
8. On `STOP` or timeout: log `plan_cancelled`, return `"[cancelled: {reason}]"` to Claude

### Slack approval listener

Minimal `fastapi` server started as a background task alongside the agent and
monitor. Listens on `approval_listener.host:port` from config.

- Endpoint: `POST /slack`
- Parses request body for `APPROVE <plan_id>` or `STOP <plan_id>` (case-insensitive)
- Resolves the matching `asyncio.Future` in `PendingApprovals`
- Returns HTTP 200 in all cases (Slack requires a 200 response)
- If plan ID not found: return 200 with body `"Unknown plan ID"` — do not error

Configure Slack outgoing webhook to POST to `http://<agent-node-ip>:8765/slack`.

### Terminal output (rich)

- Agent text: cyan `Agent:` prefix
- Tool calls: yellow `  > tool_name(key=value, ...)`
- Safe mode active on a call: amber `  [SAFE MODE — tier forced to 3]`
- Tool results: dim, truncated to 3 lines unless explicitly requested
- Tier reasoning: dim italic, indented under the tool call line

### Conversation history

`self.history: list[dict]` across events. Cap at `MAX_HISTORY_TURNS = 20`
turn-pairs, trimming from the front.

### Event → user message

- `service_down` → `"[MONITOR ALERT] Service {svc} is degraded: {running}/{desired} replicas running. Last error: {err}. Investigate and take appropriate action per your autonomy tier rules."`
- `service_recovered` → `"[MONITOR] Service {svc} has recovered after {dur}s. Notify Slack."`
- `user_message` → pass through as-is

---

## tools.py

`ToolExecutor(config, slack_client)`. Single `execute(tool_name, tool_input) -> str`
dispatcher. All methods async. Docker SDK calls wrapped in `run_in_executor`.

**Implementation notes:**

- `docker_stack_deploy`: default path = `{repo_path}/{stack_name}/docker-compose.yaml`.
  Subprocess: `docker stack deploy --with-registry-auth -c {path} {stack_name}`
- `read_logs`: `docker service logs --no-trunc --tail {lines} {service_name}`
- `run_shell` SSH: `ssh -i {key} -o StrictHostKeyChecking=no -o ConnectTimeout=10 {user}@{node} '{cmd}'`
- `run_ansible_playbook`: `ansible-playbook -i {inventory} {playbook}` with
  optional `--limit` and `-e '{json}'`. Timeout 300s.
- `get_prometheus_alerts`: aiohttp GET `http://alertmanager:9093/api/v2/alerts`.
  Format as `[SEVERITY] alertname: summary`. Return `"No active alerts"` if empty.
- All subprocesses: `asyncio.create_subprocess_exec`, capture stdout+stderr,
  timeout 60s default, return combined string.
- On any error: return `"ERROR: {message}"` — never raise.

---

## slack.py

`SlackClient(webhook_url, channel)`. Single `aiohttp.ClientSession` reused.

**Attachment colours:** `info` #378ADD · `warning` #EF9F27 · `error` #E24B4A ·
`success` #639922 · `action` #7F77DD

**Convenience methods:**
- `notify_action_taken(action, service, reason)` — post-action tier-1 notification
- `notify_plan(plan_id, plan_text, veto_seconds)` — include plan ID prominently;
  for tier 3 omit the timeout language
- `notify_alert(alert, service)` — monitor detected degradation
- `notify_resolved(service, how)` — service recovered

---

## cli.py

```
python cli.py                            # interactive REPL + monitor
python cli.py "why is sonarr down?"     # single question, exit when done
python cli.py --daemon                  # monitor + agent, no stdin
python cli.py --check                   # list service status and exit
python cli.py --config path/to/cfg.yaml
```

**Interactive REPL:**
- Background tasks: `MonitorDaemon`, `HomelabAgent`, Slack approval listener
- Input via `loop.run_in_executor(None, input_fn)` — never block the loop
- Built-in commands: `/quit`, `/status`, `/history`, `/safemode` (shows current
  state — does not toggle; use `config_cli.py` to change)
- Clean cancellation on `/quit` or `KeyboardInterrupt`

**Single message mode:** inject one event, `queue.join()`, exit.

**Daemon mode:** all three background tasks, no stdin.

---

## config_cli.py

Separate entrypoint. Uses `ruamel.yaml` to preserve comments on write. Never
writes env var placeholders as resolved values — detect `${...}` patterns in the
raw file and preserve them unchanged.

```
python config_cli.py show
python config_cli.py get safety.global_safe_mode
python config_cli.py set safety.global_safe_mode true
python config_cli.py set safety.tool_tiers.run_shell 2
python config_cli.py set safety.tool_tiers.docker_stack_deploy agent
python config_cli.py safemode on|off
python config_cli.py safe-resource add stack|service|node <value>
python config_cli.py safe-resource remove stack|service|node <value>
python config_cli.py safe-resource list
python config_cli.py log-reasoning on|off
```

Validate before writing: tier values must be `1`, `2`, `3`, or `"agent"`;
booleans must parse cleanly. Print a confirmation of what changed after every
write.

---

## Future: Multi-agent Extension

When a Research Agent is added, the pattern is:

```
User message
     ↓
OrchestratorAgent  (Claude call with routing prompt)
     ├── HomelabAgent   (this project)
     └── ResearchAgent  (future)
```

Design `HomelabAgent` so it can be called programmatically
(`await agent.chat(message) -> str`) as well as from the CLI. No other changes
needed — it becomes one of several agents the orchestrator delegates to.
