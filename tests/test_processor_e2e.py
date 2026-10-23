"""End-to-end test of the processor pipeline with a mocked LLM.

Run: .venv312/bin/python tests/test_processor_e2e.py
Requires: news_postgres and news_redis containers up, schema applied.
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
    ALLOWED_CATEGORIES="",
)

sys.path.insert(0, ".")

from newsparser.config import get_settings  # noqa: E402
from newsparser.db import Database  # noqa: E402
from newsparser.processor.main import process_item  # noqa: E402
from newsparser.redis_queue import create_redis  # noqa: E402


class MockLLM:
    """Deterministic embedder: vector from keyword hash. Summaries are fakes."""

    def __init__(self):
        self.calls = []

    async def embed(self, text: str) -> list[float]:
        t = text.lower()
        v = [0.0] * int(os.environ.get("EMBED_DIM", "8"))
        if "цб" in t or "ставк" in t or "rate" in t:
            v[0] = 1.0
        if "нефт" in t or "oil" in t:
            v[1] = 1.0
        v[-1] = 0.1
        return v

    async def summarize_new_event(
        self, title: str, text: str
    ) -> tuple[str, list[str], str]:
        self.calls.append(("new", title))
        return f"Сводка (новое): {title}", ["ЭКОНОМИКА"], "med"

    async def summarize_merge(
        self,
        summary: str,
        title: str,
        text: str,
        tags: list[str] | None = None,
        impact: str = "low",
    ) -> tuple[str, list[str], str]:
        self.calls.append(("merge", title))
        return f"{summary} + деталь от {title}", (tags or []) + ["ТЕСТ"], impact


async def main() -> None:
    settings = get_settings()
    redis = await create_redis(settings)
    db = Database(settings.pg_dsn)
    await db.connect()
    llm = MockLLM()

    # clean state
    await db.pool.execute("TRUNCATE news_items, events, reactions CASCADE")
    await redis.delete(settings.redis_latest_key)

    # 1. new event (central bank rate)
    await process_item(db, llm, redis, settings, {
        "source_type": "rss", "source": "www.ria.ru",
        "title": "ЦБ повысил ставку", "text": "Центробанк повысил ключевую ставку",
        "summary": "", "link": "https://ria.ru/a1", "published_at": None,
    })
    events = await db.latest_events()
    assert len(events) == 1, f"expected 1 event, got {len(events)}"
    assert events[0]["sources_count"] == 1
    eid = events[0]["id"]

    # 2. same event from another source -> merge, no new event
    await process_item(db, llm, redis, settings, {
        "source_type": "telegram", "source": "interfaxonline",
        "title": "Ставка ЦБ повышена", "text": "Совет директоров ЦБ повысил ставку",
        "summary": "", "link": "https://t.me/interfaxonline/1", "published_at": None,
    })
    events = await db.latest_events()
    assert len(events) == 1, f"dedup failed, got {len(events)} events"
    assert events[0]["sources_count"] == 2
    assert any(c == ("merge", "Ставка ЦБ повышена") for c in llm.calls), llm.calls

    # 3. different topic -> new event
    await process_item(db, llm, redis, settings, {
        "source_type": "site", "source": "www.bcs-express.ru",
        "title": "Нефть подорожала", "text": "Нефть марки Brent подорожала",
        "summary": "", "link": "https://bcs-express.ru/n1", "published_at": None,
    })
    events = await db.latest_events()
    assert len(events) == 2, f"expected 2 events, got {len(events)}"

    # 4. duplicate link -> skipped
    await process_item(db, llm, redis, settings, {
        "source_type": "rss", "source": "www.ria.ru",
        "title": "ЦБ повысил ставку", "text": "dup", "summary": "",
        "link": "https://ria.ru/a1", "published_at": None,
    })
    n_items = await db.pool.fetchval("SELECT count(*) FROM news_items")
    assert n_items == 3, f"expected 3 news items, got {n_items}"

    # 5. redis cache has both events
    cached = await redis.hgetall(settings.redis_latest_key)
    assert len(cached) == 2, f"cache has {len(cached)} events"
    cached_sorted = sorted(
        (v for v in cached.values()),
        key=lambda s: __import__("json").loads(s)["id"],
    )
    import json

    ev1 = json.loads(cached_sorted[0])
    assert ev1["sources_count"] == 2
    assert len(ev1["items"]) == 2

    # 6. reactions
    await db.add_reaction(eid, 111, "like")
    await db.add_reaction(eid, 111, "like")  # duplicate ignored
    assert await db.likes_count(eid) == 1

    # 7. vector search sanity
    vec = await llm.embed("ставка ЦБ")
    hit = await db.find_similar_event(vec, 0.85)
    assert hit and hit[0] == eid, hit

    await db.close()
    await redis.aclose()
    print("E2E_OK: dedup, merge, cache, reactions, vector search all passed")


if __name__ == "__main__":
    asyncio.run(main())
