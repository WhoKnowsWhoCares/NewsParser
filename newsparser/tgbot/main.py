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
    ConversationHandler,
    MessageHandler,
)
from telegram.ext import (
    filters as tg_filters,
)
from telegram.request import HTTPXRequest

from newsparser.config import get_settings
from newsparser.db import Database
from newsparser.log_setup import setup_logging
from newsparser.redis_queue import create_redis
from newsparser.tgbot import filters as uf

POLL_DELAY = 5.0
SUBSCRIBERS_KEY = "tgbot:subscribers"

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
