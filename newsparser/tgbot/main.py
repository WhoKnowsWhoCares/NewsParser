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
from typing import Any

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)
from telegram.ext import (
    filters as tg_filters,
)
from telegram.request import HTTPXRequest

from newsparser.config import get_settings
from newsparser.db import Database
from newsparser.log_setup import LivenessFile, setup_logging
from newsparser.redis_queue import create_redis
from newsparser.tgbot import filters as uf
from newsparser.tgbot.alerts import AdminAlerter

POLL_DELAY = 5.0
SUBSCRIBERS_KEY = "tgbot:subscribers"
SENT_KEY = "tgbot:sent"  # hash: "chat_id:event_id" -> message_id (survives restarts)

# ConversationHandler states for the /filters dialog
(FS_MENU, FS_WAIT_VALUE) = range(2)


def proxy_url_from_env() -> str | None:
    """Proxy for Telegram API. SOCKS5 preferred (happ): socks5://172.18.0.1:10808.
    Falls back to http:// URL scheme if given."""
    url = os.getenv("TG_PROXY_URL") or ""
    return url or None


IMPACT_LABELS = {"low": "🟢 low", "med": "🟡 med", "high": "🔴 high"}


def format_event(event_row, likes: int, sources: list[dict]) -> str:
    summary = html.escape(event_row["summary"])
    links = " · ".join(
        f'<a href="{html.escape(s["link"])}">{html.escape(s["source"])}</a>'
        for s in sources[:5]
    )
    # header: impact rank + category (first tag) + tickers/other tags
    impact = (event_row.get("impact") or "low").lower()
    header = IMPACT_LABELS.get(impact, impact)
    tags = [t for t in (event_row.get("tags") or []) if t.strip()]
    if tags:
        header += " · " + " ".join(f"#{html.escape(t)}" for t in tags)
    parts = [header, "", summary]
    if links:
        parts.append(f"\nИсточники: {links}")
    if event_row["sources_count"] > 1:
        parts.append(f"(<i>обновлено, источников: {event_row['sources_count']}</i>)")
    if likes:
        parts.append(f"👍 {likes}")
    return "\n".join(parts)


def keyboard(event_id: int) -> InlineKeyboardMarkup:
    # likes are disabled for now (feature on hold); keep the signature for
    # the reactions pipeline — no buttons attached to messages
    return InlineKeyboardMarkup([])


class Bot:
    def __init__(self):
        self.settings = get_settings()
        self.db = Database(self.settings.pg_dsn)
        self.redis: Any = None
        self.app: Application = None  # type: ignore[assignment]
        self.last_check = datetime.now(timezone.utc)

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
        filter_conv = ConversationHandler(
            entry_points=[CommandHandler("filters", self.cmd_filters)],
            states={
                FS_MENU: [
                    CallbackQueryHandler(self.on_filter_button, pattern=r"^fset:"),
                ],
                FS_WAIT_VALUE: [
                    MessageHandler(
                        tg_filters.TEXT & ~tg_filters.COMMAND, self.on_filter_value
                    ),
                    CallbackQueryHandler(self.on_filter_button, pattern=r"^fset:"),
                ],
            },
            fallbacks=[CommandHandler("filters", self.cmd_filters)],
            name="filters",
            per_message=False,
        )
        self.app.add_handler(filter_conv)
        # like handler last: fset: callbacks are taken by the conversation
        self.app.add_handler(CallbackQueryHandler(self.on_like, pattern=r"^like:"))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Bot started")
        liveness = LivenessFile("/tmp/liveness_tgbot", max_age_hint=180.0)
        liveness.start(interval=20.0)
        alerter_task = None
        admin = self.settings.tg_bot_admin_chat.strip()
        if admin:
            alerter_task = asyncio.create_task(
                AdminAlerter(self.settings.bot_token, admin).run()
            )
        try:
            while True:
                try:
                    await self.poll_events()
                except Exception as e:  # noqa: BLE001 - transient DB/redis/telegram errors
                    # must not kill the bot: log and keep polling
                    logger.error("poll_events failed: {}", e)
                await asyncio.sleep(POLL_DELAY)
        finally:
            if alerter_task:
                alerter_task.cancel()
            await liveness.stop()
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
            "Привет! Буду присылать все новые сводки финансовых новостей.\n"
            "/filters — настроить фильтры (источники, значимость, теги).\n"
            "/stop — отписаться.",
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await self.redis.srem(SUBSCRIBERS_KEY, chat_id)
        await context.bot.send_message(chat_id, "Отписка выполнена. /start — вернуться.")

    # --- /filters conversation ---

    FILTER_BUTTONS = {
        "sources": "📰 Источники",
        "impact": "📈 Значимость",
        "tags": "🏷 Теги",
    }

    def filters_menu_markup(self, current: dict[str, str]) -> InlineKeyboardMarkup:
        rows = []
        for field, label in self.FILTER_BUTTONS.items():
            val = (current.get(field) or "").strip()
            suffix = " ✅" if val else ""
            rows.append([InlineKeyboardButton(label + suffix, callback_data=f"fset:{field}")])
        rows.append([
            InlineKeyboardButton("♻️ Сбросить всё", callback_data="fset:reset"),
            InlineKeyboardButton("✖️ Закрыть", callback_data="fset:close"),
        ])
        return InlineKeyboardMarkup(rows)

    async def cmd_filters(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        current = await uf.load(self.redis, chat_id)
        text = "Фильтры подписки:\n" + uf.summary_text(current) + (
            "\n\nВыбери, что настроить:"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=self.filters_menu_markup(current)
            )
        else:
            await context.bot.send_message(
                chat_id, text, reply_markup=self.filters_menu_markup(current)
            )
        return FS_MENU

    async def on_filter_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat.id
        action = query.data.split(":", 1)[1]
        if action == "close":
            await query.edit_message_text("Настройки сохранены. /filters — открыть снова.")
            return ConversationHandler.END
        if action == "reset":
            await uf.clear(self.redis, chat_id)
            return await self.cmd_filters(update, context)
        context.user_data["filter_field"] = action
        hints = {
            "sources": "Перечисли источники через запятую (например: markettwits, rbc_news). "
                       "«все» — отключить фильтр.",
            "impact": "Минимальная значимость для рынка:\nlow, med или high («все» — без фильтра).",
            "tags": "Теги через запятую (ЦБ, SBER, САНКЦИИ).\n«все» — без фильтра.",
        }
        await query.message.reply_text(hints[action])
        return FS_WAIT_VALUE

    async def on_filter_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        field = context.user_data.get("filter_field")
        if field not in uf.FIELDS:
            await update.message.reply_text("Не понял поле. /filters — начать заново.")
            return ConversationHandler.END
        ok, value = uf.parse_set_value(field, update.message.text or "")
        if not ok:
            await update.message.reply_text(f"Не понял: {value}. Попробуй ещё раз.")
            return FS_WAIT_VALUE
        current = await uf.load(self.redis, chat_id)
        current[field] = value
        await uf.save(self.redis, chat_id, current)
        await update.message.reply_text("Сохранил ✅")
        # re-open the menu with fresh state
        text = "Фильтры подписки:\n" + uf.summary_text(current) + "\n\nВыбери, что настроить:"
        await context.bot.send_message(
            chat_id, text, reply_markup=self.filters_menu_markup(current)
        )
        return FS_MENU

    # --- per-(chat, event) sent-message map (redis, survives restarts) ---

    async def _sent_msg_id(self, chat_id: int, event_id: int) -> int | None:
        raw = await self.redis.hget(SENT_KEY, f"{chat_id}:{event_id}")
        return int(raw) if raw else None

    async def _set_sent_msg_id(self, chat_id: int, event_id: int, message_id: int) -> None:
        await self.redis.hset(SENT_KEY, f"{chat_id}:{event_id}", message_id)

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
        rows = await self.db.events_updated_since(self.last_check, limit=50)
        if rows:
            if len(rows) == 50:
                # batch full: there may be more events beyond the limit —
                # advance only to the OLDEST seen so the next poll re-reads
                # the tail (the sent-map dedup keeps this from duplicating)
                self.last_check = min(r["updated_at"] for r in rows)
            else:
                self.last_check = max(r["updated_at"] for r in rows)
        subscribers = await self.subscribers()
        if not subscribers:
            return
        # per-subscriber filters: chat_id -> filters dict (lazy, cached per poll)
        user_filters: dict[int, dict[str, str]] = {}
        for row in rows:
            event_id = row["id"]
            likes = await self.db.likes_count(event_id)
            sources = await self.db.event_sources(event_id)
            text = format_event(row, likes, sources)
            event_ctx = {
                "sources": [s["source"] for s in sources],
                "tags": list(row.get("tags") or []),
                "impact": row.get("impact") or "low",
            }
            for chat_id in subscribers:
                if chat_id not in user_filters:
                    user_filters[chat_id] = await uf.load(self.redis, chat_id)
                if not uf.matches(user_filters[chat_id], event_ctx):
                    continue
                try:
                    msg_id = await self._sent_msg_id(chat_id, event_id)
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
                            # message deleted/blocked -> fall through and resend
                            logger.warning(
                                "Edit failed for event {} in {}: {}", event_id, chat_id, e
                            )
                    msg = await self.app.bot.send_message(
                        chat_id,
                        text,
                        parse_mode="HTML",
                        reply_markup=keyboard(event_id),
                        disable_web_page_preview=True,
                    )
                    await self._set_sent_msg_id(chat_id, event_id, msg.message_id)
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
