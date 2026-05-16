"""Bulk-submit every movie of a JavBus actress / genre to PikPak."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import insert, select

from ..database import SessionLocal
from ..models import OfflineTaskLog
from ..schemas import OfflineSubmit, SendAllOptions, SendAllResult
from ..scrapers import javbus as scraper
from ..scrapers.javbus import extract_btih, pick_best_magnet
from .pikpak import pikpak_service

logger = logging.getLogger(__name__)


async def _collect_codes(
    *, kind: str, slug: str, options: SendAllOptions
) -> list[str]:
    """Walk pages of /star or /genre and return a list of movie codes."""
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
) -> None:
    try:
        detail = await scraper.fetch_detail(code)
    except Exception as exc:  # noqa: BLE001
        async with lock:
            result.failed += 1
            result.errors.append(f"{code}: 抓取詳細頁失敗 ({exc})")
        return

    if not detail.magnets:
        async with lock:
            result.skipped_no_magnet += 1
        return

    async with lock:
        best = pick_best_magnet(
            detail.magnets,
            hd_only=options.hd_only,
            subtitle_only=options.subtitle_only,
            skip_hashes=sent_hashes if options.skip_sent else set(),
        )
    if best is None:
        # Either filtered to nothing or every candidate is already sent.
        any_already = any(
            extract_btih(m.link) in sent_hashes for m in detail.magnets
        )
        async with lock:
            if options.skip_sent and any_already:
                result.skipped_already_sent += 1
            else:
                result.skipped_no_magnet += 1
        return

    try:
        task = await pikpak_service.offline_download(
            OfflineSubmit(magnet=best.link, code=code, folder=options.folder)
        )
    except Exception as exc:  # noqa: BLE001
        async with lock:
            result.failed += 1
            result.errors.append(f"{code}: 送 PikPak 失敗 ({exc})")
        return

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


async def send_all(
    kind: str, slug: str, options: SendAllOptions
) -> SendAllResult:
    codes = await _collect_codes(kind=kind, slug=slug, options=options)
    sent_hashes = await _load_sent_hashes() if options.skip_sent else set()
    result = SendAllResult(total_movies=len(codes))
    if not codes:
        return result

    lock = asyncio.Lock()
    sem = asyncio.Semaphore(3)

    async def guarded(code: str) -> None:
        async with sem:
            await _process_code(
                code,
                options=options,
                sent_hashes=sent_hashes,
                result=result,
                lock=lock,
            )

    await asyncio.gather(*(guarded(c) for c in codes))
    return result
