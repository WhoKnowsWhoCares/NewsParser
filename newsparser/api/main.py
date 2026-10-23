"""FastAPI: /api/events (from redis cache / postgres) + single-page HTML UI."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import make_asgi_app

from newsparser.api.metrics_history import router as metrics_history_router
from newsparser.config import get_settings
from newsparser.db import Database
from newsparser.log_setup import setup_logging
from newsparser.redis_queue import create_redis

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "api_static"


async def _periodic_cache_sync() -> None:
    """Fallback: sync postgres events into redis cache every 60s."""
    settings = get_settings()
    sync_db = Database(settings.pg_dsn)
    await sync_db.connect()
    try:
        while True:
            await asyncio.sleep(60)
            try:
                from loguru import logger

                events = await sync_db.latest_events(limit=settings.redis_latest_max)
                from newsparser.redis_queue import cache_event

                for row in events:
                    await cache_event(redis_client, settings, dict(row))
            except Exception as e:
                from loguru import logger

                logger.error("Cache sync failed: {}", e)
    finally:
        await sync_db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, redis_client
    settings = get_settings()
    setup_logging()
    db = Database(settings.pg_dsn)
    await db.connect()
    redis_client = await create_redis(settings)
    sync_task = asyncio.create_task(_periodic_cache_sync())
    yield
    sync_task.cancel()
    await db.close()
    await redis_client.aclose()


app = FastAPI(title="NewsParser API", lifespan=lifespan)
# Prometheus exposition: /metrics serves all app metrics (prometheus_client ASGI)
app.mount("/metrics", make_asgi_app())
# dashboard time series for the web-page charts
app.include_router(metrics_history_router)
db: Database = None  # type: ignore[assignment]
redis_client = None


@app.get("/healthz")
async def healthz():
    """Liveness/readiness: DB and redis reachability."""
    checks = {"api": "ok", "postgres": "fail", "redis": "fail"}
    status = 200
    try:
        await db.pool.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception:
        status = 503
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception:
        status = 503
    return JSONResponse(content=checks, status_code=status)


MAX_PAGE_EVENTS = 50  # hard cap on events per page regardless of the query param


@app.get("/api/events")
async def events(limit: int = 50, tag: str = "", source: str = ""):
    """Latest events: try redis cache first, fall back to postgres.

    tag: optional exact tag filter (case-insensitive). source: optional
    exact news-source filter (same UX). Cache path serves only unfiltered
    requests; filtered ones go straight to postgres.
    """
    settings = get_settings()
    limit = max(1, min(limit, MAX_PAGE_EVENTS, settings.redis_latest_max))
    if not tag and not source:
        try:
            raw = await redis_client.hgetall(settings.redis_latest_key)
            items = [json.loads(v) for v in raw.values()]
            items.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
            if items:
                return {"events": items[:limit], "source": "cache"}
        except Exception:
            pass
    rows = await db.latest_events(limit=limit, tag=tag or None, source=source or None)
    return {"events": rows, "source": "db"}


@app.get("/api/tags")
async def tags():
    """All tags with usage counts (for the filter chips)."""
    return {"tags": await db.all_tags()}


@app.get("/api/events/{event_id}")
async def event_details(event_id: int):
    row = await db.pool.fetchrow(
        """
        SELECT e.id, e.summary, e.sources_count, e.first_seen_at, e.updated_at,
               e.tags,
               COALESCE(json_agg(json_build_object(
                    'title', n.title, 'source', n.source, 'link', n.link
               )) FILTER (WHERE n.id IS NOT NULL), '[]') AS items,
               (SELECT count(*) FROM reactions r
                 WHERE r.event_id = e.id AND r.reaction = 'like') AS likes
        FROM events e
        LEFT JOIN news_items n ON n.event_id = e.id
        WHERE e.id = $1
        GROUP BY e.id
        """,
        event_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    result = dict(row)
    # asyncpg returns json_agg as str; decode so the client gets a real array
    if isinstance(result.get("items"), str):
        result["items"] = json.loads(result["items"])
    return result


@app.get("/api/events/{event_id}/similar")
async def similar_events(event_id: int, limit: int = 5, with_news: bool = True):
    """Similar events + (optionally) similar news items from other events.

    Vector search over pgvector; the current event's own news items are
    excluded so the section shows genuinely other coverage.
    """
    limit = max(1, min(limit, 10))
    event = await db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event["embedding"] is None:
        return {"events": [], "news": []}
    vec = event["embedding"]
    vec = vec.to_list() if hasattr(vec, "to_list") else list(vec)
    similar = await db.similar_events(vec, limit=limit, exclude_id=event_id)
    news = []
    if with_news:
        news = await db.similar_news_items(
            vec, limit=limit, exclude_event_id=event_id
        )
    return {"events": similar, "news": news}


@app.get("/api/search")
async def search(q: str, limit: int = 20):
    """Full-text search over event summaries (Russian and English)."""
    q = q.strip()
    if not q:
        return {"events": []}
    limit = max(1, min(limit, 50))
    return {"events": await db.search_events(q, limit=limit)}


@app.get("/api/sources")
async def sources():
    """News sources being collected, with per-source item counts."""
    rows = await db.pool.fetch(
        """
        SELECT source_type, source, count(*) AS items, max(parsed_at) AS last_parsed
        FROM news_items
        GROUP BY source_type, source
        ORDER BY source_type, source
        """
    )
    configured: list[dict] = [
        {
            "source_type": "site",
            "source": "www.bcs-express.ru",
            "url": "https://bcs-express.ru",
        },
    ]
    configured += [
        {"source_type": "telegram", "source": c, "url": f"https://t.me/{c}"}
        for c in get_settings().channels
    ]
    stats = {(r["source_type"], r["source"]): dict(r) for r in rows}
    # event counts per source (for the UI source filter chips)
    event_counts = {r["source"]: r["n"] for r in await db.all_sources()}
    result = []
    for src in configured:
        s = stats.get((src["source_type"], src["source"]))
        result.append({
            **src,
            "items": s["items"] if s else 0,
            "last_parsed": s["last_parsed"].isoformat() if s else None,
            "events": event_counts.get(src["source"], 0),
        })
    return {"sources": result}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="index.html not found")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
