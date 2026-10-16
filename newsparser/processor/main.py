"""Processor: raw news from redis stream -> embed -> dedup vs pgvector -> LLM summary.

Pipeline per news item:
  1. skip if link already stored
  2. embed (title + text)
  3. find nearest event by cosine similarity
     - similarity >= threshold -> same event: merge summary via LLM, update event
     - otherwise -> new event: summarize via LLM, create event
  4. store news item with event_id
  5. refresh event in redis latest-events cache
"""

import asyncio
import json
import time
from datetime import datetime, timezone

from loguru import logger

from newsparser.config import Settings, get_settings
from newsparser.db import Database, record_to_event_dict
from newsparser.llm import LLMClient
from newsparser.log_setup import LivenessFile, setup_logging
from newsparser.metrics import (
    BACKFILLED,
    DEDUP_MATCHES,
    ITEMS_FAILED,
    ITEMS_PROCESSED,
    QUEUE_LENGTH,
    STALE_EMBEDDINGS,
    start_metrics_server,
)
from newsparser.redis_queue import (
    ack_news,
    cache_event,
    create_redis,
    ensure_stream_group,
    read_news,
    trim_stream,
)

STREAM_CONSUMER_GROUP = "processors"
CONSUMER_NAME = "processor-1"


def _allowed_categories(settings: Settings) -> set[str]:
    """Parse ALLOWED_CATEGORIES (comma-separated) into a lowercase set."""
    return {c.strip().lower() for c in settings.allowed_categories.split(",") if c.strip()}


async def process_item(
    db: Database,
    llm: LLMClient,
    redis_client,
    settings: Settings,
    item: dict,
) -> None:
    link = item.get("link", "")
    if not link:
        logger.warning("Item without link, skipped: {}", item.get("title", "")[:80])
        return
    if await db.link_exists(link):
        logger.debug("Duplicate link, skipped: {}", link)
        return

    title = item.get("title", "")
    text = item.get("text", "") or item.get("summary", "")
    if not text:
        logger.warning("Item without text, skipped: {}", link)
        ITEMS_PROCESSED.labels("no_text").inc()
        return

    # Embedding may fail (local embedder down): degrade gracefully — save the
    # item marked stale, skip dedup, and backfill the vector later.
    try:
        embedding = await llm.embed(f"{title}\n{text[:4000]}")
        embedding_stale = False
    except Exception as e:  # noqa: BLE001 - any embedder failure
        logger.warning("Embed failed for {} ({}), saving with embedding_stale=true", link, e)
        embedding = None
        embedding_stale = True

    allowed = _allowed_categories(settings)

    def _category_allowed(tags: list[str]) -> bool:
        """Empty whitelist = keep all; otherwise tags[0] must be in it."""
        if not allowed:
            return True
        return bool(tags) and tags[0].lower() in allowed

    if embedding is not None:
        similar = await db.find_similar_event(embedding, settings.dedup_threshold)
        if similar:
            event_id, similarity = similar
            DEDUP_MATCHES.inc()
            logger.info("Matched event {} (similarity {:.3f}) for {}", event_id, similarity, link)
            event = await db.get_event(event_id)
            merged, tags = await llm.summarize_merge(
                event["summary"], title, text, tags=list(event["tags"] or [])
            )
            new_embedding = await llm.embed(merged)
            await db.update_event_summary(
                event_id, summary=merged, embedding=new_embedding, tags=tags
            )
        else:
            summary, tags = await llm.summarize_new_event(title, text)
            if not _category_allowed(tags):
                logger.info("Category {} filtered out: {}", tags[:1], link)
                ITEMS_PROCESSED.labels("filtered").inc()
                # save the item without an event so dedup-by-link still works
                embedding = None
                event_id = None
                published_raw = item.get("published_at")
                published_at = None
                if published_raw:
                    try:
                        published_at = datetime.fromisoformat(published_raw)
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                await db.insert_news_item(
                    source_type=item.get("source_type", "unknown"),
                    source=item.get("source", ""),
                    title=title,
                    text=text,
                    summary=item.get("summary", ""),
                    link=link,
                    language=item.get("language", "ru"),
                    published_at=published_at,
                    embedding=None,
                    embedding_stale=False,
                    event_id=None,
                )
                return
            merged_embedding = await llm.embed(summary)
            event_id = await db.create_event(
                summary=summary, embedding=merged_embedding, tags=tags
            )
            logger.info("New event {} created for {} [tags: {}]", event_id, link, tags)
        ITEMS_PROCESSED.labels("ok").inc()
    else:
        # no vector -> cannot dedup; still summarize so the event exists
        summary, tags = await llm.summarize_new_event(title, text)
        if not _category_allowed(tags):
            logger.info("Category {} filtered out: {}", tags[:1], link)
            ITEMS_PROCESSED.labels("filtered").inc()
            event_id = None
        else:
            try:
                merged_embedding = await llm.embed(summary)
                event_id = await db.create_event(
                    summary=summary, embedding=merged_embedding, tags=tags
                )
            except Exception:  # noqa: BLE001 - event without vector, embed later too
                event_id = await db.create_event(summary=summary, embedding=None, tags=tags)
            logger.info(
                "New event {} created for {} (no dedup: embedding failed) [tags: {}]",
                event_id, link, tags,
            )
            ITEMS_PROCESSED.labels("stale_saved").inc()

    published_raw = item.get("published_at")
    published_at = None
    if published_raw:
        try:
            published_at = datetime.fromisoformat(published_raw)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    await db.insert_news_item(
        source_type=item.get("source_type", "unknown"),
        source=item.get("source", ""),
        title=title,
        text=text,
        summary=item.get("summary", ""),
        link=link,
        language=item.get("language", "ru"),
        published_at=published_at,
        embedding=embedding,
        embedding_stale=embedding_stale,
        event_id=event_id,
    )

    # refresh this event's entry in the redis latest-events cache (web page)
    event_row = await db.get_event(event_id)
    sources = await db.event_sources(event_id)
    await cache_event(redis_client, settings, record_to_event_dict(event_row) | {"items": sources})


async def backfill_stale_embeddings(
    db: Database,
    llm: LLMClient,
    settings: Settings,
    limit: int = 20,
) -> int:
    """Re-embed items whose embedding failed earlier. Returns fixed count.

    Called from the main loop only while the embedder is healthy: the probe
    is a cheap embed — if it fails, the embedder is
    still down and backfill is skipped until the next iteration.
    """
    rows = await db.stale_embeddings(limit)
    if not rows:
        return 0

    # probe: one cheap embed; if it fails the embedder is still down
    try:
        await llm.embed("probe")
    except Exception:  # noqa: BLE001 - embedder still unavailable
        return 0

    fixed = 0
    for row in rows:
        text = row["text"] or row["summary"]
        if not text:
            # nothing to embed: clear the flag, otherwise it loops forever
            await db.set_item_embedding(row["id"], None, stale=False)
            continue
        try:
            emb = await llm.embed(f"{row['title']}\n{text[:4000]}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Backfill embed failed for item {}: {}", row["id"], e)
            break  # embedder died mid-backfill; retry next iteration
        await db.set_item_embedding(row["id"], emb, stale=False)
        BACKFILLED.inc()
        fixed += 1
    if fixed:
        logger.info("Backfilled embeddings for {} items", fixed)
    return fixed


async def merge_orphan_events(
    db: Database,
    llm: LLMClient,
    settings: Settings,
    limit: int = 10,
) -> int:
    """Give vectors to events created while the embedder was down, then try
    to merge each into its nearest existing event (dedup backfill).

    Returns the number of orphans processed. Skipped while the embedder is
    unhealthy (probe fails).
    """
    rows = await db.orphan_events(limit)
    if not rows:
        return 0

    try:
        await llm.embed("probe")
    except Exception:  # noqa: BLE001 - embedder still unavailable
        return 0

    processed = 0
    for row in rows:
        if not row["summary"]:
            continue
        try:
            emb = await llm.embed(row["summary"][:4000])
        except Exception as e:  # noqa: BLE001
            logger.warning("Orphan embed failed for event {}: {}", row["id"], e)
            break
        # does an older event about the same thing exist?
        similar = await db.find_similar_event(emb, settings.dedup_threshold)
        if similar and similar[0] != row["id"]:
            keep_id, similarity = similar
            keep = await db.get_event(keep_id)
            if keep is None:
                continue
            # merge orphan summary into the surviving event: update first,
            # then re-point news and drop the orphan
            merged, tags = await llm.summarize_merge(
                keep["summary"], row["summary"][:200], row["summary"],
                tags=list(keep["tags"] or []),
            )
            merged_embedding = await llm.embed(merged)
            await db.update_event_summary(
                keep_id, summary=merged, embedding=merged_embedding, tags=tags
            )
            await db.pool.execute(
                "UPDATE news_items SET event_id = $2 WHERE event_id = $1",
                row["id"], keep_id,
            )
            await db.pool.execute("DELETE FROM events WHERE id = $1", row["id"])
            logger.info(
                "Orphan event {} merged into {} (similarity {:.3f})",
                row["id"], keep_id, similarity,
            )
        else:
            await db.pool.execute(
                "UPDATE events SET embedding = $2 WHERE id = $1", row["id"], emb
            )
            logger.info("Orphan event {} got its vector", row["id"])
        processed += 1
    if processed:
        logger.info("Orphan events processed: {}", processed)
    return processed


async def main() -> None:
    settings = get_settings()
    setup_logging()
    redis_client = await create_redis(settings)
    await ensure_stream_group(redis_client, settings.redis_stream)

    db = Database(settings.pg_dsn)
    await db.connect()
    llm = LLMClient(settings)

    logger.info(
        "Processor started (stream={}, threshold={})",
        settings.redis_stream,
        settings.dedup_threshold,
    )

    liveness = LivenessFile("/tmp/liveness_processor", max_age_hint=180.0)
    liveness.start(interval=20.0)
    start_metrics_server()

    try:
        last_prune = time.monotonic()
        while True:
            # embedder healthy? then also re-embed previously failed items
            await backfill_stale_embeddings(db, llm, settings)
            # and give vectors to / merge events created during the outage
            await merge_orphan_events(db, llm, settings)

            # first: retry pending items (from this consumer's earlier crashed runs).
            # Any pending entry is fair game: on startup, stale entries have
            # times_delivered == 1, so we claim and reprocess them.
            pending = await redis_client.xpending_range(
                settings.redis_stream, STREAM_CONSUMER_GROUP, min="-", max="+",
                count=10, consumername=CONSUMER_NAME,
            )
            retry_ids = [str(p["message_id"]) for p in pending]
            for msg_id in retry_ids:
                msgs = await redis_client.xrange(settings.redis_stream, min=msg_id, max=msg_id)
                if not msgs:
                    await ack_news(redis_client, settings.redis_stream, msg_id)
                    continue
                _, fields = msgs[0]
                data = fields.get("data")
                if not data:
                    logger.warning("No data in fields for msg {}, acking", msg_id)
                    await ack_news(redis_client, settings.redis_stream, msg_id)
                    continue
                if isinstance(data, bytes):
                    data = data.decode()
                item = json.loads(data)
                try:
                    await process_item(db, llm, redis_client, settings, item)
                    await ack_news(redis_client, settings.redis_stream, msg_id)
                    logger.info("Retry succeeded for {}", item.get("link", "?"))
                except Exception as e:
                    logger.warning("Retry failed for {}: {}", item.get("link", "?"), e)

            try:
                entries = await read_news(redis_client, settings.redis_stream, CONSUMER_NAME)
            except Exception as e:
                # transient redis hiccup: log and keep the loop alive
                logger.warning("read_news failed ({}), retrying in 5s", e)
                await asyncio.sleep(5)
                continue
            for msg_id, item in entries:
                try:
                    await process_item(db, llm, redis_client, settings, item)
                except Exception as e:
                    # do NOT ack: item stays pending and will be retried above
                    ITEMS_FAILED.inc()
                    logger.error(
                        "Failed to process item {} (will retry): {}",
                        item.get("link", "?"),
                        e,
                    )
                    continue
                await ack_news(redis_client, settings.redis_stream, str(msg_id))

            # gauges: queue depth + stale backfill backlog
            QUEUE_LENGTH.set(await redis_client.xlen(settings.redis_stream))
            stale_count = await db.stale_count()
            STALE_EMBEDDINGS.set(stale_count)

            # retention: cap redis stream growth
            await trim_stream(redis_client, settings.redis_stream, settings.stream_max_len)

            # retention: prune old db rows once a day (cheap no-op most of the time)
            now_mono = time.monotonic()
            if now_mono - last_prune >= 86400:
                last_prune = now_mono
                try:
                    deleted_items, orphan_events = await db.prune_old_items(
                        settings.db_retention_days
                    )
                    if deleted_items or orphan_events:
                        logger.info(
                            "Retention: pruned {} news items, {} orphaned events",
                            deleted_items, orphan_events,
                        )
                except Exception as e:  # noqa: BLE001 - never kill the loop
                    logger.warning("DB retention prune failed: {}", e)
    finally:
        await liveness.stop()
        await llm.close()
        await db.close()
        await redis_client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Processor stopped")
