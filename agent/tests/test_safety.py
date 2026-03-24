"""Tests for SafetyPolicy shell command guards (_check_shell_command)."""
import pytest
from agent.config_schema import (
    AgentConfig,
    SafetyConfig,
    SafeModeResourcesConfig,
    ShellCommandGuardsConfig,
)
from agent.safety import SafetyPolicy


def make_policy(
    extra_tier3: list[str] | None = None,
    extra_tier2: list[str] | None = None,
    global_safe_mode: bool = False,
) -> SafetyPolicy:
    guards = ShellCommandGuardsConfig(
        force_tier3=extra_tier3 or [],
        force_tier2=extra_tier2 or [],
    )
    safety = SafetyConfig(
        global_safe_mode=global_safe_mode,
        safe_mode_resources=SafeModeResourcesConfig(),
        tool_tiers={"run_shell": "agent"},
        log_agent_tier_reasoning=False,
        shell_command_guards=guards,
    )
    config = AgentConfig.model_construct(safety=safety)
    return SafetyPolicy(config)


# --- _check_shell_command unit tests ---

def test_check_rm_rf_returns_3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("rm -rf /tmp/test", agent_proposed_tier=1)
    assert result == 3


def test_check_mkfs_returns_3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("mkfs.ext4 /dev/sdb", agent_proposed_tier=1)
    assert result == 3


def test_check_dd_returns_3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("dd if=/dev/zero of=/dev/sda", agent_proposed_tier=1)
    assert result == 3


def test_check_git_push_returns_min_tier2() -> None:
    policy = make_policy()
    result = policy._check_shell_command("git push origin main", agent_proposed_tier=1)
    assert result == 2


def test_check_git_push_does_not_lower_tier3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("git push origin main", agent_proposed_tier=3)
    assert result == 3


def test_check_df_passthrough() -> None:
    policy = make_policy()
    result = policy._check_shell_command("df -h", agent_proposed_tier=1)
    assert result == 1


def test_check_systemctl_restart_returns_min_tier2() -> None:
    policy = make_policy()
    result = policy._check_shell_command("systemctl restart nginx", agent_proposed_tier=1)
    assert result == 2


def test_check_sed_i_returns_min_tier2() -> None:
    policy = make_policy()
    result = policy._check_shell_command("sed -i 's/foo/bar/' file.conf", agent_proposed_tier=1)
    assert result == 2


# --- _last_guard_match side-channel ---

def test_last_guard_match_set_on_tier3_match() -> None:
    policy = make_policy()
    policy._check_shell_command("rm -rf /tmp/test", agent_proposed_tier=1)
    assert policy._last_guard_match is not None
    list_name, pattern_str = policy._last_guard_match
    assert list_name == "force_tier3"
    assert isinstance(pattern_str, str)


def test_last_guard_match_set_on_tier2_match() -> None:
    policy = make_policy()
    policy._check_shell_command("git push origin main", agent_proposed_tier=1)
    assert policy._last_guard_match is not None
    list_name, pattern_str = policy._last_guard_match
    assert list_name == "force_tier2"
    assert isinstance(pattern_str, str)


def test_last_guard_match_cleared_on_no_match() -> None:
    policy = make_policy()
    # First call sets it
    policy._check_shell_command("rm -rf /tmp", agent_proposed_tier=1)
    assert policy._last_guard_match is not None
    # Second call should clear it
    policy._check_shell_command("df -h", agent_proposed_tier=1)
    assert policy._last_guard_match is None


# --- config-provided extra patterns ---

def test_config_extra_tier3_pattern() -> None:
    policy = make_policy(extra_tier3=[r"my-nuke\.sh"])
    result = policy._check_shell_command("my-nuke.sh", agent_proposed_tier=1)
    assert result == 3


def test_config_extra_tier2_pattern() -> None:
    policy = make_policy(extra_tier2=[r"my-deploy\.sh"])
    result = policy._check_shell_command("my-deploy.sh", agent_proposed_tier=1)
    assert result == 2


def test_hardcoded_defaults_not_replaced_by_config() -> None:
    """Config-extra patterns are additive; hardcoded defaults still apply."""
    policy = make_policy(extra_tier2=[r"my-deploy\.sh"])
    result = policy._check_shell_command("git push origin main", agent_proposed_tier=1)
    assert result == 2
