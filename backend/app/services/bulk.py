"""Bulk-submit every movie of a JavBus actress / genre to PikPak.

Provides an async-generator API (``send_all_stream``) so the HTTP layer
can stream progress events to the browser as NDJSON, plus a convenience
wrapper (``send_all``) that drains the generator and returns the final
summary for non-streaming callers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable

from sqlalchemy import insert, select

from ..database import SessionLocal
from ..models import OfflineTaskLog
from ..schemas import OfflineSubmit, SendAllOptions, SendAllResult
from ..scrapers import javbus as scraper
from ..scrapers.javbus import extract_btih, pick_best_magnet
from .pikpak import pikpak_service

logger = logging.getLogger(__name__)


async def _collect_codes(*, kind: str, slug: str, options: SendAllOptions) -> list[str]:
    if kind == "star":
        fetch = scraper.fetch_star
    elif kind == "genre":
        fetch = scraper.fetch_genre
    else:
        raise ValueError(f"unknown listing kind: {kind}")

    codes: list[str] = []
    seen: set[str] = set()
    for page in range(1, max(1, options.max_pages) + 1):
        result = await fetch(slug, page=page, uncensored=options.uncensored)
        for item in result.items:
            if item.code and item.code not in seen:
                seen.add(item.code)
                codes.append(item.code)
        if not result.has_next:
            break
    return codes


async def _load_sent_hashes() -> set[str]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(OfflineTaskLog.magnet))).scalars().all()
    return {h for m in rows if (h := extract_btih(m or ""))}


async def _process_code(
    code: str,
    *,
    options: SendAllOptions,
    sent_hashes: set[str],
    result: SendAllResult,
    lock: asyncio.Lock,
) -> dict:
    """Process one movie. Returns a dict describing what happened."""
    try:
        detail = await scraper.fetch_detail(code)
    except Exception as exc:  # noqa: BLE001
        async with lock:
            result.failed += 1
            result.errors.append(f"{code}: 抓取詳細頁失敗 ({exc})")
        return {"status": "failed", "message": str(exc)}

    if not detail.magnets:
        async with lock:
            result.skipped_no_magnet += 1
        return {"status": "skipped_no_magnet"}

    async with lock:
        best = pick_best_magnet(
            detail.magnets,
            hd_only=options.hd_only,
            subtitle_only=options.subtitle_only,
            skip_hashes=sent_hashes if options.skip_sent else set(),
        )
    if best is None:
        any_already = any(
            extract_btih(m.link) in sent_hashes for m in detail.magnets
        )
        async with lock:
            if options.skip_sent and any_already:
                result.skipped_already_sent += 1
                return {"status": "skipped_already_sent"}
            result.skipped_no_magnet += 1
            return {"status": "skipped_no_magnet"}

    try:
        task = await pikpak_service.offline_download(
            OfflineSubmit(magnet=best.link, code=code, folder=options.folder)
        )
    except Exception as exc:  # noqa: BLE001
        async with lock:
            result.failed += 1
            result.errors.append(f"{code}: 送 PikPak 失敗 ({exc})")
        return {"status": "failed", "message": str(exc)}

    async with SessionLocal() as session:
        await session.execute(
            insert(OfflineTaskLog).values(
                code=code,
                magnet=best.link,
                task_id=task.id,
                file_id=task.file_id or "",
                name=task.name,
                phase=task.phase,
                message=task.message or "",
            )
        )
        await session.commit()

    h = extract_btih(best.link)
    async with lock:
        if h:
            sent_hashes.add(h)
        result.sent += 1
    return {
        "status": "sent",
        "magnet_name": best.name,
        "size": best.size,
        "is_hd": best.is_hd,
        "has_subtitle": best.has_subtitle,
    }


async def send_codes_stream(
    codes: list[str],
    options: SendAllOptions,
    *,
    on_sent: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncIterator[dict]:
    """Process a precomputed list of codes. The on_sent hook fires after
    each successful submission (e.g. to update CollectedMovie status)."""
    yield {
        "type": "start",
        "total": len(codes),
        "preview": codes[:8] + (["…"] if len(codes) > 8 else []),
    }

    if not codes:
        yield {"type": "done", "result": SendAllResult().model_dump()}
        return

    sent_hashes = await _load_sent_hashes() if options.skip_sent else set()
    result = SendAllResult(total_movies=len(codes))
    queue: asyncio.Queue = asyncio.Queue()
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(3)

    async def worker(idx: int, code: str) -> None:
        async with sem:
            try:
                event = await _process_code(
                    code,
                    options=options,
                    sent_hashes=sent_hashes,
                    result=result,
                    lock=lock,
                )
            except Exception as exc:  # noqa: BLE001
                async with lock:
                    result.failed += 1
                    result.errors.append(f"{code}: {exc}")
                event = {"status": "failed", "message": str(exc)}
            if event.get("status") == "sent" and on_sent is not None:
                try:
                    await on_sent(code)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("on_sent hook failed for %s: %s", code, exc)
            await queue.put({"type": "progress", "current": idx + 1, "code": code, **event})

    tasks = [asyncio.create_task(worker(i, c)) for i, c in enumerate(codes)]
    try:
        for _ in range(len(codes)):
            yield await queue.get()
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise
    await asyncio.gather(*tasks, return_exceptions=True)
    yield {"type": "done", "result": result.model_dump()}


async def send_all_stream(
    kind: str, slug: str, options: SendAllOptions
) -> AsyncIterator[dict]:
    """Walk JavBus listing pages first, then stream submissions."""
    codes = await _collect_codes(kind=kind, slug=slug, options=options)
    async for event in send_codes_stream(codes, options):
        yield event


async def send_all(kind: str, slug: str, options: SendAllOptions) -> SendAllResult:
    """Drain the stream and return the final summary."""
    final = SendAllResult()
    async for event in send_all_stream(kind, slug, options):
        if event["type"] == "done":
            final = SendAllResult(**event["result"])
    return final
