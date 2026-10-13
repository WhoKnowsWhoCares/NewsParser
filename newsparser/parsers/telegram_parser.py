"""Telegram channels parser via Telethon (user API).

Reads new messages from configured channels and pushes them to the redis stream.

Auth: TELETHON_SESSION env var (StringSession string) when set — preferred for
production (fits secret management). Otherwise falls back to the file-based
session `newsparser.session` (created by newsparser/tools/tg_login.py).

Proxy: TG_PROXY_URL (socks5://host:port or http://host:port) is used when set.
"""

import os

from loguru import logger
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from newsparser.config import Settings
from newsparser.redis_queue import push_news

SESSION_NAME = "newsparser"

SCHEME_TO_PROXY_TYPE = {"socks5": "socks5", "socks4": "socks4", "http": "http", "https": "http"}


def proxy_from_env() -> dict | None:
    """Telethon proxy dict from TG_PROXY_URL (socks5 preferred, http supported)."""
    url = os.getenv("TG_PROXY_URL") or ""
    if not url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(url)
    proxy_type = SCHEME_TO_PROXY_TYPE.get(parsed.scheme)
    if not proxy_type:
        logger.warning("TG_PROXY_URL: unsupported scheme {}, skipping proxy", parsed.scheme)
        return None
    host = parsed.hostname or "172.18.0.1"
    port = parsed.port or 10808
    return {"proxy_type": proxy_type, "addr": host, "port": port}


def build_client(settings: Settings) -> TelegramClient:
    kwargs: dict = {"device_model": "newsparser"}
    proxy = proxy_from_env()
    if proxy:
        # requires python-socks[asyncio]
        kwargs["proxy"] = proxy
        logger.info(
            "Telethon using {} proxy {}:{}",
            proxy["proxy_type"], proxy["addr"], proxy["port"],
        )
    session_string = settings.telethon_session
    if session_string:
        logger.info("Telethon using TELETHON_SESSION (StringSession) from env")
        return TelegramClient(
            StringSession(session_string),
            settings.api_id,
            settings.api_hash,
            **kwargs,
        )
    logger.info("TELETHON_SESSION not set, using file session '{}.session'", SESSION_NAME)
    return TelegramClient(
        SESSION_NAME,
        settings.api_id,
        settings.api_hash,
        **kwargs,
    )


def message_to_item(settings: Settings, message) -> dict | None:
    if not message.text and not message.message:
        return None
    chat = message.chat
    username = getattr(chat, "username", None) or str(getattr(chat, "id", ""))
    text = message.text or message.message
    title = text.split("\n")[0][:200]
    return {
        "source_type": "telegram",
        "source": username,
        "title": title,
        "text": text,
        "summary": "",
        "link": f"https://t.me/{username}/{message.id}",
        "published_at": message.date.isoformat() if message.date else None,
    }


async def run_telegram_parser(settings: Settings, redis_client) -> None:
    if not settings.api_id or not settings.api_hash:
        logger.error("api_id/api_hash not set, telegram parser disabled")
        return

    client = build_client(settings)
    await client.start()  # interactive login on first run only

    channels = settings.channels
    logger.info("Telegram parser started for channels: {}", channels)

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        item = message_to_item(settings, event.message)
        if item is None:
            return
        result = await push_news(
            redis_client, settings.redis_stream, item,
            max_length=settings.queue_max_length,
        )
        if result is None:
            logger.warning(
                "Queue cap reached, dropped TG item: {source} - {title}",
                source=item["source"], title=item["title"],
            )
            return
        logger.debug("TG item pushed: {} - {}", item["source"], item["title"])

    await client.run_until_disconnected()
