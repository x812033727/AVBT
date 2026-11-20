"""Which downloaded works belong to a tracked listing (any kind).

Archive folders don't answer this: every work with a studio lands under
``製作商/<studio>/<series>/<code>`` whichever listing triggered it, so a
star / series / label / director has no folder of its own. Membership is
read from the persistent detail cache instead (like ``studio_index``):

- studio / label / series / director → the matching ``LinkRef.id``
- star → any id in ``actresses``

restricted to codes physically on PikPak (``presence_index``). Rows are
read ignoring the detail-cache TTL — a work's cast/studio never changes.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from ..database import SessionLocal
from ..models import MovieDetailCache
from ..schemas import MovieDetail
from .jav_code import normalize_code
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)

KINDS = ("studio", "star", "label", "series", "director")
_TTL_SECONDS = 60.0

# (kind, id) → normalized codes on PikPak
_index: dict[tuple[str, str], set[str]] | None = None
_built_at = 0.0
_lock = asyncio.Lock()


def invalidate() -> None:
    global _built_at
    _built_at = 0.0


def keys_for_detail(detail: MovieDetail) -> list[tuple[str, str]]:
    """Every (kind, id) listing a work belongs to. Pure; unit-tested."""
    out: list[tuple[str, str]] = []
    for kind in ("studio", "label", "series", "director"):
        ref = getattr(detail, kind, None)
        rid = (getattr(ref, "id", "") or "").strip() if ref else ""
        if rid:
            out.append((kind, rid))
    for a in detail.actresses or []:
        aid = (a.id or "").strip()
        if aid:
            out.append(("star", aid))
    return out


async def _build() -> dict[tuple[str, str], set[str]]:
    present = presence_index.peek()
    if present is None:
        try:
            present = await presence_index.get()
        except Exception as exc:  # noqa: BLE001 — presence down → empty index
            logger.warning("listing_codes: presence unavailable: %s", exc)
            return {}
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(MovieDetailCache.code, MovieDetailCache.detail))
        ).all()
    index: dict[tuple[str, str], set[str]] = {}
    for code, detail_json in rows:
        norm = normalize_code(code) or code
        if code not in present and norm not in present:
            continue
        try:
            detail = MovieDetail.model_validate_json(detail_json)
        except Exception:  # noqa: BLE001 — one corrupt row must not kill the index
            continue
        for key in keys_for_detail(detail):
            index.setdefault(key, set()).add(norm)
    return index


async def _get() -> dict[tuple[str, str], set[str]]:
    global _index, _built_at
    if _index is not None and time.monotonic() - _built_at < _TTL_SECONDS:
        return _index
    async with _lock:
        if _index is not None and time.monotonic() - _built_at < _TTL_SECONDS:
            return _index
        _index = await _build()
        _built_at = time.monotonic()
        return _index


async def codes_for_listing(kind: str, listing_id: str) -> set[str]:
    return set((await _get()).get((kind, (listing_id or "").strip()), set()))
