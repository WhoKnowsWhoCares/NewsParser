"""Parsers entrypoint: run all source parsers concurrently."""

import asyncio

from loguru import logger

from newsparser.config import get_settings
from newsparser.log_setup import LivenessFile, setup_logging
from newsparser.metrics import start_metrics_server
from newsparser.parsers import rss_parser, site_parser, telegram_parser
from newsparser.redis_queue import create_redis

LIVENESS = LivenessFile("/tmp/liveness_collector", max_age_hint=180.0)


async def run_with_restart(name: str, coro_factory, restart_delay: float = 10.0):
    """Run a parser forever, restart it if it crashes."""
    while True:
        try:
            await coro_factory()
            logger.warning("{} exited normally, restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("{} crashed: {}, restarting in {}s", name, e, restart_delay)
        await asyncio.sleep(restart_delay)


async def main() -> None:
    settings = get_settings()
    setup_logging()
    redis_client = await create_redis(settings)
    logger.info("Starting parsers: rss, site, telegram")
    LIVENESS.start(interval=20.0)
    start_metrics_server()

    tasks = [
        asyncio.create_task(
            run_with_restart("rss", lambda: rss_parser.run_rss_parser(settings, redis_client))
        ),
        asyncio.create_task(
            run_with_restart(
                "site", lambda: site_parser.run_site_parser(settings, redis_client)
            )
        ),
        asyncio.create_task(
            run_with_restart(
                "telegram",
                lambda: telegram_parser.run_telegram_parser(settings, redis_client),
            )
        ),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await redis_client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Parsers stopped")
