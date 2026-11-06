"""Technical alerts delivered to the admin chat via the Telegram bot.

Messages are prefixed with ``[ALERT]`` so they are visually distinct from
news deliveries. The bot worker runs the watchdog loop; other workers
report through redis pub/sub channel ``alerts`` so they don't need bot
credentials.
"""

import asyncio
import os
from datetime import datetime, timezone

import httpx
from loguru import logger

from newsparser.config import get_settings
from newsparser.redis_queue import create_redis

ALERTS_CHANNEL = "alerts"
PREFIX = "[ALERT]"

# repeat-suppression: don't resend the same alert more often than this
_COOLDOWN_SECONDS = 3600


def _proxy() -> str | None:
    return os.getenv("TG_PROXY_URL") or None


async def send_alert(text: str) -> None:
    """Publish an alert; the bot worker delivers it to the admin chat.

    Safe to call from any worker: when the bot is down, alerts stay in the
    redis channel history is not kept (pub/sub), so also write to a redis
    list as a spool the bot drains on start.
    """
    settings = get_settings()
    redis = await create_redis(settings)
    try:
        await redis.lpush(ALERTS_CHANNEL + ":spool", text)
        await redis.publish(ALERTS_CHANNEL, text)
    finally:
        await redis.aclose()


async def deliver_spooled(bot_token: str, admin_chat: str, proxy: str | None) -> None:
    """Send all spooled alerts to the admin chat (called by the bot worker)."""
    if not admin_chat:
        return
    settings = get_settings()
    redis = await create_redis(settings)
    try:
        while True:
            raw = await redis.rpop(ALERTS_CHANNEL + ":spool")
            if raw is None:
                break
            await _send_text(bot_token, admin_chat, proxy, str(raw))
    finally:
        await redis.aclose()


async def _send_text(bot_token: str, admin_chat: str, proxy: str | None, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": admin_chat, "text": f"{PREFIX} {text}"}
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=15) as client:
            r = await client.post(url, json=payload)
            return r.status_code == 200
    except Exception as e:  # noqa: BLE001 - alerting must never crash the caller
        logger.warning("Alert delivery failed: {}", e)
        return False


class AdminAlerter:
    """Watchdog loop inside the bot worker: checks queue depth / worker
    liveness / embedder state and reports problems to the admin chat.

    Cooldowns keep repeated failures from spamming: an alert for the same
    key is sent at most once per hour.
    """

    def __init__(self, bot_token: str, admin_chat: str):
        self.bot_token = bot_token
        self.admin_chat = admin_chat
        self.proxy = _proxy()
        self._last_sent: dict[str, float] = {}

    def _suppressed(self, key: str) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        last = self._last_sent.get(key, 0)
        if now - last < _COOLDOWN_SECONDS:
            return True
        self._last_sent[key] = now
        return False

    async def _check_once(self) -> list[str]:
        settings = get_settings()
        problems: list[str] = []
        try:
            from newsparser.redis_queue import create_redis

            redis = await create_redis(settings)
            try:
                # real backlog: undelivered (lag) + delivered-but-unacked
                # (pending). XLEN would count acked entries awaiting the
                # retention trim — those are done and not a problem.
                groups = await redis.xinfo_groups(settings.redis_stream)
                lag = int(groups[0].get("lag", 0) or 0)
                pending = int(groups[0].get("pending", 0) or 0)
                backlog = max(lag, pending)
                if backlog > 200 and not self._suppressed("queue"):
                    problems.append(
                        f"queue backlog: {backlog} unprocessed items in {settings.redis_stream}"
                    )
                # spool size: alerts piling up means the bot itself was down
            finally:
                await redis.aclose()
        except Exception as e:  # noqa: BLE001
            if not self._suppressed("redis"):
                problems.append(f"redis unreachable: {e}")

        try:
            db_ok = True
            import asyncpg

            conn = await asyncpg.connect(
                host=settings.db_host,
                port=settings.db_port,
                user=settings.db_user,
                password=settings.db_password,
                database=settings.db_name,
                timeout=5,
            )
            await conn.close()
        except Exception as e:  # noqa: BLE001
            db_ok = False
            if not self._suppressed("db"):
                problems.append(f"postgres unreachable: {e}")
        del db_ok

        # local embedder health (primary embeddings endpoint)
        try:
            import httpx

            async with httpx.AsyncClient(
                proxy=get_settings().tg_proxy_url or None, timeout=10
            ) as client:
                r = await client.get(settings.embeddings_base_url.rstrip("/") + "/models")
                if r.status_code >= 500 and not self._suppressed("embedder"):
                    problems.append(f"embedder returns {r.status_code}")
        except Exception as e:  # noqa: BLE001
            if not self._suppressed("embedder"):
                problems.append(f"embedder unreachable: {e}")
        return problems

    async def run(self, interval: float = 300.0) -> None:
        if not self.admin_chat:
            logger.info("No TG_BOT_ADMIN_CHAT set, technical alerts disabled")
            return
        logger.info("Admin alerter started (chat {})", self.admin_chat)
        # drain alerts spooled while the bot was down
        await deliver_spooled(self.bot_token, self.admin_chat, self.proxy)
        while True:
            try:
                for problem in await self._check_once():
                    logger.warning("ALERT: {}", problem)
                    ok = await _send_text(
                        self.bot_token, self.admin_chat, self.proxy, problem
                    )
                    if not ok and not self._suppressed("delivery"):
                        logger.warning("Alert to admin chat failed")
            except Exception as e:  # noqa: BLE001 - watchdog must not die
                logger.error("Alerter loop error: {}", e)
            await asyncio.sleep(interval)
