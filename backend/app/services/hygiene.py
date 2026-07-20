"""Library hygiene scan over the presence index — DB only, zero PikPak
calls, cheap enough for the hourly 輪值 to run every round.

Two defect classes this surfaces (2026-07-20 user report — every issue
that round was one of these, and nothing in the pipeline would ever
have noticed either):

- **wild wrapper** — a presence row whose basename has no video /
  container extension. The presence indexer matched a FOLDER as the
  code's item, which is the signature of an unflattened download
  wrapper sitting in a series folder (live: ``[吾爱GIGA]TRE-76`` held
  four real episodes invisible to every path-based read).
- **nonconforming filename** — a video file whose stem isn't the
  canonical ``CODE`` / ``CODE_N`` / lettered-disc / short-tag shape the
  sweep produces. These never self-heal: the rename planner only strips
  known BT-site noise, so a title-decorated name (``300NTK-836 【…150
  字標題…】.mp4``) survives every sweep until a human looks.

The scan is REPORT-ONLY. Deliberately-kept shapes (composite ``CD1-A``
disc markers, ``-UNCENSORED`` cut tags) are treated as conforming;
``(2)``-suffixed duplicate candidates are NOT, because surfacing those
pending-judgment copies is the point of a hygiene report.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from ..database import SessionLocal
from ..models import PresenceEntry
from .jav_code import CONTAINER_EXTS, VIDEO_EXTS, ext_of

SAMPLE_LIMIT = 30


def classify_basename(code: str, basename: str) -> str | None:
    """Return ``None`` when ``basename`` conforms to the archive naming
    convention for ``code``, else the defect class."""
    ext = ext_of(basename)
    if ext not in VIDEO_EXTS and ext not in CONTAINER_EXTS:
        return "wild_wrapper"
    stem = basename[: -len(ext)]
    cu = code.upper().strip()
    s = stem.upper().strip()
    if s == cu:
        return None
    escaped = re.escape(cu)
    for pattern in (
        rf"{escaped}(_\d{{1,2}}|[A-D])",          # parts / lettered discs
        rf"{escaped}CD\d{{1,2}}([-_]?[A-D])?",    # composite CD1-A markers
        rf"{escaped}[-_][A-Z0-9]{{1,12}}",        # short tags (-UNCENSORED)
    ):
        if re.fullmatch(pattern, s):
            return None
    return "nonconforming"


async def scan() -> dict:
    """Classify every presence row. Returns counts plus capped samples
    so the 輪值 report can quote offenders without flooding."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(PresenceEntry.code, PresenceEntry.path)
            )
        ).all()

    result: dict = {
        "total_rows": len(rows),
        "wild_wrappers": {"count": 0, "samples": []},
        "nonconforming": {"count": 0, "samples": []},
    }
    for code, path in rows:
        basename = path.rsplit("/", 1)[-1]
        defect = classify_basename(code, basename)
        if defect is None:
            continue
        key = "wild_wrappers" if defect == "wild_wrapper" else "nonconforming"
        bucket = result[key]
        bucket["count"] += 1
        if len(bucket["samples"]) < SAMPLE_LIMIT:
            bucket["samples"].append({"code": code, "path": path})
    return result
