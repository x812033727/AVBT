"""Root logger configuration.

Without this the root logger has no handler, so every ``logger.info``
across the app is dropped and WARNING+ only reaches stderr through
logging's bare lastResort handler (message only — no timestamp, level
or origin). Uvicorn configures just its own ``uvicorn.*`` loggers,
which don't propagate, so calling ``basicConfig`` here neither
duplicates access logs nor is duplicated by them.
"""

from __future__ import annotations

import logging

from .config import settings


def setup_logging() -> None:
    # No-op if the root logger already has handlers (e.g. pytest's
    # caplog or an embedding process configured logging first).
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Per-request noise from the HTTP stack drowns the app's own INFO
    # lines; keep them at WARNING.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
