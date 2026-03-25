"""Tests for Fix 6: removal of watched_stacks from MonitorConfig.

watched_stacks was a vestigial field — MonitorDaemon monitors all Docker
services unconditionally and never read the field. Fix 6 removes it from
the schema so config.yaml stays lean and no future code accidentally relies
on a field that has no effect.
"""
from __future__ import annotations

from agent.config_schema import MonitorConfig


def test_monitor_config_accepts_poll_interval_only() -> None:
    """MonitorConfig is valid with just poll_interval — no watched_stacks needed."""
    cfg = MonitorConfig(poll_interval=30)
    assert cfg.poll_interval == 30


def test_monitor_config_has_no_watched_stacks_field() -> None:
    """watched_stacks must not be a field on MonitorConfig."""
    fields = MonitorConfig.model_fields
    assert "watched_stacks" not in fields, (
        "watched_stacks was removed in Fix 6 and must not reappear"
    )


def test_monitor_config_model_construct_without_watched_stacks() -> None:
    """model_construct (used in test helpers) works without watched_stacks."""
    cfg = MonitorConfig.model_construct(poll_interval=60)
    assert cfg.poll_interval == 60
    assert not hasattr(cfg, "watched_stacks")
