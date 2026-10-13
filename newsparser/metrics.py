"""Prometheus metrics shared by all services.

Counters/gauges/histograms are module-level singletons (prometheus_client
requirement): importing the same name from any module yields the same series.
Exposed via /metrics on the API and a tiny HTTP server on worker services.
"""

import os

from loguru import logger
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# --- processor ---
ITEMS_PROCESSED = Counter(
    "newsparser_items_processed_total",
    "News items fully processed",
    ["result"],  # ok | stale_saved | duplicate | no_text
)
ITEMS_FAILED = Counter(
    "newsparser_items_failed_total",
    "News items that failed processing (stay pending, will retry)",
)
DEDUP_MATCHES = Counter(
    "newsparser_dedup_matches_total",
    "Dedup matches (item merged into an existing event)",
)
EMBED_FAILURES = Counter(
    "newsparser_embed_failures_total",
    "Embedding call failures (local embedder down)",
)
BACKFILLED = Counter(
    "newsparser_embeddings_backfilled_total",
    "Embeddings computed by the stale-backfill loop",
)
LLM_LATENCY = Histogram(
    "newsparser_llm_latency_seconds",
    "LLM call latency",
    ["endpoint", "provider"],  # embed|chat x local|openrouter
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
STALE_EMBEDDINGS = Gauge(
    "newsparser_stale_embeddings",
    "News items waiting for embedding backfill",
)
QUEUE_LENGTH = Gauge(
    "newsparser_queue_length",
    "Redis stream length (news:raw)",
)

# --- parsers ---
PARSER_ITEMS_PUSHED = Counter(
    "newsparser_parser_items_pushed_total",
    "Items pushed to the queue",
    ["parser"],  # rss | site | telegram
)
PARSER_ITEMS_DROPPED = Counter(
    "newsparser_parser_items_dropped_total",
    "Items dropped because the queue was full",
    ["parser"],
)
PARSER_ERRORS = Counter(
    "newsparser_parser_errors_total",
    "Parser cycle errors (restart follows)",
    ["parser"],
)


def start_metrics_server(port: int | None = None) -> None:
    """Serve /metrics on 0.0.0.0:port (default METRICS_PORT or 9100+)."""
    port = int(port or os.getenv("METRICS_PORT", "9100"))
    try:
        start_http_server(port)
        logger.info("Metrics server on :{}", port)
    except OSError as e:
        logger.warning("Metrics server not started ({}): {}", port, e)
