"""Dashboard API: 5-minute time series for the last hour + thresholds.

Feeds the metrics charts on the web page.

How it works: the API process scrapes the worker /metrics endpoints
(processor :9102, parsers :9101) once per 5-minute bucket, keeps an
in-memory snapshot history, and computes per-bucket counter increments
(rates) and gauge values on request. Freshness (seconds from source
publication to full processing) comes from postgres news_items.
"""

import asyncio
import time

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/metrics-history")

# worker metrics endpoints (compose network names)
SCRAPER_TARGETS = [
    "http://news_processor:9102/metrics",
    "http://news_parsers:9101/metrics",
]

BUCKET_SECONDS = 300  # 5 min
WINDOW_BUCKETS = 12   # 1 hour

# thresholds shown as horizontal lines on the charts
THRESHOLDS = {
    "stale_embeddings": 1,     # >0 for long = embedder down
    "items_failed": 0,         # any growth is abnormal
    "freshness_seconds": 600,  # 10 min from publication to event
}

# {key: value}, labels joined into the key: name:label1_label2
_HISTORY: list[dict] = []  # [{ts, values}], one entry per bucket
_HIST_LAST_BUCKET = 0
_HIST_LOCK = asyncio.Lock()


def _bucket_start(ts: float) -> int:
    return int(ts // BUCKET_SECONDS * BUCKET_SECONDS)


async def _scrape_once(client: httpx.AsyncClient, url: str) -> dict[str, float]:
    """Parse the Prometheus text exposition into {key: value}."""
    from prometheus_client.parser import text_string_to_metric_families

    resp = await client.get(url, timeout=5.0)
    resp.raise_for_status()
    out: dict[str, float] = {}
    for family in text_string_to_metric_families(resp.text):
        for sample in family.samples:
            key = sample.name
            if sample.labels:
                suffix = "_".join(v for _, v in sorted(sample.labels.items()))
                key = f"{sample.name}:{suffix}"
            out[key] = sample.value
    return out


async def _scrape_all() -> dict[str, float]:
    """Merge all targets; later targets overwrite duplicate keys."""
    merged: dict[str, float] = {}
    async with httpx.AsyncClient() as client:
        for url in SCRAPER_TARGETS:
            try:
                merged.update(await _scrape_once(client, url))
            except Exception:  # noqa: BLE001 - worker may be down; keep old values
                continue
    return merged


async def _snapshot_if_new_bucket() -> None:
    """Store one snapshot per 5-minute bucket (called on every request)."""
    global _HIST_LAST_BUCKET, _HISTORY
    now_bucket = _bucket_start(time.time())
    if now_bucket == _HIST_LAST_BUCKET and _HISTORY:
        return
    async with _HIST_LOCK:
        now_bucket = _bucket_start(time.time())  # re-check under lock
        if now_bucket == _HIST_LAST_BUCKET and _HISTORY:
            return
        values = await _scrape_all()
        _HISTORY.append({"ts": now_bucket, "values": values})
        _HIST_LAST_BUCKET = now_bucket
        cutoff = now_bucket - (WINDOW_BUCKETS + 1) * BUCKET_SECONDS
        _HISTORY = [h for h in _HISTORY if h["ts"] >= cutoff]


def _rate(history: list[dict], key: str) -> list[float]:
    """Per-bucket increment of a counter; 0 for the first bucket."""
    rates = []
    for i, h in enumerate(history):
        if i == 0:
            rates.append(0.0)
            continue
        prev = history[i - 1]["values"].get(key, 0.0)
        cur = h["values"].get(key, 0.0)
        rates.append(max(0.0, cur - prev))
    return rates


def _gauges(history: list[dict], key: str) -> list[float]:
    return [h["values"].get(key, 0.0) for h in history]


def _freshness_series(rows) -> list[float | None]:
    now_bucket = _bucket_start(time.time())
    by_bucket = {r["bucket"]: float(r["avg_secs"]) for r in rows if r["n"]}
    return [
        by_bucket.get(now_bucket - i * BUCKET_SECONDS)
        for i in range(WINDOW_BUCKETS - 1, -1, -1)
    ]


@router.get("")
async def metrics_history():
    """12 five-minute buckets for the last hour, aligned to bucket starts."""
    await _snapshot_if_new_bucket()
    history = list(_HISTORY)

    def counter_series(base: str, label: str | None = None) -> list[float]:
        key = f"{base}:{label}" if label else base
        return _rate(history, key)

    pushed = {}
    dropped = {}
    for parser in ("rss", "site", "telegram"):
        pushed[parser] = counter_series("newsparser_parser_items_pushed_total", parser)
        dropped[parser] = counter_series("newsparser_parser_items_dropped_total", parser)

    # freshness: avg (parsed_at - published_at) per 5-min bucket of parsed_at
    from newsparser.api.main import db  # late import: the pool lives in api.main

    now_bucket = _bucket_start(time.time())
    start = now_bucket - (WINDOW_BUCKETS - 1) * BUCKET_SECONDS
    rows = await db.pool.fetch(
        """
        SELECT
            (extract(epoch from parsed_at)::bigint / $2) * $2 AS bucket,
            avg(extract(epoch from (parsed_at - published_at))) AS avg_secs,
            count(*) AS n
        FROM news_items
        WHERE parsed_at >= to_timestamp($1)
          AND published_at IS NOT NULL
          AND parsed_at >= published_at   -- ignore clock-skew negatives
        GROUP BY bucket
        """,
        float(start),
        BUCKET_SECONDS,
    )

    return {
        "bucket_seconds": BUCKET_SECONDS,
        "buckets": [now_bucket - i * BUCKET_SECONDS for i in range(WINDOW_BUCKETS - 1, -1, -1)],
        "series": {
            "processed_ok": counter_series("newsparser_items_processed_total", "ok"),
            "processed_stale": counter_series("newsparser_items_processed_total", "stale_saved"),
            "items_failed": counter_series("newsparser_items_failed_total"),
            "dedup_matches": counter_series("newsparser_dedup_matches_total"),
            "embed_failures": counter_series("newsparser_embed_failures_total"),
            "backfilled": counter_series("newsparser_embeddings_backfilled_total"),
            "queue_length": _gauges(history, "newsparser_queue_length"),
            "stale_embeddings": _gauges(history, "newsparser_stale_embeddings"),
            "pushed": pushed,
            "dropped": dropped,
            "freshness_seconds": _freshness_series(rows),
        },
        "thresholds": THRESHOLDS,
        "targets_up": [t for t in SCRAPER_TARGETS],
    }
