"""E2E: graceful embedding degradation + stale backfill.

Scenario:
  1. embedder is "down" -> process_item saves the item with embedding_stale=true,
     skips dedup, but still summarizes and creates an event (via chat mock)
  2. embedder "recovers" -> backfill_stale_embeddings computes the missing
     vectors and clears the stale flags

Uses the shared mock LLM from tests (mono LLM_ENDPOINT=None approach: we stub
LLMClient methods directly). Run with the same env as test_processor_e2e.py.
"""

import asyncio
import os
import sys

os.environ.update(
    DB_HOST=os.environ.get("TEST_DB_HOST", "localhost"),
    DB_PORT=os.environ.get("TEST_DB_PORT", "5434"),
    DB_NAME=os.environ.get("TEST_DB_NAME", "newsparser"),
    DB_USER=os.environ.get("TEST_DB_USER", "newsparser"),
    DB_PASSWORD=os.environ.get("TEST_DB_PASSWORD", "newsparser"),
    REDIS_HOST=os.environ.get("TEST_REDIS_HOST", "localhost"),
    REDIS_PORT=os.environ.get("TEST_REDIS_PORT", "6380"),
    EMBED_DIM=os.environ.get("TEST_EMBED_DIM", "8"),
    DEDUP_THRESHOLD="0.85",
    LLM_BASE_URL="http://mock",
)

sys.path.insert(0, ".")

from newsparser.config import get_settings  # noqa: E402
from newsparser.db import Database  # noqa: E402
from newsparser.llm import LLMClient  # noqa: E402
from newsparser.processor.main import backfill_stale_embeddings, process_item  # noqa: E402

TEST_LINK = "https://test.local/stale-e2e-1"


class _NoopRedis:
    """cache_event no-op stub: the test passes no redis client."""

    async def hset(self, *a, **k) -> None:
        pass

    async def hgetall(self, *a, **k) -> dict:
        return {}

    async def hdel(self, *a, **k) -> None:
        pass


class FlakyEmbedLLM(LLMClient):
    """LLMClient whose embed() fails until `recover()` is called."""

    def __init__(self, settings):
        # skip the real __init__ (no httpx client needed - everything mocked)
        self.settings = settings

    async def close(self) -> None:  # pragma: no cover - not used
        pass

    async def embed(self, text: str) -> list[float]:
        if not self.embed_ok:
            raise RuntimeError("embedder down (simulated)")
        # deterministic 8-dim vector (matches EMBED_DIM=8 test schema)
        return [float(len(text) % 10), 1.0, 0.5, 0.25, 0.125, 0.0625, 0.03125, 0.015625]

    async def chat(self, prompt: str, max_tokens: int = 2048) -> str:
        return "Тестовая сводка события."

    def recover(self) -> None:
        self.embed_ok = True

    embed_ok = False


async def main() -> None:
    settings = get_settings()
    db = Database(settings.pg_dsn)
    await db.connect()

    llm = FlakyEmbedLLM(settings)

    # clean slate
    old_event = await db.pool.fetchval(
        "SELECT event_id FROM news_items WHERE link = $1", TEST_LINK
    )
    await db.pool.execute("DELETE FROM news_items WHERE link = $1", TEST_LINK)
    if old_event:
        await db.pool.execute("DELETE FROM events WHERE id = $1", old_event)

    item = {
        "source_type": "rss",
        "source": "test.local",
        "title": "Stale degradation e2e",
        "text": "Проверка сохранения новости при недоступном embedder с последующим backfill.",
        "link": TEST_LINK,
    }

    # --- phase 1: embedder down -> saved stale, event exists, no vector ---
    await process_item(db, llm, _NoopRedis(), settings, item)
    row = await db.pool.fetchrow(
        "SELECT embedding IS NULL AS no_emb, embedding_stale, event_id "
        "FROM news_items WHERE link = $1",
        TEST_LINK,
    )
    assert row is not None, "item must be saved even when embedder is down"
    assert row["embedding_stale"] is True, "item must be marked stale"
    assert row["no_emb"] is True, "embedding must be NULL"
    assert row["event_id"] is not None, "event must still be created (chat worked)"
    print("phase1 OK: saved stale with event", row["event_id"])

    # --- phase 2: backfill refuses while embedder is still down ---
    fixed = await backfill_stale_embeddings(db, llm, settings)
    assert fixed == 0, "backfill must no-op while embedder is down"
    print("phase2 OK: backfill skipped while embedder down")

    # --- phase 3: embedder recovers -> backfill fixes the vector ---
    llm.recover()
    fixed = await backfill_stale_embeddings(db, llm, settings)
    assert fixed >= 1, "backfill must process at least our item"
    row2 = await db.pool.fetchrow(
        "SELECT embedding IS NULL AS no_emb, embedding_stale FROM news_items WHERE link = $1",
        TEST_LINK,
    )
    assert row2["embedding_stale"] is False, "stale flag must be cleared"
    assert row2["no_emb"] is False, "embedding must be computed"
    print("phase3 OK: backfilled", fixed, "embedding(s)")

    # --- cleanup ---
    ev = await db.pool.fetchval("SELECT event_id FROM news_items WHERE link = $1", TEST_LINK)
    await db.pool.execute("DELETE FROM news_items WHERE link = $1", TEST_LINK)
    if ev:
        await db.pool.execute("DELETE FROM events WHERE id = $1", ev)
    await db.close()
    print("STALE_E2E_OK")


if __name__ == "__main__":
    asyncio.run(main())
