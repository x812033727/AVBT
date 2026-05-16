"""Tiny webhook fan-out. POSTs `{"content": "..."}` which is the format
both Discord webhooks and most bridges accept."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


_client_singleton: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client_singleton
    if _client_singleton is None or _client_singleton.is_closed:
        async with _client_lock:
            if _client_singleton is None or _client_singleton.is_closed:
                _client_singleton = httpx.AsyncClient(timeout=10)
    return _client_singleton


async def aclose_client() -> None:
    global _client_singleton
    cli, _client_singleton = _client_singleton, None
    if cli is not None and not cli.is_closed:
        try:
            await cli.aclose()
        except Exception:  # noqa: BLE001
            pass


async def send_webhook(message: str) -> None:
    if not settings.webhook_url:
        return
    try:
        cli = await _get_client()
        await cli.post(settings.webhook_url, json={"content": message})
    except (httpx.PoolTimeout, httpx.ReadError):
        # Pool got stuck — recycle and retry once.
        await aclose_client()
        try:
            cli = await _get_client()
            await cli.post(settings.webhook_url, json={"content": message})
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook failed: %s", exc)
