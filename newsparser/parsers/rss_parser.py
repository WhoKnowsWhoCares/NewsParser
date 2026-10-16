"""RSS parser: fetch feed, extract entries, download full text, push to redis stream."""

import asyncio
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp
import feedparser
from loguru import logger

from newsparser.config import Settings
from newsparser.metrics import (
    PARSER_ERRORS,
    PARSER_ITEMS_DROPPED,
    PARSER_ITEMS_PUSHED,
)
from newsparser.parsers.common import BROWSER_HEADERS, html_to_text
from newsparser.redis_queue import push_news

# source domain -> (rss url, css selector for full text on article page)
RSS_SOURCES = {
    "www.ria.ru": (
        "https://ria.ru/export/rss2/archive/index.xml",
        "div.article__text",
    ),
}


async def fetch_text(session: aiohttp.ClientSession, link: str, css_selector: str) -> str:
    try:
        async with session.get(link, headers=BROWSER_HEADERS) as resp:
            resp.raise_for_status()
            html = await resp.text()
    except Exception as e:
        logger.warning("Failed to fetch article {}: {}", link, e)
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(css_selector)
    return html_to_text(str(node)) if node else ""


def parse_published(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, attr, None)
        if struct:
            try:
                return datetime.fromtimestamp(
                    __import__("calendar").timegm(struct), tz=timezone.utc
                )
            except Exception:
                pass
    raw = getattr(entry, "published", None)
    if raw:
        try:
            return parsedate_to_datetime(raw)
        except Exception:
            pass
    return None


async def run_rss_parser(settings: Settings, redis_client) -> None:
    interval = settings.poll_interval
    consecutive_errors = 0
    pushed_links: set[str] = set()  # de-dup across polls (link dedup in processor is too late)
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            backoff = min(interval * 2**consecutive_errors, interval * 16)
            for source, (rss_url, css_selector) in RSS_SOURCES.items():
                try:
                    async with session.get(rss_url, headers=BROWSER_HEADERS) as resp:
                        resp.raise_for_status()
                        feed = feedparser.parse(await resp.text())
                except Exception as e:
                    consecutive_errors += 1
                    PARSER_ERRORS.labels("rss").inc()
                    logger.error(
                        "RSS fetch failed for {} (error streak {}, next interval {:.0f}s): {}",
                        source, consecutive_errors, backoff, e,
                    )
                    continue

                consecutive_errors = 0
                for entry in feed.entries[:10][::-1]:
                    link = getattr(entry, "link", "")
                    title = getattr(entry, "title", "")
                    if not link or not title:
                        continue
                    if link in pushed_links:
                        continue  # already queued in an earlier poll
                    pushed_links.add(link)
                    summary = html_to_text(getattr(entry, "summary", "") or "")
                    text = await fetch_text(session, link, css_selector)
                    published = parse_published(entry)
                    item = {
                        "source_type": "rss",
                        "source": source,
                        "title": title,
                        "text": text or summary,
                        "summary": summary,
                        "link": link,
                        "published_at": published.isoformat() if published else None,
                    }
                    result = await push_news(
                        redis_client, settings.redis_stream, item,
                        max_length=settings.queue_max_length,
                    )
                    if result is None:
                        PARSER_ITEMS_DROPPED.labels("rss").inc()
                        logger.warning("Queue cap reached, dropped RSS item: {}", title)
                        continue
                    PARSER_ITEMS_PUSHED.labels("rss").inc()
                    logger.debug("RSS item pushed: {} - {}", source, title)

                await asyncio.sleep(random.uniform(0.5, 2.0))
            await asyncio.sleep(backoff + random.uniform(0, 2.0))
