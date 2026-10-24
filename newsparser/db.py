"""Postgres (asyncpg) storage: news items, events, reactions + pgvector search."""

import json
from datetime import datetime
from typing import Any

import asyncpg
from loguru import logger
from pgvector.asyncpg import register_vector


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool = None  # type: ignore[assignment]  # set in connect()

    async def connect(self) -> None:
        async def _init_conn(conn):
            await register_vector(conn)
            # return json/jsonb as parsed objects, not strings
            await conn.set_type_codec(
                "json",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )
            await conn.set_type_codec(
                "jsonb",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )

        self.pool = await asyncpg.create_pool(
            self.dsn, min_size=1, max_size=5, init=_init_conn
        )
        logger.info("Postgres connected")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # --- news items ---

    async def link_exists(self, link: str) -> bool:
        row = await self.pool.fetchrow("SELECT 1 FROM news_items WHERE link = $1", link)
        return row is not None

    async def insert_news_item(
        self,
        *,
        source_type: str,
        source: str,
        title: str,
        text: str,
        summary: str,
        link: str,
        language: str,
        published_at: datetime | None,
        embedding: list[float] | None,
        event_id: int | None,
        embedding_stale: bool = False,
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO news_items
                (source_type, source, title, text, summary, link, language,
                 published_at, embedding, embedding_stale, event_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            source_type,
            source,
            title,
            text,
            summary,
            link,
            language,
            published_at,
            embedding,
            embedding_stale,
            event_id,
        )
        return row["id"]

    async def stale_embeddings(self, limit: int = 20) -> list[asyncpg.Record]:
        """News items whose embedding failed earlier (backfill candidates)."""
        return await self.pool.fetch(
            """
            SELECT id, title, text, summary
            FROM news_items
            WHERE embedding_stale
            ORDER BY parsed_at
            LIMIT $1
            """,
            limit,
        )

    async def stale_count(self) -> int:
        """Number of items waiting for embedding backfill (for metrics)."""
        return int(
            await self.pool.fetchval("SELECT count(*) FROM news_items WHERE embedding_stale")
            or 0
        )

    async def set_item_embedding(
        self, item_id: int, embedding: list[float] | None, *, stale: bool
    ) -> None:
        """Store a backfilled embedding (or re-mark stale if it failed again)."""
        await self.pool.execute(
            "UPDATE news_items SET embedding = $2, embedding_stale = $3 WHERE id = $1",
            item_id,
            embedding,
            stale,
        )

    # --- dedup / events ---

    async def find_similar_event(
        self, embedding: list[float], threshold: float
    ) -> tuple[int, float] | None:
        """Nearest event by cosine distance; returns (event_id, similarity) or None."""
        row = await self.pool.fetchrow(
            """
            SELECT id, 1 - (embedding <=> $1) AS similarity
            FROM events
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1
            LIMIT 1
            """,
            embedding,
        )
        if row and row["similarity"] >= threshold:
            return row["id"], float(row["similarity"])
        return None

    async def orphan_events(self, limit: int = 20) -> list[asyncpg.Record]:
        """Events with no vector (created while the embedder was down)."""
        return await self.pool.fetch(
            """
            SELECT id, summary
            FROM events
            WHERE embedding IS NULL
            ORDER BY updated_at
            LIMIT $1
            """,
            limit,
        )

    async def similar_events(
        self, embedding: list[float], limit: int = 5, exclude_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Top-N nearest events by cosine, excluding one (e.g. the current)."""
        rows = await self.pool.fetch(
            """
            SELECT id, summary, sources_count, updated_at,
                   1 - (embedding <=> $1) AS similarity
            FROM events
            WHERE embedding IS NOT NULL AND id <> $2
            ORDER BY embedding <=> $1
            LIMIT $3
            """,
            embedding,
            exclude_id or 0,
            limit,
        )
        return [dict(r) for r in rows]

    async def similar_news_items(
        self,
        embedding: list[float],
        limit: int = 5,
        exclude_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Top-N news items nearest to the given vector, other events only."""
        rows = await self.pool.fetch(
            """
            SELECT n.id, n.title, n.source, n.link, n.language, n.event_id,
                   e.summary AS event_summary,
                   1 - (n.embedding <=> $1) AS similarity
            FROM news_items n
            LEFT JOIN events e ON e.id = n.event_id
            WHERE n.embedding IS NOT NULL
              AND (n.event_id IS NULL OR n.event_id <> $2)
            ORDER BY n.embedding <=> $1
            LIMIT $3
            """,
            embedding,
            exclude_event_id or 0,
            limit,
        )
        return [dict(r) for r in rows]

    async def search_events(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search over event summaries (Russian + English configs)."""
        rows = await self.pool.fetch(
            """
            SELECT e.id, e.summary, e.sources_count, e.updated_at, e.tags,
                   ts_rank(e.fts, websearch_to_tsquery('russian', $1)
                              || websearch_to_tsquery('english', $1)) AS rank
            FROM events e
            WHERE e.fts @@ (websearch_to_tsquery('russian', $1)
                               || websearch_to_tsquery('english', $1))
            ORDER BY rank DESC, e.updated_at DESC
            LIMIT $2
            """,
            query,
            limit,
        )
        return [dict(r) for r in rows]

    async def create_event(
        self,
        *,
        summary: str,
        embedding: list[float] | None,
        tags: list[str] | None = None,
        impact: str = "low",
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO events (summary, embedding, tags, impact)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            summary,
            embedding,
            tags or [],
            impact,
        )
        return row["id"]

    async def get_event(self, event_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT * FROM events WHERE id = $1", event_id)

    async def update_event_summary(
        self,
        event_id: int,
        *,
        summary: str,
        embedding: list[float],
        tags: list[str] | None = None,
        impact: str | None = None,
    ) -> None:
        """Update summary + embedding; tags merge, impact takes the max."""
        await self.pool.execute(
            """
            UPDATE events
            SET summary = $2,
                embedding = $3,
                tags = COALESCE(
                    (SELECT array_agg(DISTINCT t)
                     FROM unnest(tags || $4::text[]) AS t),
                    '{}'
                ),
                impact = (SELECT CASE
                              WHEN $5 = 'high' OR impact = 'high' THEN 'high'
                              WHEN $5 = 'med' OR impact = 'med' THEN 'med'
                              ELSE 'low' END),
                sources_count = sources_count + 1,
                updated_at = now()
            WHERE id = $1
            """,
            event_id,
            summary,
            embedding,
            tags or [],
            impact or "low",
        )

    async def set_tg_message(self, event_id: int, message_id: int) -> None:
        await self.pool.execute(
            "UPDATE events SET tg_message_id = $2 WHERE id = $1", event_id, message_id
        )

    async def event_sources(self, event_id: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT title, source, link FROM news_items WHERE event_id = $1
            ORDER BY parsed_at
            """,
            event_id,
        )
        return [dict(r) for r in rows]

    async def events_updated_since(self, since: datetime, limit: int = 20) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            SELECT id, summary, sources_count, first_seen_at, updated_at, tg_message_id,
                   tags, impact
            FROM events
            WHERE updated_at > $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            since,
            limit,
        )

    # --- reactions ---

    async def prune_old_items(self, days: int) -> tuple[int, int]:
        """Delete news items older than `days`; events without items follow.

        Returns (deleted_items, deleted_orphan_events).
        """
        deleted = await self.pool.fetchval(
            """
            WITH old AS (
                DELETE FROM news_items
                WHERE parsed_at < now() - make_interval(days => $1)
                RETURNING 1
            )
            SELECT count(*) FROM old
            """,
            days,
        )
        orphans = await self.pool.fetchval(
            """
            WITH orphan AS (
                DELETE FROM events e
                WHERE e.updated_at < now() - make_interval(days => $1)
                  AND NOT EXISTS (SELECT 1 FROM news_items n WHERE n.event_id = e.id)
                RETURNING 1
            )
            SELECT count(*) FROM orphan
            """,
            days,
        )
        return int(deleted or 0), int(orphans or 0)

    async def add_reaction(self, event_id: int, user_id: int, reaction: str = "like") -> bool:
        """Insert reaction, ignore duplicates (ON CONFLICT DO NOTHING)."""
        status = await self.pool.execute(
            """
            INSERT INTO reactions (event_id, user_id, reaction)
            VALUES ($1, $2, $3)
            ON CONFLICT (event_id, user_id, reaction) DO NOTHING
            """,
            event_id,
            user_id,
            reaction,
        )
        return status.endswith("INSERT 0 1")

    async def likes_count(self, event_id: int) -> int:
        row = await self.pool.fetchrow(
            "SELECT count(*) AS n FROM reactions WHERE event_id = $1 AND reaction = 'like'",
            event_id,
        )
        return row["n"]

    # --- helpers for bootstrap / api ---

    async def latest_events(
        self,
        limit: int = 30,
        tag: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Latest events, optionally filtered by tag and/or news source.

        Source filter matches the source string of any linked news item
        (exact, case-insensitive) — same UX as the tag filter.
        """
        filters = [
            "($2 = '' OR EXISTS "
            "(SELECT 1 FROM unnest(e.tags) t WHERE lower(t) = lower($2)))",
            # $3 = '' disables the source filter; keeping the clause always
            # present keeps the asyncpg prepared-statement arity stable
            "($3 = '' OR EXISTS (SELECT 1 FROM news_items n "
            "WHERE n.event_id = e.id AND lower(n.source) = lower($3)))",
        ]
        where = "WHERE " + " AND ".join(filters)
        rows = await self.pool.fetch(
            f"""
            SELECT e.id, e.summary, e.sources_count, e.first_seen_at, e.updated_at,
                   e.tg_message_id, e.tags, e.impact,
                   COALESCE(json_agg(json_build_object(
                        'title', n.title, 'source', n.source, 'link', n.link
                   )) FILTER (WHERE n.id IS NOT NULL), '[]') AS items
            FROM events e
            LEFT JOIN news_items n ON n.event_id = e.id
            {where}
            GROUP BY e.id
            ORDER BY e.updated_at DESC
            LIMIT $1
            """,
            limit,
            tag or "",
            (source or "").lower(),
        )
        return [dict(r) for r in rows]

    async def all_sources(self) -> list[dict[str, Any]]:
        """Distinct news sources with event counts, most used first."""
        rows = await self.pool.fetch(
            """
            SELECT n.source AS source, count(DISTINCT n.event_id) AS n
            FROM news_items n
            WHERE n.source <> ''
            GROUP BY n.source
            ORDER BY n DESC, source
            """
        )
        return [dict(r) for r in rows]

    async def all_tags(self) -> list[dict[str, Any]]:
        """Distinct tags with usage counts, most used first."""
        rows = await self.pool.fetch(
            """
            SELECT t AS tag, count(*) AS n
            FROM events, unnest(tags) AS t
            GROUP BY t
            ORDER BY n DESC, t
            """
        )
        return [dict(r) for r in rows]


def record_to_event_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        d = dict(row)
    else:  # pragma: no cover
        d = dict(row._mapping)  # type: ignore[attr-defined]
    for key in ("first_seen_at", "updated_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    # asyncpg returns json_agg as str; decode so clients get a real array
    if isinstance(d.get("items"), str):
        d["items"] = json.loads(d["items"])
    return json.loads(json.dumps(d, ensure_ascii=False, default=str))
