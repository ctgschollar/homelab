from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import docker

if TYPE_CHECKING:
    from .agent import ActionLogger
    from .config_schema import AgentConfig


class MonitorDaemon:
    def __init__(
        self,
        config: AgentConfig,
        event_queue: asyncio.Queue,
        action_logger: ActionLogger,
    ) -> None:
        self._poll_interval: int = config.monitor.poll_interval
        self._docker_socket: str = config.docker.socket
        self._event_queue = event_queue
        self._logger = action_logger

        # service_name -> datetime when it first went down
        self._down_since: dict[str, datetime] = {}

    def _docker_client(self) -> docker.DockerClient:
        return docker.DockerClient(base_url=self._docker_socket)

    def _poll(self) -> list[dict]:
        """Run a Docker health check synchronously. Returns a list of status dicts."""
        results = []
        client = self._docker_client()
        services = client.services.list()

        for svc in services:
            spec = svc.attrs.get("Spec", {})
            mode = spec.get("Mode", {})

            # Only handle replicated services
            replicated = mode.get("Replicated")
            if replicated is None:
                continue

            desired: int = replicated.get("Replicas", 0)
            if desired == 0:
                continue  # intentionally stopped

            tasks = svc.tasks()
            running = sum(
                1
                for t in tasks
                if t.get("Status", {}).get("State") == "running"
                and t.get("DesiredState") == "running"
            )

            last_error = ""
            if running < desired:
                # Find the most recent error message
                for t in tasks:
                    err = t.get("Status", {}).get("Err", "")
                    if err:
                        last_error = err
                        break

            results.append({
                "name": svc.name,
                "running": running,
                "desired": desired,
                "last_error": last_error,
            })

        return results

    async def _check_once(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(None, self._poll)
        except Exception as e:
            # Docker daemon restart or socket issue — log and continue
            print(f"[monitor] Docker poll error: {e}")
            return

        now = datetime.now(timezone.utc)

        newly_down: list[dict] = []

        for svc in results:
            name = svc["name"]
            running = svc["running"]
            desired = svc["desired"]
            last_error = svc["last_error"]

            if running < desired:
                if name not in self._down_since:
                    # First detection of this outage
                    self._down_since[name] = now
                    await self._logger.log({
                        "event": "monitor_alert",
                        "service": name,
                        "running": running,
                        "desired": desired,
                        "last_error": last_error,
                    })
                    newly_down.append({
                        "service": name,
                        "running": running,
                        "desired": desired,
                        "last_error": last_error,
                    })
            else:
                if name in self._down_since:
                    down_since = self._down_since.pop(name)
                    duration = int((now - down_since).total_seconds())
                    await self._logger.log({
                        "event": "monitor_recovered",
                        "service": name,
                        "down_duration_seconds": duration,
                    })
                    await self._event_queue.put({
                        "source": "monitor",
                        "type": "service_recovered",
                        "data": {
                            "service": name,
                            "down_duration_seconds": duration,
                        },
                        "timestamp": now,
                    })

        if newly_down:
            await self._event_queue.put({
                "source": "monitor",
                "type": "services_down",
                "data": {"services": newly_down},
                "timestamp": now,
            })

    async def run(self) -> None:
        while True:
            await self._check_once()
            await asyncio.sleep(self._poll_interval)
