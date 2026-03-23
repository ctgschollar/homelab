import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
import httpx
from rich.console import Console

_console = Console()


# ---------------------------------------------------------------------------
# Anthropic tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "docker_service_list",
        "description": "List all Docker Swarm services with their replica counts and image.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stack": {
                    "type": "string",
                    "description": "Optional stack name to filter services by.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "docker_service_inspect",
        "description": "Inspect a specific Docker Swarm service for detailed configuration and task state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "The full service name (e.g. jellyfin_jellyfin).",
                }
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "read_logs",
        "description": "Read recent logs from a Docker Swarm service.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "The full service name (e.g. jellyfin_jellyfin).",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to retrieve (default 100).",
                },
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the local filesystem (e.g. a compose file or config).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file on the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "docker_service_scale",
        "description": "Scale a Docker Swarm service to a given number of replicas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "The full service name (e.g. jellyfin_jellyfin).",
                },
                "replicas": {
                    "type": "integer",
                    "description": "Desired number of replicas.",
                },
            },
            "required": ["service_name", "replicas"],
        },
    },
    {
        "name": "docker_stack_deploy",
        "description": (
            "Deploy (or redeploy) a Docker stack from its compose file. "
            "Compose file path defaults to /opt/homelab/<stack_name>/docker-compose.yaml."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stack_name": {
                    "type": "string",
                    "description": "The stack name (e.g. jellyfin).",
                },
                "compose_path": {
                    "type": "string",
                    "description": "Optional override path to the compose file.",
                },
            },
            "required": ["stack_name"],
        },
    },
    {
        "name": "run_ansible_playbook",
        "description": (
            "Run an Ansible playbook against the homelab inventory. "
            "Use this to deploy or configure infrastructure — preferred over editing files directly on nodes. "
            "The repo path (e.g. /opt/homelab) is prepended automatically; pass playbook as a relative path "
            "such as 'ansible/deploy-edge.yml' or 'ansible/site.yml'. "
            "Always do a git pull first if you've just pushed changes to the repo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook": {
                    "type": "string",
                    "description": "Playbook path relative to the repo root (e.g. 'ansible/deploy-edge.yml').",
                },
                "limit": {
                    "type": "string",
                    "description": "Optional --limit value (host or group, e.g. 'edge_nodes' or 'dks01.schollar.dev').",
                },
                "extra_vars": {
                    "type": "object",
                    "description": "Optional extra variables passed as -e JSON.",
                },
            },
            "required": ["playbook"],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "Run a shell command locally or on a remote node via SSH. "
            "Because this tool's tier is at your discretion, you MUST supply "
            "agent_proposed_tier and agent_reasoning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run.",
                },
                "node": {
                    "type": "string",
                    "description": "Optional remote node hostname for SSH execution.",
                },
                "agent_proposed_tier": {
                    "type": "integer",
                    "description": "Your proposed safety tier (1, 2, or 3).",
                    "enum": [1, 2, 3],
                },
                "agent_reasoning": {
                    "type": "string",
                    "description": "Your reasoning for the proposed tier.",
                },
            },
            "required": ["command", "agent_proposed_tier", "agent_reasoning"],
        },
    },
    {
        "name": "get_prometheus_alerts",
        "description": "Fetch active alerts from Alertmanager.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "slack_notify",
        "description": "Send a plain informational message to the Slack homelab channel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message text (markdown supported).",
                }
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_pull",
        "description": (
            "Pull the latest changes from the remote into the dev repo at the configured dev_repo_path. "
            "Uses the configured PAT for HTTPS authentication. "
            "If the pull succeeds cleanly, returns the git output. "
            "If there are merge conflicts, returns the list of conflicted files and their conflict markers. "
            "On conflict: read each conflicted file, decide which version is correct "
            "(incoming remote change vs local agent change), write the resolved file using write_file, "
            "then call commit_config_updates with message 'resolve merge conflict' to stage and push. "
            "Never leave a repo in a conflicted state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "commit_config_updates",
        "description": (
            "Sync config changes made in /opt/homelab back to the dev repo at "
            "/home/chris/src/homelab, then commit and push as the chris user. "
            "Use this after editing any file in /opt/homelab so the change is "
            "persisted in git. Always tier 3 — requires explicit approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Git commit message describing what changed.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of repo-relative paths to sync "
                        "(e.g. ['jellyseerr/docker-compose.yaml']). "
                        "If omitted, rsync is used to sync all non-excluded files."
                    ),
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "docker_stack_rollback",
        "description": (
            "Roll back a Docker stack to the image tags that were running before the last deploy. "
            "Uses the snapshot saved automatically by docker_stack_deploy. "
            "Calls `docker service update --image` for each service in the stack."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stack_name": {
                    "type": "string",
                    "description": "The stack name to roll back (e.g. jellyfin).",
                },
            },
            "required": ["stack_name"],
        },
    },
]


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------

class ToolExecutor:
    def __init__(self, config: dict, slack_client: Any) -> None:
        self._config = config
        self._slack = slack_client
        self._docker_socket = config.get("docker", {}).get("socket", "unix:///var/run/docker.sock")
        self._ssh_key = config.get("swarm", {}).get("ssh_key", "/root/.ssh/ansible_ssh_key")
        self._ssh_user = config.get("swarm", {}).get("ssh_user", "root")
        self._repo_path = config.get("ansible", {}).get("repo_path", "/opt/homelab")
        self._inventory = config.get("ansible", {}).get("inventory", "/opt/homelab/ansible/inventory.yml")
        self._dev_repo_path = config.get("ansible", {}).get("dev_repo_path", "/home/chris/src/homelab")
        self._git_token = config.get("ansible", {}).get("git_token", "")
        self._git_author_name = config.get("ansible", {}).get("git_author_name", "Homelab Agent")
        self._git_author_email = config.get("ansible", {}).get("git_author_email", "agent@schollar.dev")
        default_rollback_path = str(Path(__file__).parent.parent / "rollback_state.json")
        rollback_path = config.get("rollback", {}).get("state_path", default_rollback_path)
        self._rollback_state_path = Path(rollback_path)

    def _docker_client(self) -> docker.DockerClient:
        return docker.DockerClient(base_url=self._docker_socket)

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        method = getattr(self, f"_tool_{tool_name}", None)
        if method is None:
            return f"ERROR: Unknown tool '{tool_name}'"
        return await method(tool_input)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _run_subprocess(
        self,
        args: list[str],
        timeout: int = 60,
        cwd: str | None = None,
        stream: bool = False,
    ) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        except FileNotFoundError:
            return f"ERROR: command not found: {args[0]}"
        except OSError as exc:
            return f"ERROR: failed to start process: {exc}"

        if stream:
            lines: list[str] = []
            async def _read() -> None:
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    lines.append(line)
                    _console.print(f"  [dim]│ {line}[/dim]")
            try:
                await asyncio.wait_for(_read(), timeout=timeout)
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                return f"ERROR: command timed out after {timeout}s"
            return "\n".join(lines)

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"ERROR: command timed out after {timeout}s"
        return stdout.decode().strip()

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _tool_docker_service_list(self, inp: dict) -> str:
        loop = asyncio.get_event_loop()
        stack_filter = inp.get("stack")

        def _list() -> str:
            client = self._docker_client()
            filters = {}
            if stack_filter:
                filters["label"] = f"com.docker.stack.namespace={stack_filter}"
            services = client.services.list(filters=filters if filters else None)
            if not services:
                return "No services found."
            lines = []
            for svc in services:
                spec = svc.attrs.get("Spec", {})
                mode = spec.get("Mode", {})
                replicated = mode.get("Replicated", {})
                desired = replicated.get("Replicas", "?")
                image = spec.get("TaskTemplate", {}).get("ContainerSpec", {}).get("Image", "?")
                image = image.split("@")[0]  # strip digest
                lines.append(f"{svc.name}  replicas={desired}  image={image}")
            return "\n".join(lines)

        return await loop.run_in_executor(None, _list)

    async def _tool_docker_service_inspect(self, inp: dict) -> str:
        service_name = inp["service_name"]
        loop = asyncio.get_event_loop()

        def _inspect() -> str:
            client = self._docker_client()
            services = client.services.list(filters={"name": service_name})
            if not services:
                return f"ERROR: Service '{service_name}' not found."
            svc = services[0]
            tasks = svc.tasks()
            task_summary = []
            for t in tasks:
                state = t.get("Status", {}).get("State", "?")
                desired = t.get("DesiredState", "?")
                err = t.get("Status", {}).get("Err", "")
                node_id = t.get("NodeID", "?")
                task_summary.append(
                    f"  task node={node_id} state={state} desired={desired}"
                    + (f" err={err}" if err else "")
                )
            spec = json.dumps(svc.attrs.get("Spec", {}), indent=2)
            return f"=== {service_name} ===\n{spec}\n\nTasks:\n" + "\n".join(task_summary)

        return await loop.run_in_executor(None, _inspect)

    async def _tool_read_logs(self, inp: dict) -> str:
        service_name = inp["service_name"]
        lines = inp.get("lines", 100)
        return await self._run_subprocess([
            "docker", "service", "logs",
            "--no-trunc", f"--tail={lines}",
            service_name,
        ])

    async def _tool_read_file(self, inp: dict) -> str:
        path = inp["path"]
        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            return f"ERROR: {e}"

    async def _tool_write_file(self, inp: dict) -> str:
        path = inp["path"]
        content = inp["content"]
        try:
            with open(path, "w") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {path}."
        except Exception as e:
            return f"ERROR: {e}"

    async def _tool_docker_service_scale(self, inp: dict) -> str:
        service_name = inp["service_name"]
        replicas = inp["replicas"]
        return await self._run_subprocess([
            "docker", "service", "scale",
            f"{service_name}={replicas}",
        ])

    def _load_rollback_state(self) -> dict:
        if self._rollback_state_path.exists():
            return json.loads(self._rollback_state_path.read_text())
        return {}

    def _save_rollback_state(self, state: dict) -> None:
        self._rollback_state_path.write_text(json.dumps(state, indent=2))

    def _snapshot_stack_images(self, stack_name: str) -> dict[str, str]:
        """Return {service_name: image} for all services in the stack."""
        client = self._docker_client()
        services = client.services.list(
            filters={"label": f"com.docker.stack.namespace={stack_name}"}
        )
        snapshot: dict[str, str] = {}
        for svc in services:
            image = (
                svc.attrs.get("Spec", {})
                .get("TaskTemplate", {})
                .get("ContainerSpec", {})
                .get("Image", "")
            )
            snapshot[svc.name] = image.split("@")[0]  # strip digest
        return snapshot

    async def _tool_docker_stack_deploy(self, inp: dict) -> str:
        stack_name = inp["stack_name"]
        compose_path = inp.get(
            "compose_path",
            f"{self._repo_path}/{stack_name}/docker-compose.yaml",
        )

        # Snapshot current images before deploying for rollback
        loop = asyncio.get_event_loop()
        snapshot = await loop.run_in_executor(None, self._snapshot_stack_images, stack_name)
        state = self._load_rollback_state()
        state[stack_name] = {
            "services": snapshot,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._save_rollback_state(state)

        return await self._run_subprocess([
            "docker", "stack", "deploy",
            "--with-registry-auth",
            "-c", compose_path,
            stack_name,
        ])

    async def _tool_git_pull(self, inp: dict) -> str:
        dev = self._dev_repo_path
        token = self._git_token
        git_opts = f'-c safe.directory="{dev}" -c "http.extraHeader=Authorization: token {token}"'
        cmd = f'cd "{dev}" && git {git_opts} pull 2>&1'
        result = await self._run_subprocess(["bash", "-c", cmd], timeout=60, stream=True)

        # Check for conflicts and return conflicted file contents if found
        if "CONFLICT" in result or "Merge conflict" in result:
            conflicts_cmd = f'cd "{dev}" && git {git_opts} diff --name-only --diff-filter=U'
            conflicted = await self._run_subprocess(["bash", "-c", conflicts_cmd], timeout=10)
            files = [f.strip() for f in conflicted.splitlines() if f.strip()]
            details = [result, "\nConflicted files:"]
            for f in files:
                try:
                    content = (Path(dev) / f).read_text()
                    details.append(f"\n--- {f} ---\n{content}")
                except Exception:
                    details.append(f"\n--- {f} --- (could not read)")
            return "\n".join(details)

        return result

    async def _tool_commit_config_updates(self, inp: dict) -> str:
        message = inp["message"]
        paths: list[str] | None = inp.get("paths")
        prod = self._repo_path
        dev = self._dev_repo_path
        token = self._git_token
        author_name = self._git_author_name
        author_email = self._git_author_email

        if paths:
            # Sync specific files only
            copy_cmds: list[str] = []
            for p in paths:
                dest_dir = str(Path(dev) / Path(p).parent)
                copy_cmds += [f'mkdir -p "{dest_dir}"', f'cp "{prod}/{p}" "{dev}/{p}"']
            sync_cmd = " && ".join(copy_cmds)
        else:
            # Full rsync excluding runtime/secret files
            sync_cmd = (
                f"rsync -a --exclude='.git' --exclude='*.log' "
                f"--exclude='rollback_state.json' --exclude='agent_history.json' "
                f"--exclude='action.log' --exclude='agent/config.yaml' "
                f'"{prod}/" "{dev}/"'
            )

        # Commit and push using PAT over HTTPS; no-op if nothing changed
        git_opts = f'-c user.name="{author_name}" -c user.email="{author_email}"'
        git_cmd = (
            f'cd "{dev}" && git add -A && '
            f'git {git_opts} diff --cached --quiet && echo "No changes to commit." || '
            f'(git {git_opts} commit -m "{message}" && '
            f'git -c "http.extraHeader=Authorization: token {token}" push)'
        )

        return await self._run_subprocess(
            ["bash", "-c", f"{sync_cmd} && {git_cmd}"],
            timeout=60,
            stream=True,
        )

    async def _tool_docker_stack_rollback(self, inp: dict) -> str:
        stack_name = inp["stack_name"]
        state = self._load_rollback_state()
        entry = state.get(stack_name)
        if not entry:
            return f"ERROR: No rollback snapshot found for stack '{stack_name}'."

        services: dict[str, str] = entry["services"]
        if not services:
            return f"ERROR: Rollback snapshot for '{stack_name}' is empty."

        timestamp = entry.get("timestamp", "unknown")
        lines = [f"Rolling back {stack_name} to snapshot from {timestamp}:"]
        for svc_name, image in services.items():
            result = await self._run_subprocess([
                "docker", "service", "update",
                "--with-registry-auth",
                "--image", image,
                svc_name,
            ])
            lines.append(f"  {svc_name}: {result.strip()}")

        return "\n".join(lines)

    async def _tool_run_ansible_playbook(self, inp: dict) -> str:
        playbook = inp["playbook"]
        limit = inp.get("limit")
        extra_vars = inp.get("extra_vars")

        cmd = ["ansible-playbook", "-i", self._inventory, playbook]
        if limit:
            cmd += ["--limit", limit]
        if extra_vars:
            cmd += ["-e", json.dumps(extra_vars)]

        return await self._run_subprocess(cmd, timeout=300, cwd=self._repo_path, stream=True)

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

        return await self._run_subprocess(args, timeout=300, stream=True)

    async def _tool_get_prometheus_alerts(self, inp: dict) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("http://alertmanager:9093/api/v2/alerts")
            alerts = resp.json()

        if not alerts:
            return "No active alerts."

        lines = []
        for alert in alerts:
            labels = alert.get("labels", {})
            severity = labels.get("severity", "unknown").upper()
            name = labels.get("alertname", "?")
            summary = alert.get("annotations", {}).get("summary", "")
            lines.append(f"[{severity}] {name}: {summary}")
        return "\n".join(lines)

    async def _tool_slack_notify(self, inp: dict) -> str:
        message = inp["message"]
        await self._slack.notify(message)
        return "Slack notification sent."
