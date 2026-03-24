"""Pydantic v2 config schema for the homelab agent."""
from __future__ import annotations

import os
import warnings
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic.fields import FieldInfo

TierValue = Literal[1, 2, 3, "agent"]


class AnthropicConfig(BaseModel):
    api_key: Optional[str] = Field(default=None)
    model: str
    input_cost_per_mtok: float
    output_cost_per_mtok: float


class SlackConfig(BaseModel):
    bot_token: Optional[str] = Field(default=None)
    signing_secret: Optional[str] = Field(default=None)
    channel: str
    veto_window_seconds: int = Field(gt=0, default=300)


class DockerConfig(BaseModel):
    socket: str


class SwarmConfig(BaseModel):
    nodes: list[str]
    ssh_key: str
    ssh_user: str


class EdgeConfig(BaseModel):
    cloudflare_tunnel_node: str = ""
    ssh_key: str = ""
    ssh_user: str = ""


class AnsibleConfig(BaseModel):
    repo_path: str
    inventory: str
    git_token: Optional[str] = Field(default=None)
    git_author_name: str
    git_author_email: str


class MonitorConfig(BaseModel):
    poll_interval: int
    watched_stacks: list[str] = []


class SafeModeResourcesConfig(BaseModel):
    stacks: list[str] = []
    services: list[str] = []
    nodes: list[str] = []


class ShellCommandGuardsConfig(BaseModel):
    force_tier3: list[str] = []
    force_tier2: list[str] = []


class SafetyConfig(BaseModel):
    global_safe_mode: bool
    safe_mode_resources: SafeModeResourcesConfig
    tool_tiers: dict[str, TierValue]
    log_agent_tier_reasoning: bool
    shell_command_guards: ShellCommandGuardsConfig = Field(default_factory=ShellCommandGuardsConfig)


class ReportsConfig(BaseModel):
    path: str
    tags: list[str]


class ActionLogConfig(BaseModel):
    path: str


class ApprovalListenerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(ge=1024, le=65535, default=8765)


class HistoryConfig(BaseModel):
    path: str = "./agent_history.json"


class RollbackConfig(BaseModel):
    state_path: str = "./rollback_state.json"


class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type, yaml_path: str) -> None:
        super().__init__(settings_cls)
        self._path = yaml_path

    def get_field_value(self, field: FieldInfo, field_name: str) -> None:
        return None

    def field_is_complex(self, field: FieldInfo) -> bool:
        return True

    def __call__(self) -> dict:
        with open(self._path) as f:
            data = yaml.safe_load(f) or {}
        _env_map = {
            ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
            ("slack", "bot_token"): "SLACK_BOT_TOKEN",
            ("slack", "signing_secret"): "SLACK_SIGNING_SECRET",
            ("ansible", "git_token"): "AGENT_GITHUB_TOKEN",
        }
        for (section, field), env_var in _env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                data.setdefault(section, {})[field] = val
        return data


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True)

    anthropic: AnthropicConfig
    slack: SlackConfig
    docker: DockerConfig
    swarm: SwarmConfig
    edge: EdgeConfig = EdgeConfig()
    ansible: AnsibleConfig
    monitor: MonitorConfig
    safety: SafetyConfig
    reports: ReportsConfig
    action_log: ActionLogConfig
    approval_listener: ApprovalListenerConfig = ApprovalListenerConfig()
    history: HistoryConfig = HistoryConfig()
    rollback: RollbackConfig = RollbackConfig()

    @model_validator(mode="after")
    def _warn_missing_signing_secret(self) -> "AgentConfig":
        if not self.slack.signing_secret:
            warnings.warn(
                "slack.signing_secret is not set — approval listener will be "
                "restricted to localhost"
            )
        return self


def load_agent_config(yaml_path: str) -> AgentConfig:
    class _Config(AgentConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type,
            **kwargs: object,
        ) -> tuple:
            return (
                YamlConfigSettingsSource(settings_cls, yaml_path),
                kwargs["init_settings"],
            )
    return _Config()
