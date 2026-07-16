"""Find codes that landed as a disc image / archive instead of a video.

A magnet that turns out to hold ``CODE.iso`` downloads to 100% and looks
like a clean success to every layer of the pipeline: the task completes,
the archiver moves it, finalize renames it, presence records it. Nothing
asks whether the result is playable, so nine of these sat unnoticed for
weeks (SNIS-494.iso at 23.8GB, AP-619.zip …) until the user spotted them.

This detector is the missing question, in two passes. The first scans the
presence index — free, but the index lags reality: it still listed
AP-619.zip and MAS-096.iso after both were gone, and reporting those cost
a pointless download each (live, 2026-07-16). So the handful of candidates
the scan produces are re-read from PikPak before anyone acts on them. That
keeps the cost proportional to how many codes are actually broken (a
listing or two each) rather than to the size of the library, and it heals
the stale rows on the way through.

Acting on the answer (fetch the detail, pick a magnet with a different
btih, submit) stays with the cron worker, which paces JavBus; the tail end
is automatic again, because once the real video lands beside the container
:mod:`series_junk` sees the container is redundant and trashes it.
"""

from __future__ import annotations

import logging
from typing import Any

from .jav_code import CONTAINER_EXTS, ext_of, is_video
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)


def _container_only(paths: list[str]) -> list[str]:
    """The containers in ``paths``, or [] when a playable video is there
    too — a code with both is mid-swap and already solved."""
    if any(is_video(p) for p in paths):
        return []
    return [p for p in paths if ext_of(p) in CONTAINER_EXTS]


async def container_only_codes(*, verify: bool = True) -> list[dict[str, Any]]:
    """Archived codes whose only files are containers — no playable video.

    ``verify=False`` returns the raw index scan, stale rows and all; it
    exists for tests and for callers that only want a cheap estimate.
    """
    codes = await presence_index.get()
    candidates = [c for c in sorted(codes)
                  if _container_only(presence_index.paths_for(c))]
    if not candidates or not verify:
        return [{"code": c, "paths": _container_only(presence_index.paths_for(c))}
                for c in candidates]

    try:
        await presence_index.refresh_codes(candidates)
    except Exception as exc:  # noqa: BLE001 — a stale answer beats no answer
        logger.warning("container-only verify failed, reporting unverified: %s",
                       exc)
    out: list[dict[str, Any]] = []
    for code in candidates:
        containers = _container_only(presence_index.paths_for(code))
        if containers:
            out.append({"code": code, "paths": containers})
        else:
            logger.info("container-only: %s was stale, dropped", code)
    return out
