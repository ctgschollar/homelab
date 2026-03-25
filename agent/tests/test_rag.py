"""Tests for IncidentRAG — store, search, count, and embed."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from agent.config_schema import RagConfig
from agent.rag import IncidentRAG


def _make_rag(dsn: str = "postgresql://u:p@host/postgres", log_debug: bool = False) -> IncidentRAG:
    cfg = RagConfig.model_construct(dsn=dsn, database="homelab_agent", log_rag_debug=log_debug)
    return IncidentRAG(cfg)


def _make_incident(inc_id: str = "INC-0001") -> dict:
    return {
        "id": inc_id,
        "title": "Traefik down after deploy",
        "date": datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc),
        "tags": ["failure", "docker"],
        "inciting_incident": "Traefik container exited with code 1.",
        "resolution": "Reverted to previous image.",
        "tools_used": ["docker_stack_deploy", "docker_service_inspect"],
    }


# ---------------------------------------------------------------------------
# _embed
# ---------------------------------------------------------------------------

def test_embed_returns_384_floats() -> None:
    rag = _make_rag()
    with patch("agent.rag.SentenceTransformer") as mock_st:
        mock_model = MagicMock()
        mock_model.encode.return_value = [0.1] * 384
        mock_st.return_value = mock_model
        result = rag._embed("hello world")
    assert len(result) == 384
    assert all(isinstance(v, float) for v in result)


def test_embed_caches_model() -> None:
    rag = _make_rag()
    with patch("agent.rag.SentenceTransformer") as mock_st:
        mock_model = MagicMock()
        mock_model.encode.return_value = [0.0] * 384
        mock_st.return_value = mock_model
        rag._embed("first call")
        rag._embed("second call")
    # SentenceTransformer constructor called exactly once (lazy, cached)
    assert mock_st.call_count == 1


def test_embed_uses_correct_model_name() -> None:
    rag = _make_rag()
    with patch("agent.rag.SentenceTransformer") as mock_st:
        mock_model = MagicMock()
        mock_model.encode.return_value = [0.0] * 384
        mock_st.return_value = mock_model
        rag._embed("test")
    mock_st.assert_called_once_with("all-MiniLM-L6-v2")


# ---------------------------------------------------------------------------
# store_incident
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_incident_upserts_row() -> None:
    rag = _make_rag()
    incident = _make_incident()

    mock_conn = AsyncMock()
    mock_cursor = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("agent.rag.SentenceTransformer") as mock_st, \
         patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
        mock_model = MagicMock()
        mock_model.encode.return_value = [0.5] * 384
        mock_st.return_value = mock_model
        await rag.store_incident(incident)

    # cursor.execute should have been called with an INSERT ... ON CONFLICT statement
    assert mock_cursor.execute.called
    sql_arg = mock_cursor.execute.call_args[0][0]
    assert "ON CONFLICT" in sql_arg
    assert "incidents" in sql_arg


@pytest.mark.asyncio
async def test_store_incident_embeds_correct_text() -> None:
    rag = _make_rag()
    incident = _make_incident()
    expected_text = (
        "Traefik down after deploy "
        "Traefik container exited with code 1. "
        "Reverted to previous image."
    )

    mock_conn = AsyncMock()
    mock_cursor = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("agent.rag.SentenceTransformer") as mock_st, \
         patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
        mock_model = MagicMock()
        mock_model.encode.return_value = [0.0] * 384
        mock_st.return_value = mock_model
        await rag.store_incident(incident)

    mock_model.encode.assert_called_once_with(expected_text)


# ---------------------------------------------------------------------------
# search_incidents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_incidents_returns_results() -> None:
    rag = _make_rag()

    mock_row = (
        "INC-0001",
        "Traefik down",
        datetime(2026, 3, 25, tzinfo=timezone.utc),
        ["failure", "docker"],
        "Container exited with code 1.",
        "Reverted image.",
        0.92,
    )

    mock_conn = AsyncMock()
    mock_cursor = AsyncMock()
    mock_cursor.fetchall = AsyncMock(return_value=[mock_row])
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("agent.rag.SentenceTransformer") as mock_st, \
         patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
        mock_model = MagicMock()
        mock_model.encode.return_value = [0.1] * 384
        mock_st.return_value = mock_model
        results = await rag.search_incidents("traefik crash", top_k=3)

    assert len(results) == 1
    assert results[0]["id"] == "INC-0001"
    assert results[0]["title"] == "Traefik down"
    assert results[0]["similarity"] == 0.92


# ---------------------------------------------------------------------------
# count_incidents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_count_incidents_returns_integer() -> None:
    rag = _make_rag()

    mock_conn = AsyncMock()
    mock_cursor = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=(7,))
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
        count = await rag.count_incidents()

    assert count == 7
