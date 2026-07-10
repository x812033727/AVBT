"""Last-resort supervision for background tasks.

Every long-lived loop here (tracker, archiver, queue workers, …) already
catches per-iteration exceptions, but code *outside* those inner try
blocks — ``queue.get()``, lock acquisition, the loop shell itself — can
still raise, and an unsupervised task then dies silently: the process
stays healthy while a worker slot or an entire subsystem is gone until
the next restart. ``supervise`` wraps the coroutine factory so any
unexpected exit is logged loudly and the task is rebuilt after a short
delay. Cancellation (shutdown) passes through untouched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

RESTART_DELAY_S = 5.0


def supervise(
    factory: Callable[[], Awaitable[None]],
    name: str,
    *,
    restart_delay: float = RESTART_DELAY_S,
    should_restart: Callable[[], bool] | None = None,
) -> asyncio.Task:
    """Run ``factory()`` as a task, rebuilding it whenever it exits for
    any reason other than cancellation.

    ``should_restart`` lets loops with a graceful stop condition (e.g.
    a ``_stop`` event) return normally without being resurrected: it is
    consulted after every exit, and a False ends supervision quietly."""

    async def _runner() -> None:
        while True:
            returned_cleanly = False
            try:
                await factory()
                returned_cleanly = True
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — must log + restart anything
                logger.exception(
                    "background task %r crashed — restarting in %.0fs",
                    name, restart_delay,
                )
            if should_restart is not None and not should_restart():
                return
            if returned_cleanly:
                logger.error(
                    "background task %r returned unexpectedly — restarting in %.0fs",
                    name, restart_delay,
                )
            await asyncio.sleep(restart_delay)

    return asyncio.get_event_loop().create_task(_runner(), name=f"supervised:{name}")
