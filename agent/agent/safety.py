from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config_schema import AgentConfig


# Hardcoded default tiers for each tool (used when no config override exists)
_DEFAULT_TIERS: dict[str, int] = {
    "docker_service_list": 1,
    "docker_service_inspect": 1,
    "read_logs": 1,
    "read_file": 1,
    "get_prometheus_alerts": 1,
    "slack_notify": 1,
    "docker_service_scale": 2,
    "docker_stack_deploy": 2,
    "run_ansible_playbook": 2,
    "run_shell": 2,
    "write_file": 3,
}


@dataclass
class ResolvedTier:
    tier: int                    # effective tier after all overrides (1, 2, or 3)
    safe_mode_active: bool       # true if safe mode forced the tier up
    original_tier: int | None    # tier before safe mode override (None if not overridden)
    agent_reasoning: str | None  # set when tool is "agent"-discretion


class SafetyPolicy:
    def __init__(self, config: AgentConfig) -> None:
        self.global_safe_mode: bool = config.safety.global_safe_mode

        safe_resources = config.safety.safe_mode_resources
        self._safe_stacks: list[str] = safe_resources.stacks
        self._safe_services: list[str] = safe_resources.services
        self._safe_nodes: list[str] = safe_resources.nodes

        self.tool_tiers: dict[str, int | str] = dict(config.safety.tool_tiers)
        self.log_agent_tier_reasoning: bool = config.safety.log_agent_tier_reasoning

    def _resource_in_safe_mode(self, target_resource: str | None) -> bool:
        if target_resource is None:
            return False
        for prefix in self._safe_stacks + self._safe_services + self._safe_nodes:
            if target_resource.startswith(prefix):
                return True
        return False

    def _base_tier(self, tool_name: str, agent_proposed_tier: int | None) -> int:
        """Return the raw tier before safe-mode overrides."""
        configured = self.tool_tiers.get(tool_name)

        if configured is not None:
            if configured in (1, 2, 3):
                return int(configured)
            if configured == "agent":
                # Agent discretion — use agent's proposal or fall back to 2
                return agent_proposed_tier if agent_proposed_tier is not None else 2

        return _DEFAULT_TIERS.get(tool_name, 2)

    def resolve_tier(
        self,
        tool_name: str,
        target_resource: str | None = None,
        agent_proposed_tier: int | None = None,
        agent_reasoning: str | None = None,
    ) -> ResolvedTier:
        """Resolve the effective execution tier for a tool call.

        Resolution order (highest priority first):
        1. global_safe_mode → tier 3, log original
        2. target_resource in safe_mode_resources → tier 3, log original
        3. explicit numeric value in tool_tiers config → use it
        4. tool_tiers value is "agent" → use agent_proposed_tier
        5. hardcoded default → use _DEFAULT_TIERS
        """
        original = self._base_tier(tool_name, agent_proposed_tier)

        # Priority 1: global safe mode
        if self.global_safe_mode:
            return ResolvedTier(
                tier=3,
                safe_mode_active=True,
                original_tier=original,
                agent_reasoning=agent_reasoning,
            )

        # Priority 2: per-resource safe mode
        if self._resource_in_safe_mode(target_resource):
            return ResolvedTier(
                tier=3,
                safe_mode_active=True,
                original_tier=original,
                agent_reasoning=agent_reasoning,
            )

        # No override — use original tier
        return ResolvedTier(
            tier=original,
            safe_mode_active=False,
            original_tier=None,
            agent_reasoning=agent_reasoning,
        )
