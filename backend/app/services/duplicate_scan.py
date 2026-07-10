"""Scheduled PikPak↔pCloud duplicate scan.

The /duplicates page can find codes present on both clouds, but only
when the user remembers to run it — duplicates otherwise accumulate
silently. This loop (default OFF — it walks both cloud trees, so it's
opt-in via ``DUPLICATES_SCAN_ENABLED``) reruns the same read-only scan
periodically and pushes a notification when anything is found. It never
deletes; acting on the result stays a human decision on /duplicates.

Follows auto_backup's shape: sleep-first hourly-granularity loop,
last-run status in app_meta for the settings page.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import settings
from ..database import SessionLocal
from ..models import AppMeta
from .duplicates import find_duplicates_stream
from .webhook_queue import webhook_queue

logger = logging.getLogger(__name__)

_META_KEY = "duplicates_scan:last"
_PREVIEW_CODES = 8


async def run_scan() -> dict:
    """One full scan. Returns the stream's final result dict; raises on
    a scan that errored before producing one."""
    result: dict | None = None
    async for event in find_duplicates_stream(
        settings.duplicates_scan_pikpak_folder,
        settings.duplicates_scan_pcloud_folder or "0",
    ):
        etype = event.get("type") or event.get("kind")
        if etype == "error":
            raise RuntimeError(str(event.get("message") or "掃描失敗"))
        if etype == "done":
            result = event.get("result") or {}
    if result is None:
        raise RuntimeError("掃描未產生結果")

    dupes = result.get("duplicates") or []
    if dupes:
        codes = [d.get("code", "?") for d in dupes]
        preview = "、".join(codes[:_PREVIEW_CODES])
        more = f" 等 {len(codes)} 個" if len(codes) > _PREVIEW_CODES else ""
        webhook_queue.enqueue_nowait(
            f"🔁 定期重複掃描:PikPak 與 pCloud 同時存在 {preview}{more}"
            "——到「重複」頁面檢視與處理。",
            event="duplicates_found",
        )
    await _record(f"ok:{len(dupes)} duplicates")
    return result


async def _record(value: str) -> None:
    try:
        async with SessionLocal() as session:
            row = await session.get(AppMeta, _META_KEY)
            if row is None:
                row = AppMeta(key=_META_KEY)
                session.add(row)
            row.value = value
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — status record is best-effort
        logger.warning("duplicate scan status record failed: %s", exc)


async def run_loop() -> None:
    """Sleep-first so a boot loop can't hammer both cloud APIs."""
    while True:
        interval = max(1, settings.duplicates_scan_interval_hours) * 3600
        await asyncio.sleep(interval)
        if not settings.duplicates_scan_enabled:
            continue
        try:
            result = await run_scan()
            logger.info(
                "scheduled duplicate scan done: %d duplicates",
                len(result.get("duplicates") or []),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduled duplicate scan failed")
            await _record(f"error:{exc}")
