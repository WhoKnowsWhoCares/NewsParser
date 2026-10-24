"""User filter settings for the Telegram bot, persisted in redis.

Each subscriber has a hash `tgbot:filters:<chat_id>`:

    sources  — comma-separated source names (empty = all)
    impact   — minimal market impact: "" (all) | low | med | high
    tags     — comma-separated tags, ANY-match (empty = all)

Empty field = no filter (the default: a new subscriber gets everything).
"""

import json

FILTERS_PREFIX = "tgbot:filters"

FIELDS = ("sources", "impact", "tags")
IMPACT_RANK = {"low": 0, "med": 1, "high": 2}


def _key(chat_id: int) -> str:
    return f"{FILTERS_PREFIX}:{chat_id}"


async def load(redis, chat_id: int) -> dict[str, str]:
    raw = await redis.hgetall(_key(chat_id))
    return {f: (raw.get(f) or "").strip() for f in FIELDS}


async def save(redis, chat_id: int, filters: dict[str, str]) -> None:
    await redis.hset(_key(chat_id), mapping={f: (filters.get(f) or "").strip() for f in FIELDS})


async def clear(redis, chat_id: int) -> None:
    await redis.delete(_key(chat_id))


def _csv(field: str) -> set[str]:
    return {p.strip().lower() for p in field.split(",") if p.strip()}


def matches(filters: dict[str, str], event: dict) -> bool:
    """True when the event passes all non-empty filters.

    sources: the event must contain at least one news item from a listed
    source (event["sources"] — list of source names, filled by the caller).
    impact: event impact rank >= the chosen minimum.
    tags: at least one listed tag present among event tags.
    """
    want_sources = _csv(filters.get("sources", ""))
    if want_sources and not want_sources & {s.lower() for s in event.get("sources", ())}:
        return False

    want_impact = (filters.get("impact") or "").strip().lower()
    if want_impact in IMPACT_RANK:
        event_rank = IMPACT_RANK.get((event.get("impact") or "low").lower(), 0)
        if event_rank < IMPACT_RANK[want_impact]:
            return False

    want_tags = _csv(filters.get("tags", ""))
    if want_tags and not want_tags & {t.lower() for t in event.get("tags", ())}:
        return False

    return True


def summary_text(filters: dict[str, str]) -> str:
    """Human-readable filter state for the /filters command."""
    lines = []
    labels = {"sources": "Источники", "impact": "Мин. значимость", "tags": "Теги"}
    for f in FIELDS:
        val = (filters.get(f) or "").strip()
        shown = val if val else "все"
        if f == "impact" and val:
            shown = f"{val} и выше"
        lines.append(f"{labels[f]}: {shown}")
    return "\n".join(lines)


def parse_set_value(field: str, raw: str) -> tuple[bool, str]:
    """Validate a value typed by the user for one filter field.

    Returns (ok, normalized_value_or_error_hint).
    """
    raw = raw.strip()
    if raw in ("", "все", "all", "нет", "-"):
        return True, ""
    if field == "impact":
        v = raw.lower()
        if v in IMPACT_RANK:
            return True, v
        return False, "укажи low, med или high (или «все»)"
    if field == "sources":
        # comma-separated; keep user casing but validate loosely
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            return False, "перечисли источники через запятую"
        return True, ", ".join(parts)
    if field == "tags":
        parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
        if not parts:
            return False, "перечисли теги через запятую"
        return True, ", ".join(parts)
    return False, "неизвестное поле"


def dumps(filters: dict[str, str]) -> str:
    return json.dumps(filters, ensure_ascii=False)


def loads(raw: str) -> dict[str, str]:
    try:
        d = json.loads(raw)
        return {f: str(d.get(f) or "") for f in FIELDS}
    except (json.JSONDecodeError, TypeError):
        return {f: "" for f in FIELDS}
