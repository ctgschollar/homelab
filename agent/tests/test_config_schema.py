"""Tests for ShellCommandGuardsConfig and its integration into SafetyConfig."""
import pytest
from agent.config_schema import (
    SafetyConfig,
    SafeModeResourcesConfig,
    ShellCommandGuardsConfig,
)


def make_safety(**kwargs) -> SafetyConfig:
    defaults = dict(
        global_safe_mode=False,
        safe_mode_resources=SafeModeResourcesConfig(),
        tool_tiers={"run_shell": "agent"},
        log_agent_tier_reasoning=False,
    )
    defaults.update(kwargs)
    return SafetyConfig(**defaults)


def test_shell_command_guards_defaults_to_empty() -> None:
    safety = make_safety()
    assert safety.shell_command_guards.force_tier3 == []
    assert safety.shell_command_guards.force_tier2 == []


def test_shell_command_guards_accepts_patterns() -> None:
    guards = ShellCommandGuardsConfig(
        force_tier3=[r"my-nuke\.sh"],
        force_tier2=[r"my-deploy\.sh"],
    )
    safety = make_safety(shell_command_guards=guards)
    assert safety.shell_command_guards.force_tier3 == [r"my-nuke\.sh"]
    assert safety.shell_command_guards.force_tier2 == [r"my-deploy\.sh"]


def test_shell_command_guards_independent_instances() -> None:
    """Default factory must not share a mutable instance across SafetyConfig instances."""
    s1 = make_safety()
    s2 = make_safety()
    assert s1.shell_command_guards is not s2.shell_command_guards
