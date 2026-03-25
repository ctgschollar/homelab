# Incident RAG Design

**Date:** 2026-03-25
**Scope:** `postgres/docker-compose.yaml`, `agent/agent/rag.py` (new), `agent/agent/tools.py`, `agent/agent/config_schema.py`, `agent/config.yaml`, `agent/pyproject.toml`, `agent/cli.py`
**PR:** standalone

---

## Overview

Replace the file-based incident report system with a vector database (pgvector on PostgreSQL). When an incident concludes, the agent stores it as a vector embedding rather than writing a Markdown file. When a new incident starts, the agent can explicitly search past incidents for relevant context using a new `search_incidents` tool.

---

## 1. Infrastructure — pgvector

Change the postgres Docker image in `postgres/docker-compose.yaml`:

```yaml
# Before
image: postgres:17-alpine

# After
image: pgvector/pgvector:pg17
```

No other changes to the stack are needed. The pgvector extension is bundled in the image and enabled via `CREATE EXTENSION IF NOT EXISTS vector` at first startup by the agent.

---

## 2. Database Schema

A single `incidents` table in a new `homelab_agent` database, created automatically on agent startup.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    id                TEXT PRIMARY KEY,        -- e.g. INC-0016
    title             TEXT NOT NULL,
    date              TIMESTAMPTZ NOT NULL,
    tags              TEXT[] NOT NULL DEFAULT '{}',
    inciting_incident TEXT NOT NULL,
    resolution        TEXT NOT NULL,
    tools_used        TEXT[] NOT NULL DEFAULT '{}',
    embedding         vector(384) NOT NULL,    -- all-MiniLM-L6-v2 output
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS incidents_embedding_idx
    ON incidents USING ivfflat (embedding vector_cosine_ops);
```

**Embedding source text:** `title + " " + inciting_incident + " " + resolution` — the full narrative content of the incident.

**Model:** `all-MiniLM-L6-v2` — 384 dimensions, ~90MB, CPU-only, strong semantic search quality.

Schema is created idempotently on every agent startup. No manual migration step required.

---

## 3. `agent/agent/rag.py` — New Module

Single module responsible for all RAG operations.

### Class: `IncidentRAG`

```python
class IncidentRAG:
    def __init__(self, config: RagConfig) -> None
    async def init_schema(self) -> None
    def _embed(self, text: str) -> list[float]
    async def store_incident(self, incident: dict) -> None
    async def search_incidents(self, query: str, top_k: int = 5) -> list[dict]
```

**`__init__`**
- Stores config
- Does NOT load the embedding model or open a DB connection (both are lazy)

**`init_schema`**
- Opens async connection pool via `psycopg` (psycopg3)
- Creates the `homelab_agent` database if it does not exist (connects to `postgres` DB first)
- Runs the schema SQL above
- Called once from `cli.py` during agent startup, before the event loop starts

**`_embed(text)`**
- Loads `SentenceTransformer("all-MiniLM-L6-v2")` on first call (lazy, cached on `self._model`)
- Returns a list of 384 floats
- If `log_rag_debug` is enabled, logs to stdout:
  - Input text (truncated to 200 chars)
  - Embedding dimensions and first 5 values

**`store_incident(incident)`**
- `incident` dict keys: `id`, `title`, `date`, `tags`, `inciting_incident`, `resolution`, `tools_used`
- Generates embedding from `title + " " + inciting_incident + " " + resolution`
- Upserts into `incidents` table (`ON CONFLICT (id) DO UPDATE`)
- If `log_rag_debug` is enabled, logs to stdout:
  - Incident ID and title being stored
  - Embedding text used (truncated)

**`search_incidents(query, top_k)`**
- Embeds the query string
- Runs cosine similarity search: `ORDER BY embedding <=> $1 LIMIT $2`
- Returns list of dicts: `{id, title, date, tags, inciting_incident, resolution, similarity}`
- Similarity is `1 - cosine_distance` (1.0 = identical, 0.0 = unrelated)
- If `log_rag_debug` is enabled, logs to stdout:
  - Query text
  - Each result: ID, title, similarity score, inciting_incident preview (first 100 chars)

### Wiring

`IncidentRAG` is instantiated in `cli.py` during `amain()`:

```python
rag = IncidentRAG(config.rag)
await rag.init_schema()
# then passed to ToolExecutor
executor = ToolExecutor(config, rag=rag)
```

If `config.rag.dsn` is `None` or empty, `IncidentRAG` is not instantiated and `ToolExecutor` receives `rag=None`. Tools that require RAG log a warning and return gracefully rather than crashing.

---

## 4. Tool Changes — `agent/agent/tools.py`

### `write_incident_report` (modified)

**Current behaviour:** generates Markdown file → commits to git → posts to Slack.

**New behaviour:**
1. Read action log entries within time window (unchanged)
2. Call `self._rag.store_incident(incident_dict)` with all fields
3. Post summary to Slack (unchanged)

**Removed:**
- Markdown file generation
- Git commit and push for the report file
- `_next_incident_number()` helper is retained — incident IDs still follow the `INC-XXXX` sequence, but the counter is now derived from the DB (`SELECT COUNT(*) FROM incidents`) rather than scanning the `reports/` directory

**Incident ID generation (updated):**
```python
async def _next_incident_number(self) -> str:
    count = await self._rag.count_incidents()
    return f"INC-{count + 1:04d}"
```

### `search_incidents` (new, tier-1)

**Tool definition:**
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
                "description": "Describe the current problem in a sentence or two."
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return. Default 5.",
                "default": 5
            }
        },
        "required": ["query"]
    }
}
```

**Returns:** formatted text block with top matching incidents, each showing:
- Incident ID, title, date, tags
- Similarity score (0–1)
- Inciting incident and resolution text

---

## 5. Config Changes

### `agent/agent/config_schema.py`

New model:
```python
class RagConfig(BaseModel):
    dsn: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("dsn", "AGENT_POSTGRES_DSN"),
    )
    database: str = "homelab_agent"
    log_rag_debug: bool = False
```

`AgentConfig` gains:
```python
rag: RagConfig = RagConfig()
```

The `reports` section (`ReportsConfig`) is removed from `AgentConfig` — it is no longer needed.

### `agent/config.yaml`

Add:
```yaml
rag:
  database: homelab_agent
  log_rag_debug: false
  # dsn: set via AGENT_POSTGRES_DSN environment variable
  # format: postgresql://postgres:<password>@pg.schollar.dev:5432/postgres
```

Remove the `reports` section entirely.

### `YamlConfigSettingsSource` env var injection

Add to the `_env_map` in `YamlConfigSettingsSource.__call__`:
```python
("rag", "dsn"): "AGENT_POSTGRES_DSN",
```

---

## 6. Dependencies — `agent/pyproject.toml`

Add to `[project] dependencies`:
```toml
"psycopg[binary]>=3.1",        # async PostgreSQL driver (psycopg3)
"sentence-transformers>=3.0",  # embedding model (includes PyTorch CPU)
```

---

## 7. Graceful Degradation

If `AGENT_POSTGRES_DSN` is not set:
- `IncidentRAG` is not instantiated
- `write_incident_report` logs a warning and exits early (no file written, no DB insert)
- `search_incidents` returns a message indicating RAG is not configured
- Agent continues to operate normally for all other tools

---

## 8. Existing Incident Reports

The 15+ existing Markdown files in `reports/` are left as-is. They are not migrated into the database. New incidents are stored in the DB only.

---

## Files Changed

| File | Change |
|------|--------|
| `postgres/docker-compose.yaml` | Change image from `postgres:17-alpine` to `pgvector/pgvector:pg17` |
| `agent/agent/rag.py` | New — `IncidentRAG` class |
| `agent/agent/tools.py` | Modify `write_incident_report`; add `search_incidents`; update `_next_incident_number` |
| `agent/agent/config_schema.py` | Add `RagConfig`; add `rag` field to `AgentConfig`; remove `ReportsConfig` |
| `agent/config.yaml` | Add `rag` section; remove `reports` section |
| `agent/cli.py` | Instantiate `IncidentRAG`, await `init_schema()`, pass to `ToolExecutor` |
| `agent/pyproject.toml` | Add `psycopg[binary]`, `sentence-transformers` |

---

## Out of Scope

- Migration of existing Markdown reports into the DB
- A UI or CLI for browsing incidents (the `search_incidents` tool is the access layer)
- Changes to any other tools
- Changes to Slack notification content
