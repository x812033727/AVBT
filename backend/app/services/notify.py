"""Tiny webhook fan-out. POSTs `{"content": "..."}` which is the format
both Discord webhooks and most bridges accept."""

from __future__ import annotations

import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


async def send_webhook(message: str) -> None:
    if not settings.webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(settings.webhook_url, json={"content": message})
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook failed: %s", exc)
