#!/usr/bin/env python3
"""End-to-end RAG smoke test — requires a live Postgres instance with pgvector.

Usage:
    export AGENT_POSTGRES_DSN="postgresql://postgres:<password>@pg.schollar.dev:5432/postgres"
    python test_rag_e2e.py

Exit codes:
    0  — all checks passed
    1  — one or more checks failed
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from agent.config_schema import load_agent_config
from agent.rag import IncidentRAG

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_TEST_INCIDENTS = [
    {
        "id": "INC-E2E-001",
        "title": "Traefik down after image update",
        "date": datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
        "tags": ["failure", "docker", "traefik"],
        "inciting_incident": "Traefik container exited with code 1 immediately after deploying a new image.",
        "resolution": "Reverted to previous image via docker stack deploy.",
        "tools_used": ["docker_stack_deploy", "docker_service_inspect"],
    },
    {
        "id": "INC-E2E-002",
        "title": "Sonarr database corruption",
        "date": datetime(2026, 3, 20, 8, 0, tzinfo=timezone.utc),
        "tags": ["failure", "sonarr", "database"],
        "inciting_incident": "Sonarr reported a SQLite database corruption error on startup.",
        "resolution": "Restored database from nightly backup. Service recovered.",
        "tools_used": ["read_logs", "read_file"],
    },
]

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [{PASS}] {name}")
    else:
        msg = f"{name}" + (f": {detail}" if detail else "")
        print(f"  [{FAIL}] {msg}")
        failures.append(msg)


async def main() -> None:
    config = load_agent_config("config.yaml")
    rag = IncidentRAG(config.rag)

    # ------------------------------------------------------------------ #
    # 1. Schema bootstrap
    # ------------------------------------------------------------------ #
    print("\n-- Schema bootstrap")
    try:
        await rag.init_schema()
        check("init_schema completed without error", True)
    except Exception as exc:
        check("init_schema completed without error", False, str(exc))
        print("\nCannot proceed — database unreachable.")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 2. Store incidents
    # ------------------------------------------------------------------ #
    print("\n-- Store incidents")
    for inc in _TEST_INCIDENTS:
        try:
            await rag.store_incident(inc)
            check(f"store_incident {inc['id']}", True)
        except Exception as exc:
            check(f"store_incident {inc['id']}", False, str(exc))

    # ------------------------------------------------------------------ #
    # 3. Count
    # ------------------------------------------------------------------ #
    print("\n-- Count")
    count = await rag.count_incidents()
    check("count_incidents >= 2", count >= 2, f"got {count}")

    # ------------------------------------------------------------------ #
    # 4. Semantic search — relevant query
    # ------------------------------------------------------------------ #
    print("\n-- Semantic search")
    results = await rag.search_incidents("traefik container crashed after deploy", top_k=5)
    check("search returns results", len(results) > 0, f"got {len(results)}")

    if results:
        top = results[0]
        check(
            "top result is traefik incident",
            top["id"] == "INC-E2E-001",
            f"got {top['id']} (sim={top['similarity']:.3f})",
        )
        check("similarity is a float in (0, 1]", 0 < top["similarity"] <= 1.0, str(top["similarity"]))
        check("result has expected keys", all(k in top for k in ("id", "title", "date", "tags", "inciting_incident", "resolution", "similarity")))

    # ------------------------------------------------------------------ #
    # 5. Upsert idempotency
    # ------------------------------------------------------------------ #
    print("\n-- Upsert idempotency")
    count_before = await rag.count_incidents()
    await rag.store_incident(_TEST_INCIDENTS[0])  # store same incident again
    count_after = await rag.count_incidents()
    check("re-storing same incident does not increase count", count_before == count_after, f"{count_before} → {count_after}")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    print()
    if failures:
        print(f"\033[31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"\033[32mAll checks passed.\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
