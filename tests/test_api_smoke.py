"""API smoke test: /api/events and / — requires postgres/redis and some events.

Run: .venv312/bin/python tests/test_api_smoke.py
"""

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
)

sys.path.insert(0, ".")

import asyncio  # noqa: E402

from newsparser.api.main import app  # noqa: E402


async def seed() -> None:
    from newsparser.config import get_settings
    from newsparser.db import Database
    from newsparser.processor.main import process_item
    from newsparser.redis_queue import create_redis

    class Embed:
        async def embed(self, text):
            return [1.0, 0.1, 0, 0, 0, 0, 0, 0.1]

        async def summarize_new_event(self, title, text):
            return f"Сводка: {title}"

        async def summarize_merge(self, summary, title, text):
            return f"{summary} (обновлено)"

    settings = get_settings()
    db = Database(settings.pg_dsn)
    await db.connect()
    redis = await create_redis(settings)
    llm = Embed()
    await db.pool.execute("TRUNCATE news_items, events, reactions CASCADE")
    await redis.delete(settings.redis_latest_key)
    await process_item(db, llm, redis, settings, {
        "source_type": "rss", "source": "www.ria.ru",
        "title": "Тестовая новость", "text": "Текст тестовой новости про ставку ЦБ",
        "summary": "", "link": "https://ria.ru/test1", "published_at": None,
    })
    await db.close()
    await redis.aclose()


asyncio.run(seed())


async def check() -> None:
    async with app.router.lifespan_context(app):
        from newsparser.api.main import event_details, events, index

        r = await events(limit=30)
        assert r["events"], "no events returned"
        assert r["events"][0]["summary"].startswith("Сводка:"), r
        print("API /api/events OK:", r["source"], "-", len(r["events"]), "events")

        detail = await event_details(r["events"][0]["id"])
        assert detail["likes"] == 0
        print("API /api/events/{id} OK:", detail["summary"][:50])

        html = await index()
        assert "Новости" in html.body.decode()
        print("API / OK: html page served,", len(html.body), "bytes")


asyncio.run(check())
print("API_SMOKE_OK")
