"""Notification fan-out: generic webhook + Telegram.

``send_webhook`` POSTs `{"content": "..."}` which is the format both
Discord webhooks and most bridges accept. ``send_telegram`` pushes the
same text through the Bot API. ``send_notification`` is the high-level
entry: it checks the per-event toggle (AppMeta override → config
default) and fans out to every configured channel."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Known notification event keys → config default attribute.
EVENT_DEFAULTS = {
    "tracked_new": "notify_tracked_new",
    "archive_done": "notify_archive_done",
    "archive_failed": "notify_archive_failed",
    "download_failed": "notify_download_failed",
    "scraper_alert": "notify_scraper_alert",
    "backup_failed": "notify_backup_failed",
    "duplicates_found": "notify_duplicates_found",
    "transfer_done": "notify_transfer_done",
    "transfer_failed": "notify_transfer_failed",
}


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


async def _post_with_retry(url: str, json_body: dict, channel: str) -> bool:
    try:
        cli = await _get_client()
        resp = await cli.post(url, json=json_body)
        return resp.status_code < 400
    except (httpx.PoolTimeout, httpx.ReadError):
        # Pool got stuck — recycle and retry once.
        await aclose_client()
        try:
            cli = await _get_client()
            resp = await cli.post(url, json=json_body)
            return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s failed: %s", channel, exc)
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed: %s", channel, exc)
        return False


async def send_webhook(message: str) -> bool:
    if not settings.webhook_url:
        return False
    return await _post_with_retry(settings.webhook_url, {"content": message}, "webhook")


def telegram_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def send_telegram(message: str) -> bool:
    if not telegram_configured():
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body = {"chat_id": settings.telegram_chat_id, "text": message}
    return await _post_with_retry(url, body, "telegram")


async def event_enabled(event: str) -> bool:
    """Per-event toggle: AppMeta ``notify:<event>`` (set from the
    settings page) overrides the config default. Unknown events are
    always sent — better a stray message than a silently-dropped one."""
    attr = EVENT_DEFAULTS.get(event)
    if attr is None:
        return True
    # Local import: notify is imported by low-level modules and must not
    # drag the DB engine in at import time.
    from ..database import SessionLocal
    from ..models import AppMeta

    try:
        async with SessionLocal() as session:
            row = await session.get(AppMeta, f"notify:{event}")
        if row is not None and row.value in ("0", "1"):
            return row.value == "1"
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify toggle lookup failed for %s: %s", event, exc)
    return bool(getattr(settings, attr, True))


async def send_notification(message: str, event: str = "generic") -> dict[str, bool]:
    """Fan a message out to every configured channel, honoring the
    per-event toggle. Returns per-channel success for the test button."""
    if not await event_enabled(event):
        return {}
    results: dict[str, bool] = {}
    if settings.webhook_url:
        results["webhook"] = await send_webhook(message)
    if telegram_configured():
        results["telegram"] = await send_telegram(message)
    return results
