"""Notification settings + test endpoint.

Channel credentials (webhook URL, Telegram bot token/chat id) stay
env-only like every other secret; this router only exposes whether
they're configured, the per-event toggles (persisted in app_meta so
they survive restarts without a .env edit), and a test-fire button.
"""

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_session
from ..models import AppMeta
from ..services.notify import (
    EVENT_DEFAULTS,
    event_enabled,
    send_notification,
    telegram_configured,
)

router = APIRouter(prefix="/api/notify", tags=["notify"])


@router.get("/settings")
async def get_settings():
    toggles = {event: await event_enabled(event) for event in EVENT_DEFAULTS}
    return {
        "webhook_configured": bool(settings.webhook_url),
        "telegram_configured": telegram_configured(),
        "toggles": toggles,
    }


@router.post("/settings")
async def update_settings(
    toggles: dict[str, bool] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
):
    applied: dict[str, bool] = {}
    for event, enabled in toggles.items():
        if event not in EVENT_DEFAULTS:
            continue
        key = f"notify:{event}"
        row = await session.get(AppMeta, key)
        if row is None:
            row = AppMeta(key=key)
            session.add(row)
        row.value = "1" if enabled else "0"
        applied[event] = bool(enabled)
    await session.commit()
    return {"ok": True, "applied": applied}


@router.post("/test")
async def send_test():
    """Fire a test message through every configured channel, bypassing
    the event toggles, and report per-channel success so a bad bot token
    surfaces immediately."""
    results = await send_notification("🔔 AVBT 測試通知", event="__test__")
    if not results:
        return {"ok": False, "results": {}, "message": "沒有設定任何通知管道"}
    return {"ok": all(results.values()), "results": results}
