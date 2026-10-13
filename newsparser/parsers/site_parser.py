"""Site scraper for bcs-express.ru news feed (no RSS)."""

import asyncio
import random

import aiohttp
from bs4 import BeautifulSoup
from loguru import logger

from newsparser.config import Settings
from newsparser.metrics import (
    PARSER_ERRORS,
    PARSER_ITEMS_DROPPED,
    PARSER_ITEMS_PUSHED,
)
from newsparser.parsers.common import BROWSER_HEADERS, html_to_text
from newsparser.redis_queue import push_news

BASE_URL = "https://bcs-express.ru"
FEED_URL = f"{BASE_URL}/category"
SOURCE = "www.bcs-express.ru"


async def parse_feed(session: aiohttp.ClientSession) -> list[dict]:
    """Parse the news feed page, return list of {link, title}."""
    async with session.get(FEED_URL, headers=BROWSER_HEADERS) as resp:
        resp.raise_for_status()
        html = await resp.text()
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()
    # feed item cards link to articles; keep absolute hrefs only
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href.startswith(BASE_URL):
            continue
        # article urls look like https://bcs-express.ru/novosti-i-analitika/...
        if "/novosti-i-analitika/" not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        title = html_to_text(str(a))[:300].strip()
        items.append({"link": href, "title": title})
    return items


async def parse_article(session: aiohttp.ClientSession, link: str) -> str:
    async with session.get(link, headers=BROWSER_HEADERS) as resp:
        resp.raise_for_status()
        html = await resp.text()
    soup = BeautifulSoup(html, "lxml")
    # article body container; fallback to whole page text if markup changes
    node = (
        soup.select_one("div.article__body")
        or soup.select_one("article")
        or soup.select_one("main")
    )
    return html_to_text(str(node)) if node else ""


async def run_site_parser(settings: Settings, redis_client) -> None:
    interval = settings.poll_interval
    consecutive_errors = 0
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            backoff = min(interval * 2**consecutive_errors, interval * 16)
            try:
                feed_items = await parse_feed(session)
                consecutive_errors = 0
                for feed_item in feed_items[:10]:
                    text = await parse_article(session, feed_item["link"])
                    if not text:
                        continue
                    item = {
                        "source_type": "site",
                        "source": SOURCE,
                        "title": feed_item["title"] or text[:120],
                        "text": text,
                        "summary": "",
                        "link": feed_item["link"],
                        "published_at": None,
                    }
                    result = await push_news(
                        redis_client, settings.redis_stream, item,
                        max_length=settings.queue_max_length,
                    )
                    if result is None:
                        PARSER_ITEMS_DROPPED.labels("site").inc()
                        logger.warning(
                            "Queue cap reached, dropped site item: {}", feed_item["link"]
                        )
                        continue
                    PARSER_ITEMS_PUSHED.labels("site").inc()
                    logger.debug("Site item pushed: {}", feed_item["link"])
            except Exception as e:
                consecutive_errors += 1
                PARSER_ERRORS.labels("site").inc()
                logger.error(
                    "Site parser failed (error streak {}, next interval {:.0f}s): {}",
                    consecutive_errors, backoff, e,
                )
            await asyncio.sleep(backoff + random.uniform(0, 2.0))
