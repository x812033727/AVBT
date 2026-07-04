"""Single-worker webhook fan-out.

Both tracker and archiver previously did
``asyncio.create_task(send_webhook(msg))`` directly, which is unbounded:
a sweep that completes 30 archives at once spawns 30 concurrent HTTP
POSTs against the configured webhook URL. This module funnels every
notification through a 1-worker asyncio.Queue so the receiving endpoint
sees strict serial delivery, regardless of how bursty the producers
get. The queue is bounded; oldest-callers-win is the wrong semantic
for notifications, so we drop the *new* message when full and bump a
counter exposed via ``status()`` for the UI to surface."""

from __future__ import annotations

import asyncio
import logging

from .notify import send_notification

logger = logging.getLogger(__name__)


class WebhookQueue:
    def __init__(self, concurrency: int = 1, maxsize: int = 256) -> None:
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=maxsize)
        self._workers: list[asyncio.Task] = []
        self._started = False
        self._dropped = 0
        self._sent = 0
        self._failed = 0

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        loop = asyncio.get_event_loop()
        for i in range(self._concurrency):
            self._workers.append(loop.create_task(self._worker(i)))
        logger.info("webhook queue started with %d worker(s)", self._concurrency)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._started = False

    def enqueue_nowait(self, message: str, event: str = "generic") -> bool:
        """Push without awaiting. ``event`` selects the per-event toggle
        (see notify.EVENT_DEFAULTS); unknown events always deliver.
        Returns False when the queue is full (message dropped, counter
        bumped). Safe to call from sync code."""
        if not message:
            return False
        try:
            self._queue.put_nowait((event, message))
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning("webhook queue full, dropped message (total=%d)", self._dropped)
            return False

    def status(self) -> dict:
        return {
            "started": self._started,
            "concurrency": self._concurrency,
            "pending": self._queue.qsize(),
            "sent": self._sent,
            "failed": self._failed,
            "dropped": self._dropped,
        }

    async def _worker(self, idx: int) -> None:
        while True:
            event, msg = await self._queue.get()
            try:
                await send_notification(msg, event)
                self._sent += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self._failed += 1
                logger.exception("webhook worker %d failed", idx)
            finally:
                self._queue.task_done()


webhook_queue = WebhookQueue(concurrency=1)
