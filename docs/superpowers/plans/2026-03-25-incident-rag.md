# Incident RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace file-based incident reports with pgvector storage and a new `search_incidents` tool for semantic retrieval.

**Architecture:** A new `IncidentRAG` class in `agent/agent/rag.py` wraps psycopg3 + sentence-transformers. `HomelabAgent` instantiates it from config and passes it to `ToolExecutor`, which gains `search_incidents` and a rewritten `write_incident_report` that stores to the DB instead of writing a Markdown file.

**Tech Stack:** `pgvector/pgvector:pg17` Docker image, `psycopg[binary]>=3.1` (psycopg3), `sentence-transformers>=3.0` (`all-MiniLM-L6-v2`, 384-dim), Pydantic v2, pytest + unittest.mock

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `postgres/docker-compose.yaml` | Modify | Switch to pgvector image |
| `agent/pyproject.toml` | Modify | Add psycopg + sentence-transformers deps |
| `agent/agent/rag.py` | **Create** | `IncidentRAG` class — all DB + embedding logic |
| `agent/agent/config_schema.py` | Modify | Add `RagConfig`; remove `ReportsConfig`; add env map entry |
| `agent/config.yaml` | Modify | Add `rag` section; remove `reports` section; add tool tier |
| `agent/agent/tools.py` | Modify | Accept `rag` param; rewrite `write_incident_report`; add `search_incidents`; async `_next_incident_number` |
| `agent/agent/agent.py` | Modify | Instantiate `IncidentRAG`; pass to `ToolExecutor` |
| `agent/cli.py` | Modify | Await `rag.init_schema()` before event loop |
| `agent/tests/test_rag.py` | **Create** | Unit tests for `IncidentRAG` |
| `agent/tests/test_rag_tools.py` | **Create** | Unit tests for the two RAG-backed tools |
| `agent/tests/test_fix3_concurrent_shell_gate.py` | Modify | Remove `ReportsConfig` import/usage (it's deleted) |

---

## Task 1: Infrastructure and Dependencies

**Files:**
- Modify: `postgres/docker-compose.yaml`
- Modify: `agent/pyproject.toml`

No tests needed for these mechanical changes.

- [ ] **Step 1: Update postgres image**

In `postgres/docker-compose.yaml`, change:
```yaml
# Before
image: postgres:17-alpine

# After
image: pgvector/pgvector:pg17
```

- [ ] **Step 2: Add Python dependencies**

In `agent/pyproject.toml`, add two lines to `[project] dependencies`:
```toml
"psycopg[binary]>=3.1",        # async PostgreSQL driver (psycopg3)
"sentence-transformers>=3.0",  # embedding model (includes PyTorch CPU)
```

- [ ] **Step 3: Commit**

```bash
git add postgres/docker-compose.yaml agent/pyproject.toml
git commit -m "feat: add pgvector image and RAG dependencies"
```

---

## Task 2: RagConfig — Config Schema

**Files:**
- Modify: `agent/agent/config_schema.py`
- Modify: `agent/tests/test_fix3_concurrent_shell_gate.py`

`ReportsConfig` is deleted, `RagConfig` replaces it. Existing tests that reference `ReportsConfig` need updating.

- [ ] **Step 1: Write the failing tests**

Create `agent/tests/test_rag_config.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd agent && hatch run pytest tests/test_rag_config.py -v
```
Expected: ImportError or AttributeError — `RagConfig` does not exist yet.

- [ ] **Step 3: Implement config schema changes**

In `agent/agent/config_schema.py`, make these changes:

**3a. Delete the entire `ReportsConfig` class** (currently ~lines 77–79):
```python
# DELETE THIS:
class ReportsConfig(BaseModel):
    path: str
    tags: list[str]
```

**3b. Add `RagConfig` class** — insert it just before `ActionLogConfig`. The DSN is injected via `_env_map` (no pydantic `validation_alias` needed — that pattern isn't used elsewhere in this codebase):
```python
class RagConfig(BaseModel):
    dsn: Optional[str] = Field(default=None)
    database: str = "homelab_agent"
    log_rag_debug: bool = False
```

**3c. In `AgentConfig`**, replace the `reports` field with `rag`:
```python
# Remove this line:
    reports: ReportsConfig

# Add this line (place it after rollback):
    rag: RagConfig = Field(default_factory=RagConfig)
```

**3d. In `YamlConfigSettingsSource.__call__`**, add to `_env_map`:
```python
_env_map = {
    ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
    ("slack", "bot_token"): "SLACK_BOT_TOKEN",
    ("slack", "signing_secret"): "SLACK_SIGNING_SECRET",
    ("ansible", "git_token"): "AGENT_GITHUB_TOKEN",
    ("rag", "dsn"): "AGENT_POSTGRES_DSN",   # ADD THIS LINE
}
```

**3e. Also update `agent/agent/__init__.py`** — check if `ReportsConfig` is in the `__all__` list and remove it if so. Add `RagConfig` to exports.

- [ ] **Step 4: Fix test_fix3_concurrent_shell_gate.py**

That test imports and uses `ReportsConfig` which is being deleted. Make these two targeted edits:

**4a. Update the import block** (around lines 14–31). Before/after:
```python
# BEFORE — remove ReportsConfig, add RagConfig:
from agent.config_schema import (
    ...
    ReportsConfig,       # REMOVE THIS LINE
    ...
)

# AFTER:
from agent.config_schema import (
    ...
    RagConfig,           # ADD THIS LINE
    ...
)
```

**4b. Update `_make_config()`** (around line 71). Before/after:
```python
# BEFORE:
        reports=ReportsConfig.model_construct(path="reports", tags=[]),

# AFTER:
        rag=RagConfig.model_construct(dsn=None, database="homelab_agent", log_rag_debug=False),
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd agent && hatch run pytest tests/test_rag_config.py tests/test_fix3_concurrent_shell_gate.py -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/agent/config_schema.py agent/agent/__init__.py agent/tests/test_rag_config.py agent/tests/test_fix3_concurrent_shell_gate.py
git commit -m "feat: add RagConfig, remove ReportsConfig from config schema"
```

---

## Task 3: IncidentRAG Module

**Files:**
- Create: `agent/agent/rag.py`
- Create: `agent/tests/test_rag.py`

- [ ] **Step 1: Write the failing tests**

Create `agent/tests/test_rag.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd agent && hatch run pytest tests/test_rag.py -v
```
Expected: `ModuleNotFoundError: No module named 'agent.rag'`

- [ ] **Step 3: Implement `agent/agent/rag.py`**

Create `agent/agent/rag.py`:

```python
"""Incident RAG — store and search incidents using pgvector."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer as _ST
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
        self._model: "_ST | None" = None

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
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            self._model = SentenceTransformer("all-MiniLM-L6-v2")

        if self._config.log_rag_debug:
            print(f"[RAG] embed input: {text[:200]!r}")

        vec = self._model.encode(text)
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
            async with conn.cursor() as cur:
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
            async with conn.cursor() as cur:
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
            async with conn.cursor() as cur:
                await cur.execute(_COUNT_SQL)
                row = await cur.fetchone()
        return int(row[0]) if row else 0
```

- [ ] **Step 4: Export `IncidentRAG` from `agent/agent/__init__.py`**

Add to `agent/agent/__init__.py`:
```python
from .rag import IncidentRAG
```
And add `"IncidentRAG"` to `__all__` if that list exists.

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd agent && hatch run pytest tests/test_rag.py -v
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/agent/rag.py agent/agent/__init__.py agent/tests/test_rag.py
git commit -m "feat: add IncidentRAG module with store, search, count"
```

---

## Task 4: Tool Changes — `write_incident_report` and `search_incidents`

**Files:**
- Modify: `agent/agent/tools.py`
- Create: `agent/tests/test_rag_tools.py`

This task rewrites `write_incident_report`, adds `search_incidents`, and makes `_next_incident_number` async. It also updates `ToolExecutor.__init__` to accept `rag`.

- [ ] **Step 1: Write the failing tests**

Create `agent/tests/test_rag_tools.py`:

```python
"""Tests for RAG-backed tools: write_incident_report and search_incidents."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config_schema import (
    ActionLogConfig, AgentConfig, AnsibleConfig, ApprovalListenerConfig,
    DockerConfig, EdgeConfig, HistoryConfig, MonitorConfig, RagConfig,
    RollbackConfig, SafetyConfig, SafeModeResourcesConfig,
    ShellCommandGuardsConfig, SlackConfig, SwarmConfig, AnthropicConfig,
)
from agent.tools import ToolExecutor
from agent.rag import IncidentRAG


def _make_config() -> AgentConfig:
    return AgentConfig.model_construct(
        anthropic=AnthropicConfig.model_construct(api_key=None, model="x", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0),
        slack=SlackConfig.model_construct(bot_token=None, signing_secret=None, channel="#t", veto_window_seconds=300),
        docker=DockerConfig.model_construct(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig.model_construct(nodes=[], ssh_key="/k", ssh_user="root"),
        edge=EdgeConfig.model_construct(cloudflare_tunnel_node="", ssh_key="", ssh_user=""),
        ansible=AnsibleConfig.model_construct(repo_path="/opt/homelab", inventory="/opt/homelab/ansible/inventory.yml", git_token=None, git_author_name="Agent", git_author_email="agent@example.com"),
        monitor=MonitorConfig.model_construct(poll_interval=30),
        safety=SafetyConfig.model_construct(global_safe_mode=False, safe_mode_resources=SafeModeResourcesConfig(), tool_tiers={}, log_agent_tier_reasoning=False, shell_command_guards=ShellCommandGuardsConfig()),
        action_log=ActionLogConfig.model_construct(path="./action.log"),
        approval_listener=ApprovalListenerConfig.model_construct(host="127.0.0.1", port=8765),
        history=HistoryConfig.model_construct(path="./h.json"),
        rollback=RollbackConfig.model_construct(state_path="./r.json"),
        rag=RagConfig.model_construct(dsn="postgresql://u:p@host/postgres", database="homelab_agent", log_rag_debug=False),
    )


def _make_mock_rag(count: int = 0) -> MagicMock:
    rag = MagicMock(spec=IncidentRAG)
    rag.store_incident = AsyncMock()
    rag.search_incidents = AsyncMock(return_value=[])
    rag.count_incidents = AsyncMock(return_value=count)
    return rag


# ---------------------------------------------------------------------------
# ToolExecutor accepts rag parameter
# ---------------------------------------------------------------------------

def test_tool_executor_accepts_rag_none() -> None:
    """ToolExecutor must accept rag=None without error."""
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=None)
    assert executor._rag is None


def test_tool_executor_stores_rag() -> None:
    """ToolExecutor must store the provided rag instance."""
    slack = MagicMock()
    mock_rag = _make_mock_rag()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)
    assert executor._rag is mock_rag


# ---------------------------------------------------------------------------
# write_incident_report — with RAG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_incident_report_calls_store_incident() -> None:
    """write_incident_report must call rag.store_incident with the incident dict."""
    mock_rag = _make_mock_rag(count=3)
    slack = MagicMock()
    slack.notify = AsyncMock()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)

    tool_input = {
        "title": "Traefik crash",
        "tags": ["failure", "docker"],
        "inciting_incident": "Traefik exited unexpectedly.",
        "resolution": "Reverted to previous image.",
        "tools_used": ["docker_service_inspect"],
        "start_time": "",
    }
    result = await executor._tool_write_incident_report(tool_input)

    assert mock_rag.store_incident.called
    stored = mock_rag.store_incident.call_args[0][0]
    assert stored["id"] == "INC-0004"  # count was 3, so 3+1=4
    assert stored["title"] == "Traefik crash"
    assert stored["inciting_incident"] == "Traefik exited unexpectedly."
    assert stored["resolution"] == "Reverted to previous image."
    assert isinstance(stored["date"], datetime)


@pytest.mark.asyncio
async def test_write_incident_report_no_file_written(tmp_path) -> None:
    """write_incident_report must NOT write any Markdown file."""
    mock_rag = _make_mock_rag(count=0)
    slack = MagicMock()
    slack.notify = AsyncMock()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)

    tool_input = {
        "title": "Test incident",
        "tags": ["failure"],
        "inciting_incident": "Something broke.",
        "resolution": "Fixed it.",
        "tools_used": [],
        "start_time": "",
    }
    await executor._tool_write_incident_report(tool_input)

    # No .md files should exist in tmp_path or /opt/homelab/reports
    assert list(tmp_path.glob("*.md")) == []


@pytest.mark.asyncio
async def test_write_incident_report_rag_none_returns_warning() -> None:
    """write_incident_report must return a warning when rag is None."""
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=None)

    tool_input = {
        "title": "Test",
        "tags": [],
        "inciting_incident": "x",
        "resolution": "y",
        "tools_used": [],
        "start_time": "",
    }
    result = await executor._tool_write_incident_report(tool_input)
    assert "RAG" in result or "not configured" in result.lower()


# ---------------------------------------------------------------------------
# search_incidents tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_incidents_returns_formatted_text() -> None:
    mock_rag = _make_mock_rag()
    mock_rag.search_incidents = AsyncMock(return_value=[
        {
            "id": "INC-0001",
            "title": "Traefik down",
            "date": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "tags": ["failure", "docker"],
            "inciting_incident": "Container exited.",
            "resolution": "Reverted image.",
            "similarity": 0.91,
        }
    ])
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)

    result = await executor._tool_search_incidents({"query": "traefik crash"})
    assert "INC-0001" in result
    assert "Traefik down" in result
    assert "0.91" in result


@pytest.mark.asyncio
async def test_search_incidents_rag_none_returns_message() -> None:
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=None)
    result = await executor._tool_search_incidents({"query": "anything"})
    assert "not configured" in result.lower() or "RAG" in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd agent && hatch run pytest tests/test_rag_tools.py -v
```
Expected: failures on missing `rag` parameter and missing `_tool_search_incidents`.

- [ ] **Step 3: Update `ToolExecutor.__init__` in `agent/agent/tools.py`**

Find the `ToolExecutor.__init__` (currently around line 357). Make these changes:

**3a. Add `rag` import at the top of `tools.py`** (with other TYPE_CHECKING imports):
```python
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from .rag import IncidentRAG
```
If `from __future__ import annotations` is not already at the top, add it. Add the IncidentRAG import inside the TYPE_CHECKING block.

**3b. Update the signature and body of `__init__`:**
```python
def __init__(self, config: "AgentConfig", slack_client: Any, rag: "IncidentRAG | None" = None) -> None:
    self._config = config
    self._slack = slack_client
    self._rag = rag
    self._docker_socket = config.docker.socket
    self._ssh_key = config.swarm.ssh_key
    self._ssh_user = config.swarm.ssh_user
    self._repo_path = config.ansible.repo_path
    self._inventory = config.ansible.inventory
    self._git_token = config.ansible.git_token or ""
    self._git_author_name = config.ansible.git_author_name
    self._git_author_email = config.ansible.git_author_email
    self._rollback_state_path = Path(config.rollback.state_path)
    self._action_log_path = Path(config.action_log.path)
    self._secrets: list[str] = [s for s in [self._git_token] if s]
    self._shell_gate = asyncio.Semaphore(1)
```

**Key changes:** added `rag` param + `self._rag = rag`; removed `self._reports_path = ...` (no longer needed since we removed `ReportsConfig`).

- [ ] **Step 4: Replace `_next_incident_number` with an async version**

Find `_next_incident_number` (currently sync, around line 586). Replace the entire method:

```python
async def _next_incident_number(self) -> str:
    """Return the next incident ID as 'INC-XXXX' based on DB count."""
    count = await self._rag.count_incidents()
    return f"INC-{count + 1:04d}"
```

- [ ] **Step 5: Rewrite `_tool_write_incident_report`**

Replace the entire `_tool_write_incident_report` method. The new version keeps action log extraction, Slack notification, and narrative building — but replaces file writing and git commit with `rag.store_incident`:

```python
async def _tool_write_incident_report(self, tool_inp: dict) -> str:
    if self._rag is None:
        return "WARNING: RAG is not configured (AGENT_POSTGRES_DSN not set). Incident not stored."

    inp = tool_inp
    title = inp["title"]
    tags = inp["tags"]
    inciting = inp["inciting_incident"]
    resolution = inp["resolution"]
    tools_used = inp["tools_used"]
    other_tools = inp.get("other_tools", "")
    pitfalls = inp.get("pitfalls", "")
    now_utc = datetime.now(timezone.utc)
    raw_start = inp.get("start_time", "")
    start_time_valid = False
    start_time = raw_start
    try:
        parsed = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if abs((now_utc - parsed).total_seconds()) <= 86400:
            start_time_valid = True
    except (ValueError, AttributeError):
        pass

    inc_id = await self._next_incident_number()

    log_entries = self._slice_action_log(start_time) if start_time_valid else []

    # Rejected plans
    explicit_rejected: list[dict] = inp.get("rejected_plans") or []
    proposed_by_id: dict[str, dict] = {
        e["plan_id"]: e for e in log_entries
        if e.get("event") == "plan_proposed" and "plan_id" in e
    }
    log_rejected = [
        {**proposed_by_id.get(e.get("plan_id", ""), {}), **e}
        for e in log_entries
        if e.get("event") == "plan_cancelled"
    ]
    rejected_plans = explicit_rejected if explicit_rejected else log_rejected

    tags_str = ", ".join(tags)
    tools_str = ", ".join(f"`{t}`" for t in tools_used)

    # Build Slack narrative
    narrative: list[str] = [
        f"# {inc_id}: {title}",
        "",
        "**Inciting Incident**",
        inciting,
        "",
        "**Resolution**",
        resolution,
    ]

    if rejected_plans:
        narrative += ["", "**Rejected Plans**"]
        for i, e in enumerate(rejected_plans, 1):
            if "input" in e:
                plan_inp = e.get("input", {})
                cmd = plan_inp.get("command", "")
                node = plan_inp.get("node", "")
                reason = e.get("reason", "")
                agent_reasoning = plan_inp.get("agent_reasoning", "")
            else:
                cmd = e.get("command", "")
                node = ""
                reason = e.get("reason", "")
                agent_reasoning = e.get("agent_reasoning", "")
            narrative.append(f"{i}. `{cmd}`{f' on `{node}`' if node else ''}")
            if agent_reasoning:
                narrative.append(f"   _Agent reasoning:_ {agent_reasoning}")
            narrative.append(f"   _Rejected:_ {reason}")

    narrative += ["", "**Tools Used**", tools_str]
    if other_tools:
        narrative += ["", "**Other Tools**", other_tools]
    if pitfalls:
        narrative += ["", "**Pitfalls**", pitfalls]

    slack_body = "\n".join(narrative)

    # Store in DB
    await self._rag.store_incident({
        "id": inc_id,
        "title": title,
        "date": now_utc,
        "tags": tags,
        "inciting_incident": inciting,
        "resolution": resolution,
        "tools_used": tools_used,
    })

    await self._slack.notify(slack_body)

    return (
        f"{inc_id} stored in database "
        f"({len(log_entries)} action log entries, tags: {tags_str})."
    )
```

- [ ] **Step 6: Add `search_incidents` to `TOOL_DEFINITIONS`**

Find the `TOOL_DEFINITIONS` list (near the top of the file, ending around line 349). Add the new tool definition before the closing `]`:

```python
    {
        "name": "search_incidents",
        "description": (
            "Search past incident reports for similar problems. "
            "Use this at the start of an incident to find relevant past resolutions "
            "and avoid repeating known pitfalls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Describe the current problem in a sentence or two.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return. Default 5.",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
```

- [ ] **Step 7: Add `_tool_search_incidents` method**

Add this method to `ToolExecutor`, after `_tool_write_incident_report`:

```python
async def _tool_search_incidents(self, tool_inp: dict) -> str:
    if self._rag is None:
        return "RAG is not configured (AGENT_POSTGRES_DSN not set). Cannot search incidents."

    query = tool_inp["query"]
    top_k = int(tool_inp.get("top_k", 5))

    results = await self._rag.search_incidents(query, top_k=top_k)

    if not results:
        return "No past incidents found matching that query."

    lines = [f"Found {len(results)} past incident(s):\n"]
    for r in results:
        date_str = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])
        tags_str = ", ".join(r["tags"])
        lines += [
            f"---",
            f"**{r['id']}**: {r['title']}",
            f"Date: {date_str} | Tags: {tags_str} | Similarity: {r['similarity']:.2f}",
            f"**Inciting Incident:** {r['inciting_incident']}",
            f"**Resolution:** {r['resolution']}",
            "",
        ]
    return "\n".join(lines)
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd agent && hatch run pytest tests/test_rag_tools.py -v
```
Expected: all PASS.

- [ ] **Step 9: Run the full test suite to check for regressions**

```bash
cd agent && hatch run pytest -v
```
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add agent/agent/tools.py agent/tests/test_rag_tools.py
git commit -m "feat: add search_incidents tool, rewrite write_incident_report to use RAG"
```

---

## Task 5: Wiring — HomelabAgent and cli.py

**Files:**
- Modify: `agent/agent/agent.py`
- Modify: `agent/cli.py`

No new tests — the wiring is thin glue code already covered by the tool tests.

**Architectural note:** `ToolExecutor` is instantiated inside `HomelabAgent.__init__` (agent.py:356), not in cli.py. So `IncidentRAG` must also be created in `HomelabAgent.__init__` (sync — just stores config, does nothing heavy). `cli.py` then awaits the async `init_schema()` call before the event loop starts.

- [ ] **Step 1: Update `HomelabAgent.__init__` in `agent/agent/agent.py`**

Find `HomelabAgent.__init__` (around line 340).

**1a. Add import** near other imports at the top of the file:
```python
from .rag import IncidentRAG
```

**1b. Add RAG instantiation** — insert these two lines just before `self._tools = ToolExecutor(...)` (currently line 356):
```python
# RAG — only if DSN is configured. __init__ is sync; init_schema() called from cli.py.
self._rag: IncidentRAG | None = (
    IncidentRAG(config.rag) if config.rag.dsn else None
)
```

**1c. Update the ToolExecutor instantiation** on the very next line to pass rag:
```python
# BEFORE:
self._tools = ToolExecutor(config, self._slack)

# AFTER:
self._tools = ToolExecutor(config, self._slack, rag=self._rag)
```

- [ ] **Step 2: Update `cli.py` to call `init_schema`**

In `cli.py`, find `amain` (around line 428). Insert an `init_schema` call immediately after `agent = HomelabAgent(config)`:

```python
async def amain(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    event_queue: asyncio.Queue = asyncio.Queue()

    agent = HomelabAgent(config)

    # ADDED: initialise RAG schema (creates DB + table if not already present)
    if agent._rag is not None:
        await agent._rag.init_schema()

    log_path = config.action_log.path   # <-- rest of amain is unchanged
    ...
```

Also add the import at the top of `cli.py` if `IncidentRAG` isn't already imported (it won't be — it's used through `agent._rag` so no import needed in cli.py).

- [ ] **Step 3: Verify the existing test suite still passes**

```bash
cd agent && hatch run pytest -v
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add agent/agent/agent.py agent/cli.py
git commit -m "feat: wire IncidentRAG into HomelabAgent and cli startup"
```

---

## Task 6: Config YAML Update

**Files:**
- Modify: `agent/config.yaml`

- [ ] **Step 1: Edit `agent/config.yaml`**

**Remove** the entire `reports` section (lines roughly 113–138 — the block starting `# Incident reports`):
```yaml
# DELETE THESE LINES:
# -----------------------------------------------------------------------
# Incident reports
# -----------------------------------------------------------------------
reports:
  path: "reports"
  tags:
    - failure
    - recovery
    ...
```

**Add** the `rag` section in its place:
```yaml
# -----------------------------------------------------------------------
# Incident RAG
# -----------------------------------------------------------------------
rag:
  database: homelab_agent
  log_rag_debug: false
  # dsn: set via AGENT_POSTGRES_DSN environment variable
  # format: postgresql://postgres:<password>@pg.schollar.dev:5432/postgres
```

**Add** `search_incidents` to `safety.tool_tiers`:
```yaml
    search_incidents:        1       # read-only DB query
    write_incident_report:   1
```

- [ ] **Step 2: Verify the agent config still loads**

```bash
cd agent && hatch run python -c "
from agent.config_schema import load_agent_config
cfg = load_agent_config('config.yaml')
print('rag.database:', cfg.rag.database)
print('rag.dsn:', cfg.rag.dsn)
print('OK')
"
```
Expected output:
```
rag.database: homelab_agent
rag.dsn: None
OK
```

- [ ] **Step 3: Run full test suite one final time**

```bash
cd agent && hatch run pytest -v
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add agent/config.yaml
git commit -m "feat: update config.yaml — add rag section, remove reports, add search_incidents tier"
```

---

## Final Check

After all tasks complete, verify the complete picture:

```bash
# All tests pass
cd agent && hatch run pytest -v

# Config loads cleanly
cd agent && hatch run python -c "from agent.config_schema import load_agent_config; cfg = load_agent_config('config.yaml'); print('rag field:', cfg.rag)"

# IncidentRAG is importable
cd agent && hatch run python -c "from agent.rag import IncidentRAG; print('IncidentRAG OK')"

# search_incidents is in TOOL_DEFINITIONS
cd agent && hatch run python -c "from agent.tools import TOOL_DEFINITIONS; names = [t['name'] for t in TOOL_DEFINITIONS]; assert 'search_incidents' in names; print('search_incidents in TOOL_DEFINITIONS: OK')"
```
