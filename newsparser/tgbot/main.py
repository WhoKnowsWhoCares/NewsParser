"""Telegram bot worker.

- Polls postgres for events updated since last check.
- New event -> send message with inline Like button.
- Updated event -> edit the existing message (tg_message_id).
- Like presses stored in postgres `reactions`.
- Subscribers persisted in redis set `tgbot:subscribers`.
"""

import asyncio
import html
import os
from datetime import datetime, timezone

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from newsparser.config import get_settings
from newsparser.db import Database
from newsparser.log_setup import setup_logging
from newsparser.redis_queue import create_redis

POLL_DELAY = 5.0
SUBSCRIBERS_KEY = "tgbot:subscribers"


def proxy_url_from_env() -> str | None:
    """Proxy for Telegram API. SOCKS5 preferred (happ): socks5://172.18.0.1:10808.
    Falls back to http:// URL scheme if given."""
    url = os.getenv("TG_PROXY_URL") or ""
    return url or None


def format_event(event_row, likes: int, sources: list[dict]) -> str:
    summary = html.escape(event_row["summary"])
    links = " · ".join(
        f'<a href="{html.escape(s["link"])}">{html.escape(s["source"])}</a>'
        for s in sources[:5]
    )
    parts = [summary]
    if links:
        parts.append(f"\nИсточники: {links}")
    if event_row["sources_count"] > 1:
        parts.append(f"(<i>обновлено, источников: {event_row['sources_count']}</i>)")
    if likes:
        parts.append(f"👍 {likes}")
    return "\n".join(parts)


def keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("👍 Нравится", callback_data=f"like:{event_id}")]]
    )


class Bot:
    def __init__(self):
        self.settings = get_settings()
        self.db = Database(self.settings.pg_dsn)
        self.redis = None
        self.app: Application = None  # type: ignore[assignment]
        self.last_check = datetime.now(timezone.utc)
        # event_id -> (chat_id, message_id) per subscriber chat
        self.sent: dict[int, dict[int, int]] = {}

    async def start(self) -> None:
        await self.db.connect()
        self.redis = await create_redis(self.settings)
        builder = Application.builder().token(self.settings.bot_token)
        proxy = proxy_url_from_env()
        if proxy:
            logger.info("Using Telegram API proxy: {}", proxy)
            builder = (
                builder.request(HTTPXRequest(proxy=proxy))
                .get_updates_request(HTTPXRequest(proxy=proxy))
            )
        self.app = builder.build()
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("stop", self.cmd_stop))
        self.app.add_handler(CallbackQueryHandler(self.on_like))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Bot started")
        try:
            while True:
                await self.poll_events()
                await asyncio.sleep(POLL_DELAY)
        finally:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            await self.db.close()
            await self.redis.aclose()

    # --- subscribers ---

    async def subscribers(self) -> set[int]:
        raw = await self.redis.smembers(SUBSCRIBERS_KEY)
        return {int(v) for v in raw}

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await self.redis.sadd(SUBSCRIBERS_KEY, chat_id)
        await context.bot.send_message(
            chat_id,
            "Привет! Буду присылать сводки финансовых новостей. "
            "/stop — отписаться.",
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await self.redis.srem(SUBSCRIBERS_KEY, chat_id)
        await context.bot.send_message(chat_id, "Отписка выполнена. /start — вернуться.")

    # --- likes ---

    async def on_like(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        try:
            event_id = int(query.data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        added = await self.db.add_reaction(event_id, query.from_user.id, "like")
        if added:
            likes = await self.db.likes_count(event_id)
            await query.edit_message_reply_markup(reply_markup=keyboard(event_id))
            logger.info("Like for event {} from {}, total {}", event_id, query.from_user.id, likes)

    # --- event polling ---

    async def poll_events(self) -> None:
        rows = await self.db.events_updated_since(self.last_check, limit=20)
        if rows:
            self.last_check = max(r["updated_at"] for r in rows)
        subscribers = await self.subscribers()
        for row in rows:
            event_id = row["id"]
            likes = await self.db.likes_count(event_id)
            sources = await self.db.event_sources(event_id)
            text = format_event(row, likes, sources)
            for chat_id in subscribers:
                try:
                    sent_for_chat = self.sent.get(chat_id, {})
                    msg_id = sent_for_chat.get(event_id)
                    tg_id = row["tg_message_id"]
                    if tg_id:
                        try:
                            await self.app.bot.edit_message_text(
                                text,
                                chat_id=chat_id,
                                message_id=tg_id,
                                parse_mode="HTML",
                                reply_markup=keyboard(event_id),
                                disable_web_page_preview=True,
                            )
                            continue
                        except Exception as e:
                            logger.warning("Edit failed for event {}: {}", event_id, e)
                    if msg_id:
                        try:
                            await self.app.bot.edit_message_text(
                                text,
                                chat_id=chat_id,
                                message_id=msg_id,
                                parse_mode="HTML",
                                reply_markup=keyboard(event_id),
                                disable_web_page_preview=True,
                            )
                            continue
                        except Exception as e:
                            logger.warning("Edit (local map) failed: {}", e)
                    msg = await self.app.bot.send_message(
                        chat_id,
                        text,
                        parse_mode="HTML",
                        reply_markup=keyboard(event_id),
                        disable_web_page_preview=True,
                    )
                    self.sent.setdefault(chat_id, {})[event_id] = msg.message_id
                    await self.db.set_tg_message(event_id, msg.message_id)
                except Exception as e:
                    logger.error("Send to {} failed: {}", chat_id, e)


async def main() -> None:
    settings = get_settings()
    setup_logging()
    if not settings.bot_token:
        logger.error("bot_token is not set, bot disabled")
        return
    bot = Bot()
    try:
        await bot.start()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
