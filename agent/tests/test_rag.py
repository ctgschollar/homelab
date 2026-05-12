"""Tests for IncidentRAG — store, search, count, and embed."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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


def _mock_conn_ctx(mock_cursor: AsyncMock) -> AsyncMock:
    """Return an AsyncMock that behaves as an async context manager for a psycopg connection."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    cursor_ctx = AsyncMock()
    cursor_ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
    cursor_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=cursor_ctx)
    return mock_conn


def _mock_ollama_embed(embedding: list[float]) -> MagicMock:
    """Return a mock ollama.AsyncClient whose embed() returns the given embedding."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.embeddings = [embedding]
    mock_client.embed = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# _embed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_returns_768_floats() -> None:
    rag = _make_rag()
    embedding = [0.1] * 768
    with patch("agent.rag.ollama.AsyncClient", return_value=_mock_ollama_embed(embedding)):
        result = await rag._embed("hello world")
    assert len(result) == 768
    assert all(isinstance(v, float) for v in result)


@pytest.mark.asyncio
async def test_embed_uses_configured_model() -> None:
    rag = _make_rag()
    mock_client = _mock_ollama_embed([0.0] * 768)
    with patch("agent.rag.ollama.AsyncClient", return_value=mock_client):
        await rag._embed("test")
    mock_client.embed.assert_called_once_with(model="nomic-embed-text", input="test")


# ---------------------------------------------------------------------------
# store_incident
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_incident_upserts_row() -> None:
    rag = _make_rag()
    incident = _make_incident()

    mock_cursor = AsyncMock()
    mock_conn = _mock_conn_ctx(mock_cursor)
    mock_ollama = _mock_ollama_embed([0.5] * 768)

    with patch("agent.rag.ollama.AsyncClient", return_value=mock_ollama), \
         patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
        await rag.store_incident(incident)

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

    mock_cursor = AsyncMock()
    mock_conn = _mock_conn_ctx(mock_cursor)
    mock_ollama = _mock_ollama_embed([0.0] * 768)

    with patch("agent.rag.ollama.AsyncClient", return_value=mock_ollama), \
         patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
        await rag.store_incident(incident)

    mock_ollama.embed.assert_called_once_with(model="nomic-embed-text", input=expected_text)


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

    mock_cursor = AsyncMock()
    mock_cursor.fetchall = AsyncMock(return_value=[mock_row])
    mock_conn = _mock_conn_ctx(mock_cursor)
    mock_ollama = _mock_ollama_embed([0.1] * 768)

    with patch("agent.rag.ollama.AsyncClient", return_value=mock_ollama), \
         patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
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

    mock_cursor = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=(7,))
    mock_conn = _mock_conn_ctx(mock_cursor)

    with patch("agent.rag.psycopg.AsyncConnection.connect", return_value=mock_conn):
        count = await rag.count_incidents()

    assert count == 7
