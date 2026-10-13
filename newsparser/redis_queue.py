"""Redis helpers: raw news stream + latest-events cache."""

import json

import redis.asyncio as aioredis
from loguru import logger

from newsparser.config import Settings

STREAM_GROUP = "processors"


async def create_redis(settings: Settings) -> aioredis.Redis:
    client = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )
    await client.ping()
    logger.info("Redis connected")
    return client


async def ensure_stream_group(client: aioredis.Redis, stream: str) -> None:
    """Create consumer group, ignore 'already exists'."""
    try:
        await client.xgroup_create(stream, STREAM_GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def push_news(
    client: aioredis.Redis,
    stream: str,
    item: dict,
    max_length: int = 0,
) -> str | None:
    """Push a news item to the redis stream. Returns msg_id or None if full."""
    # Check current stream length and enforce cap
    if max_length > 0:
        current_len = await client.xlen(stream)
        if current_len >= max_length:
            logger.warning(
                "Queue full ({current_len} >= {max_length}), dropping item: {link}",
                current_len=current_len,
                max_length=max_length,
                link=item.get("link", "?"),
            )
            return None
    result = await client.xadd(stream, {"data": json.dumps(item, ensure_ascii=False)})
    return result.decode() if isinstance(result, bytes) else result


async def read_news(client: aioredis.Redis, stream: str, consumer: str, count: int = 10):
    """Read pending entries for the processor consumer group (blocking up to 5s)."""
    result = await client.xreadgroup(
        STREAM_GROUP, consumer, {stream: ">"}, count=count, block=5000
    )
    entries = []
    for _stream, messages in result:
        for msg_id, fields in messages:
            entries.append((msg_id, json.loads(fields["data"])))
    return entries


async def ack_news(client: aioredis.Redis, stream: str, msg_id: str) -> None:
    await client.xack(stream, STREAM_GROUP, msg_id)


async def trim_stream(client: aioredis.Redis, stream: str, max_len: int) -> None:
    """Cap the stream length (acked entries included). ~ = approximate trim."""
    if max_len > 0:
        await client.xtrim(stream, maxlen=max_len, approximate=True)


async def cache_event(client: aioredis.Redis, settings: Settings, event: dict) -> None:
    """Store latest event summary in a redis hash and trim to the last N events."""
    key = settings.redis_latest_key
    await client.hset(key, str(event["id"]), json.dumps(event, ensure_ascii=False))
    # trim: keep only redis_latest_max newest by updated_at
    all_events = await client.hgetall(key)
    if len(all_events) > settings.redis_latest_max:
        ranked = sorted(
            all_events.items(),
            key=lambda kv: json.loads(kv[1]).get("updated_at", ""),
            reverse=True,
        )
        stale = [event_id for event_id, _ in ranked[settings.redis_latest_max:]]
        if stale:
            await client.hdel(key, *stale)
