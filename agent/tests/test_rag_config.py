"""Tests for RagConfig and its integration into AgentConfig."""
import agent.config_schema as schema
from agent.config_schema import RagConfig, AgentConfig


def test_rag_config_defaults() -> None:
    cfg = RagConfig()
    assert cfg.dsn is None
    assert cfg.database == "homelab_agent"
    assert cfg.log_rag_debug is False


def test_rag_config_accepts_dsn() -> None:
    cfg = RagConfig(dsn="postgresql://postgres:pass@localhost:5432/postgres")
    assert cfg.dsn == "postgresql://postgres:pass@localhost:5432/postgres"


def test_agent_config_model_fields_include_rag() -> None:
    """AgentConfig must have a rag field of type RagConfig."""
    fields = AgentConfig.model_fields
    assert "rag" in fields, "AgentConfig must have a 'rag' field"


def test_agent_config_model_fields_exclude_reports() -> None:
    """AgentConfig must NOT have a reports field after removing ReportsConfig."""
    fields = AgentConfig.model_fields
    assert "reports" not in fields, "AgentConfig must not have a 'reports' field"


def test_reports_config_removed_from_module() -> None:
    """ReportsConfig must no longer exist in config_schema module."""
    assert not hasattr(schema, "ReportsConfig"), "ReportsConfig should be removed"
