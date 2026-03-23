import asyncio
import json
from typing import Any

import docker
import httpx


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
        "description": "Run an Ansible playbook against the homelab inventory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook": {
                    "type": "string",
                    "description": "Path to the playbook, relative to the ansible repo path.",
                },
                "limit": {
                    "type": "string",
                    "description": "Optional --limit value (host or group).",
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
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode() + stderr.decode()
        return out.strip()

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

    async def _tool_docker_stack_deploy(self, inp: dict) -> str:
        stack_name = inp["stack_name"]
        compose_path = inp.get(
            "compose_path",
            f"{self._repo_path}/{stack_name}/docker-compose.yaml",
        )
        return await self._run_subprocess([
            "docker", "stack", "deploy",
            "--with-registry-auth",
            "-c", compose_path,
            stack_name,
        ])

    async def _tool_run_ansible_playbook(self, inp: dict) -> str:
        playbook = inp["playbook"]
        limit = inp.get("limit")
        extra_vars = inp.get("extra_vars")

        cmd = ["ansible-playbook", "-i", self._inventory, playbook]
        if limit:
            cmd += ["--limit", limit]
        if extra_vars:
            cmd += ["-e", json.dumps(extra_vars)]

        return await self._run_subprocess(cmd, timeout=300, cwd=self._repo_path)

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

        return await self._run_subprocess(args)

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
