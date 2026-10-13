"""Logging setup (loguru): text for dev, JSON for prod (LOG_FORMAT=json).

Rotation keeps container/file logs bounded; `retention` prunes old files.
"""

import asyncio
import json as _json
import os
import sys
from pathlib import Path

from loguru import logger


def _serialize(record) -> str:
    """Flatten a loguru record into one JSON line."""
    payload = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": f"{record['name']}:{record['function']}:{record['line']}",
        "message": record["message"],
    }
    payload.update(record["extra"])
    return _json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", json_format: bool | None = None) -> None:
    if json_format is None:
        json_format = os.getenv("LOG_FORMAT", "").lower() == "json"
    logger.remove()
    if json_format:

        def stdout_sink(message) -> None:
            sys.stdout.write(_serialize(message.record) + "\n")

        logger.add(stdout_sink, level=level)
        logger.add(
            "./logs/log_{time:YYYY-MM-DD}.json",
            level=level,
            rotation="00:01",
            retention="30 days",
            enqueue=True,
            backtrace=True,
            diagnose=False,
            format="{message}",
        )
    else:
        logger.add(sys.stdout, level=level)
        logger.add(
            "./logs/log_{time:YYYY-MM-DD}.log",
            level=level,
            rotation="00:01",
            retention="30 days",
            backtrace=True,
            diagnose=False,
        )


class LivenessFile:
    """Touch a file periodically; a compose healthcheck checks its mtime.

    Used by services without an HTTP port (processor, collector): a separate
    asyncio task touches the file every `interval` seconds, so a stale file
    means the event loop is dead/stuck and the healthcheck turns unhealthy.
    """

    def __init__(self, path: str = "/tmp/liveness", max_age_hint: float = 180.0):
        self.path = Path(path)
        # healthcheck threshold must be a few multiples of the touch interval
        self.max_age_hint = max_age_hint
        self._task: asyncio.Task | None = None

    def touch(self) -> None:
        try:
            self.path.touch()
        except OSError as e:
            logger.warning("Liveness touch failed ({}): {}", self.path, e)

    def start(self, interval: float = 20.0) -> None:
        """Start the background heartbeat task."""
        self.touch()
        self._task = asyncio.create_task(self._beat(interval))

    async def _beat(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self.touch()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
