"""Multi-file 裁決佇列 — DB-only scan of the presence index for codes
holding more than one file, pre-sorted so a human can tell genuine part
sets from competing duplicate copies.

The pipeline already draws the automatic line (video_count / #145): loose
files sharing ONE parent folder are parts of one work, copies living in
DIFFERENT folders are duplicate homes. What it deliberately does NOT do
is judge same-size / same-runtime siblings inside one folder — see
``rename_plan.low_bitrate_copies``: "only a human can say whether the
release really is two discs" (STOL-094). This module feeds that human
queue: group every multi-row code by parent, attach the operator's
stored verdict, and let the /multipart page do the judging.

Same budget rules as ``hygiene.scan``: presence table only, zero PikPak
calls, safe to reload on every page visit. File sizes come lazily from
``GET /presence/codes/{code}/files`` when the operator opens a row.
``strip_singletons`` is the one exception — it lists and renames live,
because it runs right after the operator trashed files.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from ..database import SessionLocal
from ..models import MultipartReview, PresenceEntry
from . import video_count as video_count_svc
from .jav_code import CONTAINER_EXTS, VIDEO_EXTS, ext_of, normalize_code
from .pikpak import pikpak_service
from .rename_plan import has_part_marker

REVIEW_STATUSES = ("confirmed_parts", "resolved_dup")


def _verdict_hint(groups: list[dict]) -> str:
    """The #145 axis, applied to presence rows: one shared parent means
    the siblings are (probably) discs of one release; one file per
    parent means competing whole copies; anything else needs eyes."""
    if len(groups) == 1:
        return "likely_parts"
    if all(len(g["files"]) == 1 for g in groups):
        return "likely_dup_homes"
    return "mixed"


async def scan() -> dict:
    """Bucket every presence row by code and report codes with ≥2 file
    rows, grouped by parent folder. Folder-shaped rows (no video /
    container extension — hygiene's wild wrappers) can hide any number
    of parts behind a single presence row, so they surface separately
    under ``needs_listing`` no matter their row count."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(PresenceEntry.code, PresenceEntry.path)
            )
        ).all()
        reviews = {
            r.code: {
                "status": r.status,
                "note": r.note,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in (
                await session.execute(select(MultipartReview))
            ).scalars()
        }

    by_code: dict[str, list[str]] = {}
    for code, path in rows:
        by_code.setdefault(code, []).append(path)

    items: list[dict] = []
    needs_listing: list[dict] = []
    for code in sorted(by_code):
        paths = sorted(by_code[code])
        file_paths: list[str] = []
        folder_paths: list[str] = []
        for p in paths:
            ext = ext_of(p.rsplit("/", 1)[-1])
            if ext in VIDEO_EXTS or ext in CONTAINER_EXTS:
                file_paths.append(p)
            else:
                folder_paths.append(p)

        if folder_paths:
            needs_listing.append({
                "code": code,
                "paths": folder_paths,
                "review": reviews.get(code),
            })
        if len(file_paths) < 2:
            continue

        by_parent: dict[str, list[dict]] = {}
        for p in file_paths:
            parent, _, name = p.rpartition("/")
            by_parent.setdefault(parent, []).append({
                "path": p,
                "name": name,
                "has_marker": has_part_marker(name, code),
            })
        groups = [
            {"parent": parent, "files": files}
            for parent, files in sorted(by_parent.items())
        ]
        items.append({
            "code": code,
            "file_count": len(file_paths),
            "groups": groups,
            "verdict_hint": _verdict_hint(groups),
            "review": reviews.get(code),
        })

    return {
        "total_rows": len(rows),
        "items": items,
        "needs_listing": needs_listing,
    }


async def set_review(code: str, status: str, note: str = "") -> dict:
    """Upsert the operator's verdict; ``pending`` means 撤銷 and removes
    the row so the code re-enters the queue clean. Returns the stored
    (or cleared) state for the caller to render."""
    canon = normalize_code(code) or (code or "").strip().upper()
    if not canon:
        raise ValueError("無效番號")
    if status == "pending":
        async with SessionLocal() as session:
            await session.execute(
                delete(MultipartReview).where(MultipartReview.code == canon)
            )
            await session.commit()
        return {"code": canon, "review": None}
    if status not in REVIEW_STATUSES:
        raise ValueError(f"status 必須是 {REVIEW_STATUSES + ('pending',)} 之一")
    async with SessionLocal() as session:
        row = await session.get(MultipartReview, canon)
        if row is None:
            row = MultipartReview(code=canon)
            session.add(row)
        row.status = status
        row.note = note or ""
        await session.commit()
        return {
            "code": canon,
            "review": {
                "status": row.status,
                "note": row.note,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            },
        }


async def strip_singletons(codes: list[str]) -> dict:
    """After the operator trashes duplicates, a code left with exactly
    ONE video file that still carries a part marker (``CODE_1.mp4`` and
    friends) violates the archive convention and falsely advertises a
    part set. Strip it back to the bare canonical — the per-code twin of
    ``episode_finder.process_trash_and_strip``'s ``auto_strip``, which
    needs parent folder ids the /multipart page doesn't have.

    Deliberately conservative: any listing marked partial, any count
    other than exactly one, or an already-bare name is a skip, never a
    rename. Returns per-code actions; the router refreshes presence for
    the renamed codes after the settle."""
    results: list[dict] = []
    renamed: list[str] = []
    for raw in codes:
        canon = normalize_code(raw) or (raw or "").strip().upper()
        if not canon:
            continue
        try:
            info = await video_count_svc.files_for_code(canon)
        except Exception as exc:  # noqa: BLE001
            results.append({"code": canon, "action": "error", "detail": str(exc)})
            continue
        files = info.get("files") or [] if info.get("ok") else []
        if info.get("partial"):
            results.append({"code": canon, "action": "skip", "detail": "listing partial"})
            continue
        if len(files) != 1:
            results.append({"code": canon, "action": "skip", "detail": f"{len(files)} files"})
            continue
        name = files[0].get("name") or ""
        if not has_part_marker(name, canon):
            results.append({"code": canon, "action": "skip", "detail": "no marker"})
            continue
        new_name = f"{canon}{ext_of(name)}"
        try:
            await pikpak_service.rename_file(files[0]["id"], new_name)
        except Exception as exc:  # noqa: BLE001
            results.append({"code": canon, "action": "error", "detail": str(exc)})
            continue
        renamed.append(canon)
        results.append(
            {"code": canon, "action": "renamed", "from": name, "to": new_name}
        )
    return {"renamed": renamed, "results": results}
