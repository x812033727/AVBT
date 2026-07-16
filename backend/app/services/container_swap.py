"""Find codes that landed as a disc image / archive instead of a video.

A magnet that turns out to hold ``CODE.iso`` downloads to 100% and looks
like a clean success to every layer of the pipeline: the task completes,
the archiver moves it, finalize renames it, presence records it. Nothing
asks whether the result is playable, so nine of these sat unnoticed for
weeks (SNIS-494.iso at 23.8GB, AP-619.zip …) until the user spotted them.

This detector is the missing question. It reads the presence index only —
no PikPak call, no JavBus fetch — so it is cheap enough to poll. Acting on
the answer (fetch the detail, pick a magnet with a different btih, submit)
stays with the cron worker, which paces JavBus; the tail end is automatic
again, because once the real video lands beside the container
:mod:`series_junk` sees the container is redundant and trashes it.
"""

from __future__ import annotations

from typing import Any

from .jav_code import CONTAINER_EXTS, ext_of, is_video
from .pikpak_presence import presence_index


async def container_only_codes() -> list[dict[str, Any]]:
    """Archived codes whose only files are containers — no playable video.

    A code with both (mid-swap: the replacement has landed, the container
    is awaiting its sweep) is not reported: it is already solved.
    """
    codes = await presence_index.get()
    out: list[dict[str, Any]] = []
    for code in sorted(codes):
        paths = presence_index.paths_for(code)
        if any(is_video(p) for p in paths):
            continue
        containers = [p for p in paths if ext_of(p) in CONTAINER_EXTS]
        if containers:
            out.append({"code": code, "paths": containers})
    return out
