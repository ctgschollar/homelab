"""Incident RAG — store and search incidents using pgvector."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psycopg

try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from agent.config_schema import RagConfig

_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    date              TIMESTAMPTZ NOT NULL,
    tags              TEXT[] NOT NULL DEFAULT '{}',
    inciting_incident TEXT NOT NULL,
    resolution        TEXT NOT NULL,
    tools_used        TEXT[] NOT NULL DEFAULT '{}',
    embedding         vector(384) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS incidents_embedding_idx
    ON incidents USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);
"""

_UPSERT_SQL = """
INSERT INTO incidents
    (id, title, date, tags, inciting_incident, resolution, tools_used, embedding)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET
    title             = EXCLUDED.title,
    date              = EXCLUDED.date,
    tags              = EXCLUDED.tags,
    inciting_incident = EXCLUDED.inciting_incident,
    resolution        = EXCLUDED.resolution,
    tools_used        = EXCLUDED.tools_used,
    embedding         = EXCLUDED.embedding;
"""

_SEARCH_SQL = """
SELECT id, title, date, tags, inciting_incident, resolution,
       1 - (embedding <=> %s::vector) AS similarity
FROM incidents
ORDER BY embedding <=> %s::vector
LIMIT %s;
"""

_COUNT_SQL = "SELECT COUNT(*) FROM incidents;"


class IncidentRAG:
    def __init__(self, config: "RagConfig") -> None:
        self._config = config
        self._model: object | None = None

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def init_schema(self) -> None:
        """Create the homelab_agent database (if needed) and run schema SQL."""
        # Connect to the postgres bootstrap DB to create the target DB
        bootstrap_dsn = re.sub(r"/[^/]*$", "/postgres", self._config.dsn or "")
        target_db = self._config.database

        async with await psycopg.AsyncConnection.connect(
            bootstrap_dsn, autocommit=True
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (target_db,)
                )
                row = await cur.fetchone()
                if not row:
                    # CREATE DATABASE cannot run inside a transaction
                    await cur.execute(f'CREATE DATABASE "{target_db}"')

        # Now connect to the target DB and run schema
        target_dsn = re.sub(r"/[^/]*$", f"/{target_db}", self._config.dsn or "")
        async with await psycopg.AsyncConnection.connect(target_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SCHEMA_SQL)
            await conn.commit()

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        """Return a 384-float embedding. Loads model lazily on first call."""
        if self._model is None:
            self._model = SentenceTransformer("all-MiniLM-L6-v2")

        if self._config.log_rag_debug:
            print(f"[RAG] embed input: {text[:200]!r}")

        vec = self._model.encode(text)  # type: ignore[union-attr]
        result = [float(v) for v in vec]

        if self._config.log_rag_debug:
            print(f"[RAG] embedding: dim={len(result)}, first5={result[:5]}")

        return result

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    async def store_incident(self, incident: dict) -> None:
        """Upsert an incident into the incidents table."""
        embed_text = (
            incident["title"]
            + " "
            + incident["inciting_incident"]
            + " "
            + incident["resolution"]
        )

        if self._config.log_rag_debug:
            print(f"[RAG] store_incident id={incident['id']} title={incident['title']!r}")
            print(f"[RAG] embed_text: {embed_text[:200]!r}")

        embedding = self._embed(embed_text)
        target_dsn = re.sub(r"/[^/]*$", f"/{self._config.database}", self._config.dsn or "")

        async with await psycopg.AsyncConnection.connect(target_dsn) as conn:
            async with await conn.cursor() as cur:
                await cur.execute(
                    _UPSERT_SQL,
                    (
                        incident["id"],
                        incident["title"],
                        incident["date"],
                        incident["tags"],
                        incident["inciting_incident"],
                        incident["resolution"],
                        incident["tools_used"],
                        embedding,
                    ),
                )
            await conn.commit()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_incidents(self, query: str, top_k: int = 5) -> list[dict]:
        """Return top_k incidents most similar to the query string."""
        if self._config.log_rag_debug:
            print(f"[RAG] search_incidents query={query!r} top_k={top_k}")

        embedding = self._embed(query)
        target_dsn = re.sub(r"/[^/]*$", f"/{self._config.database}", self._config.dsn or "")

        async with await psycopg.AsyncConnection.connect(target_dsn) as conn:
            async with await conn.cursor() as cur:
                await cur.execute(_SEARCH_SQL, (embedding, embedding, top_k))
                rows = await cur.fetchall()

        results = []
        for row in rows:
            inc_id, title, date, tags, inciting, resolution, similarity = row
            if self._config.log_rag_debug:
                print(f"[RAG]   {inc_id} {title!r} sim={similarity:.3f} inciting={inciting[:100]!r}")
            results.append({
                "id": inc_id,
                "title": title,
                "date": date,
                "tags": tags,
                "inciting_incident": inciting,
                "resolution": resolution,
                "similarity": similarity,
            })
        return results

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    async def count_incidents(self) -> int:
        """Return the total number of stored incidents."""
        target_dsn = re.sub(r"/[^/]*$", f"/{self._config.database}", self._config.dsn or "")
        async with await psycopg.AsyncConnection.connect(target_dsn) as conn:
            async with await conn.cursor() as cur:
                await cur.execute(_COUNT_SQL)
                row = await cur.fetchone()
        return int(row[0]) if row else 0
