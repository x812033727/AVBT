import json

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import insert, select

from ..database import SessionLocal
from ..models import OfflineTaskLog
from ..schemas import (
    OfflineSubmit,
    PikPakFile,
    PikPakLogin,
    PikPakQuota,
    PikPakTask,
    PresenceCodeLookup,
    PresenceDetail,
    PresenceStatus,
    ReorganizeOptions,
)
from ..scrapers.javbus import extract_btih
from ..services import archiver
from ..services.jav_code import extract_jav_code, is_video
from ..services.pikpak import PikPakError, pikpak_service
from ..services.pikpak_presence import presence_index
from ..services.reorganize import reorganize_stream


class DuplicateMagnetError(PikPakError):
    """Raised when a magnet hash is already in offline_task_log and the
    caller did not opt in via ``force=true``."""


async def _is_duplicate(magnet: str) -> bool:
    h = extract_btih(magnet)
    if not h:
        return False
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(OfflineTaskLog.id).where(OfflineTaskLog.btih == h).limit(1)
            )
        ).first()
    return row is not None

router = APIRouter(prefix="/api/pikpak", tags=["pikpak"])


def _wrap(exc: Exception) -> HTTPException:
    code = 400 if isinstance(exc, PikPakError) else 502
    return HTTPException(status_code=code, detail=f"PikPak 錯誤: {exc}")


@router.post("/login")
async def login(payload: PikPakLogin):
    try:
        if payload.encoded_token:
            return await pikpak_service.login_with_token(payload.encoded_token)
        return await pikpak_service.login(payload.username, payload.password)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/token")
async def get_token():
    """Expose the currently stored token so the user can copy / back it up."""
    return {"token": pikpak_service.export_token()}


@router.get("/status")
async def status():
    info = pikpak_service.status()
    if info["logged_in"]:
        try:
            quota = await pikpak_service.quota()
            info["quota"] = quota.model_dump()
        except Exception as exc:  # noqa: BLE001
            info["quota_error"] = str(exc)
    return info


@router.post("/logout")
async def logout():
    pikpak_service.logout()
    return {"ok": True}


@router.get("/quota", response_model=PikPakQuota)
async def quota():
    try:
        return await pikpak_service.quota()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/offline", response_model=PikPakTask)
async def offline_download(payload: OfflineSubmit):
    if not payload.force and await _is_duplicate(payload.magnet):
        raise HTTPException(
            status_code=409,
            detail="此磁力已經送過 PikPak。若要再送一次，請帶 force=true。",
        )
    try:
        task = await pikpak_service.offline_download(payload)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    async with SessionLocal() as session:
        await session.execute(
            insert(OfflineTaskLog).values(
                code=payload.code,
                magnet=payload.magnet,
                btih=extract_btih(payload.magnet),
                task_id=task.id,
                file_id=task.file_id or "",
                name=task.name,
                phase=task.phase,
                message=task.message or "",
            )
        )
        await session.commit()

    return task


@router.get("/tasks", response_model=list[PikPakTask])
async def list_tasks(size: int = Query(100, ge=1, le=500)):
    try:
        return await pikpak_service.list_tasks(size=size)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    try:
        return await pikpak_service.retry_task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/tasks/delete")
async def delete_tasks(
    task_ids: list[str] = Body(..., embed=True),
    delete_files: bool = Body(False, embed=True),
):
    try:
        return await pikpak_service.delete_tasks(task_ids, delete_files=delete_files)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


_DEFAULT_FAILED_PHASES = ("PHASE_TYPE_ERROR", "ERROR")


@router.post("/tasks/cleanup-failed")
async def cleanup_failed_tasks(
    include_phases: list[str] | None = Body(None, embed=True),
):
    """Bulk-delete every PikPak offline task whose phase indicates failure.
    Does not delete the underlying files (delete_files=False)."""
    phases = tuple(include_phases) if include_phases else _DEFAULT_FAILED_PHASES
    try:
        tasks = await pikpak_service.list_tasks(size=500)
        failed_ids = [t.id for t in tasks if t.id and t.phase in phases]
        if failed_ids:
            await pikpak_service.delete_tasks(failed_ids, delete_files=False)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return {"deleted": len(failed_ids), "phases": list(phases)}


@router.get("/files", response_model=list[PikPakFile])
async def list_files(parent_id: str = "", size: int = Query(100, ge=1, le=500)):
    try:
        return await pikpak_service.list_files(parent_id=parent_id, size=size)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/files/trash")
async def trash_files(ids: list[str] = Body(..., embed=True)):
    try:
        return await pikpak_service.trash_files(ids)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/files/move")
async def move_files(
    file_ids: list[str] = Body(..., embed=True),
    target_folder_id: str = Body("", embed=True),
):
    """Move one or more files/folders to a target folder. ``target_folder_id``
    may be empty to mean the drive root."""
    if not file_ids:
        raise HTTPException(status_code=400, detail="未指定要移動的檔案")
    try:
        return await pikpak_service.move_files(file_ids, target_folder_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.get("/files/stats")
async def folder_stats(parent_id: str = ""):
    """Aggregate stats for the direct children of ``parent_id``.

    Crosses children's file_ids against ``offline_task_log`` so we can show
    how many of the videos in this folder have been archived already.
    """
    try:
        children, partial = await pikpak_service.list_all_files(parent_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    total_files = 0
    total_folders = 0
    total_size = 0
    video_count = 0
    video_size = 0
    coded_count = 0
    file_ids: list[str] = []
    for c in children:
        if c.kind == "drive#folder":
            total_folders += 1
        else:
            total_files += 1
            total_size += int(c.size or 0)
            if is_video(c.name):
                video_count += 1
                video_size += int(c.size or 0)
            if c.id:
                file_ids.append(c.id)
            if extract_jav_code(c.name):
                coded_count += 1

    archived_count = 0
    if file_ids:
        async with SessionLocal() as session:
            archived_count = len(
                (
                    await session.execute(
                        select(OfflineTaskLog.file_id).where(
                            OfflineTaskLog.file_id.in_(file_ids),
                            OfflineTaskLog.archived.is_(True),
                        )
                    )
                ).scalars().all()
            )

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_size": total_size,
        "video_count": video_count,
        "video_size": video_size,
        "coded_count": coded_count,
        "archived_count": archived_count,
        "partial": partial,
    }


@router.get("/files/{file_id}/url")
async def file_url(file_id: str):
    try:
        links = await pikpak_service.file_links(file_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    # Keep `url` for backward compat — it has always meant the download link.
    return {"url": links["download_url"], **links}


@router.get("/files/search", response_model=list[PikPakFile])
async def search_files(q: str = Query(..., min_length=1), parent_id: str = ""):
    try:
        return await pikpak_service.search_files(q, parent_id=parent_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/share")
async def share_files(
    file_ids: list[str] = Body(..., embed=True),
    need_password: bool = Body(False, embed=True),
    expiration_days: int = Body(-1, embed=True),
):
    try:
        return await pikpak_service.create_share(
            file_ids,
            need_password=need_password,
            expiration_days=expiration_days,
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/files/cleanup/stream")
async def cleanup_folder_stream(payload: dict = Body(...)):
    """Stream cleanup events (NDJSON) for the direct children of a folder.

    Body: ``{folder_id: str, dry_run: bool=True}``.
    Events: ``start`` | ``progress`` | ``done`` | ``error``.
    """
    folder_id = (payload.get("folder_id") or "").strip()
    dry_run = bool(payload.get("dry_run", True))

    async def gen():
        try:
            async for event in pikpak_service.cleanup_folder_stream(
                folder_id, dry_run=dry_run
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/presence/status", response_model=PresenceStatus)
async def presence_status():
    return PresenceStatus(**presence_index.status())


@router.post("/presence/refresh", response_model=PresenceStatus)
async def presence_refresh():
    await presence_index.rebuild()
    return PresenceStatus(**presence_index.status())


@router.get("/presence/detail", response_model=PresenceDetail)
async def presence_detail(refresh: bool = False):
    """Status + which roots were scanned + which leaf folder names didn't
    normalise into a JAV code. Lets the user spot files stored in
    unexpected locations or under odd folder names."""
    if refresh:
        await presence_index.rebuild()
    elif presence_index.status().get("built_at") is None:
        await presence_index.get()
    return PresenceDetail(**presence_index.detail())


@router.get("/presence/codes/{code}", response_model=PresenceCodeLookup)
async def presence_lookup_code(code: str):
    """Return every PikPak folder path the presence index found for
    ``code``. Empty list means the index doesn't see it anywhere."""
    if presence_index.status().get("built_at") is None:
        await presence_index.get()
    return PresenceCodeLookup(code=code, paths=presence_index.paths_for(code))


@router.post("/reorganize")
async def reorganize_endpoint(opts: ReorganizeOptions):
    async def gen():
        try:
            async for event in reorganize_stream(dry_run=opts.dry_run):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/archiver")
async def archiver_status():
    return archiver.state.to_dict()


@router.post("/archiver/run")
async def archiver_run_now():
    moved = await archiver.archive_once()
    return {"moved": moved, **archiver.state.to_dict()}


@router.post("/archiver/toggle")
async def archiver_toggle(enabled: bool = Body(..., embed=True)):
    archiver.state.enabled = enabled
    return archiver.state.to_dict()


@router.post("/offline/bulk", response_model=list[PikPakTask])
async def offline_download_bulk(items: list[OfflineSubmit]):
    # Pre-load every previously-submitted hash once so we can dedup the
    # batch without N round-trips. Reads only the indexed btih column.
    async with SessionLocal() as session:
        sent_hashes = set(
            (
                await session.execute(
                    select(OfflineTaskLog.btih).where(OfflineTaskLog.btih != "")
                )
            ).scalars().all()
        )

    tasks: list[PikPakTask] = []
    for it in items:
        h = extract_btih(it.magnet)
        if not it.force and h and h in sent_hashes:
            tasks.append(
                PikPakTask(
                    id="",
                    name=it.code or it.magnet[:40],
                    phase="DUPLICATE",
                    progress=0,
                    file_id=None,
                    file_size=None,
                    message="已送過，跳過（force=true 可強制送）",
                    created_time=None,
                )
            )
            continue
        try:
            task = await pikpak_service.offline_download(it)
            tasks.append(task)
            if h:
                sent_hashes.add(h)
        except Exception as exc:  # noqa: BLE001
            tasks.append(
                PikPakTask(
                    id="",
                    name=it.code or it.magnet[:40],
                    phase="ERROR",
                    progress=0,
                    file_id=None,
                    file_size=None,
                    message=str(exc),
                    created_time=None,
                )
            )
    return tasks
