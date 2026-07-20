import asyncio
import json
import logging

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from ..database import SessionLocal
from ..models import OfflineTaskLog
from ..schemas import (
    FinalizeOptions,
    OfflineSubmit,
    PikPakFile,
    PikPakLogin,
    PikPakQuota,
    PikPakTask,
    PresenceCodeFiles,
    PresenceCodeLookup,
    PresenceDetail,
    PresenceStatus,
    ReorganizeOptions,
    SendAllOptions,
    VideoCountRequest,
    VideoCountResponse,
    VideoCountResult,
)
from ..services import archiver, episode_finder
from ..services import container_swap as container_swap_svc
from ..services import dup_copies as dup_copies_svc
from ..services import folder_twins as folder_twins_svc
from ..services import series_junk as series_junk_svc
from ..services import video_count as video_count_svc
from ..services.download_queue import Job, download_queue
from ..services.finalize import finalize_code_folder_stream as finalize_stream_svc
from ..services.jav_code import extract_jav_code, is_video
from ..services.pikpak import ACTIVE_PHASES, PikPakError, pikpak_service
from ..services.pikpak_presence import presence_index
from ..services.reorganize import reorganize_stream

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
    """Single-magnet submit. Routes through the global download queue so
    it serialises against tracker auto-sends and bulk send-alls instead
    of competing with them."""
    job = Job(
        code=payload.code,
        options=SendAllOptions(folder=payload.folder),
        source="manual",
        direct_magnet=payload.magnet,
        folder=payload.folder,
        force=payload.force,
    )
    fut = await download_queue.enqueue(job)
    result = await fut
    if result.status == "skipped_already_sent":
        raise HTTPException(
            status_code=409,
            detail=result.message
            or "此磁力已經送過 PikPak。若要再送一次，請帶 force=true。",
        )
    if result.status == "failed":
        raise HTTPException(
            status_code=502, detail=f"PikPak 錯誤: {result.message}"
        )
    return PikPakTask(
        id=result.task_id,
        name=result.magnet_name or payload.code or payload.magnet[:40],
        phase="PHASE_TYPE_PENDING",
        progress=0,
    )


@router.get("/tasks", response_model=list[PikPakTask])
async def list_tasks(size: int = Query(100, ge=1, le=500)):
    # This endpoint is the operator's in-flight view (DB `pending` has
    # long since drifted), so it must report what _active_task_ids sees.
    # Without an explicit filter pikpakapi returns RUNNING+ERROR only,
    # which hid a 52-deep PENDING queue behind PikPak's ~100-task cap and
    # made queued tasks read as dead — the exact misread that got
    # MMGO-005's replacement resent while it was still waiting its turn.
    try:
        return await pikpak_service.list_tasks(size=size, phases=ACTIVE_PHASES)
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
    """Delete offline tasks and say what actually happened.

    PikPak's delete API answers null whether the ids existed or not, so
    a truncated/wrong id used to read as success (live 2026-07-17 r40:
    a display-layer 14-char id "deleted" a task that stayed put). The
    task list before/after is the evidence the null isn't: ``deleted``
    ids were listed and are gone, ``not_found`` ids were never listed
    (bad id — or an already-finished task PikPak no longer lists),
    ``still_listed`` ids survived the call."""
    try:
        before = {t.id for t in await pikpak_service.list_tasks(size=500) if t.id}
        await pikpak_service.delete_tasks(task_ids, delete_files=delete_files)
        after = {t.id for t in await pikpak_service.list_tasks(size=500) if t.id}
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return {
        "deleted": [i for i in task_ids if i in before and i not in after],
        "not_found": [i for i in task_ids if i not in before],
        "still_listed": [i for i in task_ids if i in after],
    }


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


# PikPak listings are eventually consistent — refreshing a code right
# after its rename can still see the old leaf, reproduce the cached
# paths, and (via the no-change check) strand the stale entry for good.
# A short settle before the re-read makes the self-heal actually land.
_RENAME_PRESENCE_REFRESH_DELAY = 15.0


async def _refresh_presence_after_rename(code: str) -> None:
    try:
        await asyncio.sleep(_RENAME_PRESENCE_REFRESH_DELAY)
        changed = await presence_index.refresh_codes([code])
        if changed:
            from ..services import missing as missing_svc
            await missing_svc.invalidate_all_caches_async()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "presence refresh after rename %s failed: %s", code, exc
        )


@router.post("/files/rename")
async def rename_file(
    file_id: str = Body(..., embed=True),
    new_name: str = Body(..., embed=True),
):
    """Rename a single file or folder in place (admin/maintenance use —
    keeps manual reorganisation on the server's persistent PikPak login
    instead of one-off client logins that trip the login throttle).

    Self-heals the presence index for the NEW name's code after a short
    settle (a rename otherwise leaves that code's rows stale until it
    lands again). A rename that CHANGES the code only refreshes the new
    one — clear the old code via POST /presence/refresh-codes."""
    if not file_id or not new_name.strip():
        raise HTTPException(status_code=400, detail="缺少 file_id 或 new_name")
    try:
        result = await pikpak_service.rename_file(file_id, new_name.strip())
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    code = extract_jav_code(new_name)
    if code:
        asyncio.create_task(_refresh_presence_after_rename(code))
    return result


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
        resp = await pikpak_service.move_files(file_ids, target_folder_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    # Every other mover stamps the moved ids so the empty-shell /
    # emptied-source trash gates wait out the settle window; this
    # human-triggered endpoint was the last unstamped one (#213 review).
    for fid in file_ids:
        pikpak_service.record_move_source(fid)
    return resp


@router.post("/files/mkdir")
async def make_folder_path(path: str = Body(..., embed=True)):
    """Resolve-or-create a nested folder path (e.g. ``AVBT/製作商/GIGA/
    ヒロイン陵辱``) on the server's persistent login and return its id.

    Exists because the cleanup misplacement pass deliberately never
    creates folders (a missing target reads as not-misplaced), so a
    misfiled 番號 whose correct 製作商 folder doesn't exist yet is
    unreachable by every existing endpoint (live case: KK-078 stuck in
    SODクリエイト because グローリークエスト had no folder). Reuses
    ``folder_id`` — twin-aware via folder_key and serialised under the
    create lock, so it cannot fork duplicate folders."""
    cleaned = path.strip().strip("/")
    if not cleaned:
        raise HTTPException(status_code=400, detail="缺少 path")
    try:
        folder_id = await pikpak_service.folder_id(cleaned)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    if not folder_id:
        raise HTTPException(status_code=502, detail="建立資料夾失敗")
    return {"id": folder_id, "path": cleaned}


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


@router.post("/files/video-count", response_model=VideoCountResponse)
async def files_video_count(payload: VideoCountRequest):
    """Batch「這部有幾個影片檔?」lookup. Each item resolves by
    ``file_id`` (pre-archive task content) or by ``code`` (post-archive,
    via the presence index). Duplicated targets within one request are
    resolved once; PikPak calls are throttled by a small semaphore."""
    sem = asyncio.Semaphore(3)

    async def resolve(item) -> dict:
        async with sem:
            try:
                if item.provider == "pcloud":
                    if item.file_id:
                        return {"ok": False, "error": "pCloud 只支援以番號查詢"}
                    return await video_count_svc.count_for_code_pcloud(item.code)
                if item.file_id:
                    return await video_count_svc.count_for_file_id(item.file_id)
                return await video_count_svc.count_for_code(item.code)
            except Exception as exc:  # noqa: BLE001 — one bad item must not fail the batch
                return {"ok": False, "error": str(exc)}

    def target_of(item) -> tuple[str, str, str]:
        kind, value = ("f", item.file_id) if item.file_id else ("c", item.code.strip().upper())
        return (item.provider, kind, value)

    # Dedupe identical targets so a page of rows for the same code costs
    # one PikPak round-trip.
    unique: dict[tuple[str, str, str], asyncio.Task] = {}
    for item in payload.items:
        target = target_of(item)
        if target not in unique:
            unique[target] = asyncio.create_task(resolve(item))
    try:
        await asyncio.gather(*unique.values())
    except asyncio.CancelledError:
        for t in unique.values():
            t.cancel()
        raise

    results = []
    for item in payload.items:
        res = unique[target_of(item)].result()
        results.append(VideoCountResult(key=item.key, **res))
    return VideoCountResponse(results=results)


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


@router.post("/files/episodes/scan/stream")
async def episodes_scan_stream(payload: dict = Body(...)):
    """Recursively walk ``folder_id`` and stream every file that looks
    like a multi-part episode. Read-only — no mutations.

    Body: ``{folder_id: str, max_depth?: int=8, cap?: int=20000}``.
    """
    folder_id = (payload.get("folder_id") or "").strip()
    max_depth = int(payload.get("max_depth") or 8)
    cap = int(payload.get("cap") or 20000)

    async def gen():
        try:
            async for event in episode_finder.walk_for_episodes(
                pikpak_service, folder_id, max_depth=max_depth, cap=cap
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/files/episodes/process/stream")
async def episodes_process_stream(payload: dict = Body(...)):
    """Trash a list of episode files, then (optionally) strip ``_N``
    markers from any code that becomes a singleton in the affected
    parent folders.

    Body: ``{file_ids_to_trash: list[str], parent_ids_touched: list[str],
            auto_strip: bool=True}``.
    """
    file_ids = list(payload.get("file_ids_to_trash") or [])
    parent_ids = list(payload.get("parent_ids_touched") or [])
    auto_strip = bool(payload.get("auto_strip", True))

    async def gen():
        try:
            async for event in episode_finder.process_trash_and_strip(
                pikpak_service,
                file_ids_to_trash=file_ids,
                parent_ids_touched=parent_ids,
                auto_strip=auto_strip,
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/files/cleanup/stream")
async def cleanup_folder_stream(payload: dict = Body(...)):
    """Stream cleanup events (NDJSON) for a folder and its sub-folders.

    Recursively descends grouping folders (製作商/廠商/系列) to reach 番號
    leaves, moves misplaced 番號 to their correct 製作商/<studio>/<系列>/
    location, normalises/flattens them, and trashes folders left empty.

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
                # Private recursion sentinels (``_became_empty``) never
                # leave the stream.
                if str(event.get("type", "")).startswith("_"):
                    continue
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/finalize/stream")
async def finalize_code_stream(opts: FinalizeOptions):
    """Stream finalize events (NDJSON) for one 番號's archive folder:
    keep only the substantial videos renamed to ``CODE.ext`` /
    ``CODE_N.ext``, permanently purge ad clips / non-video junk, trash
    (recoverable) resolution duplicates and emptied wrappers.

    Body: ``{code: str, dry_run: bool=True}``.
    Events: ``start`` | ``progress`` | ``warn`` | ``done`` | ``error``.
    """
    code = opts.code.strip()
    if not code:
        raise HTTPException(status_code=422, detail="code 不可空白")

    async def gen():
        try:
            async for event in finalize_stream_svc(
                pikpak_service, code, dry_run=opts.dry_run
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/container-only")
async def container_only(verify: bool = Query(True)):
    """Codes archived as a disc image / archive with no playable video —
    the swap worklist. Scans the presence index, then re-reads just the
    candidates from PikPak so a stale row can't cost a pointless
    download. ``verify=false`` skips that (index only, no PikPak call)."""
    return await container_swap_svc.container_only_codes(verify=verify)


@router.get("/presence/status", response_model=PresenceStatus)
async def presence_status():
    return PresenceStatus(**presence_index.status())


@router.post("/presence/refresh", response_model=PresenceStatus)
async def presence_refresh():
    await presence_index.rebuild(force=True)
    # The presence set changed (or may have); drop the cached
    # missing-summary so the /tracked badges reflect the rebuild.
    # presence=False because rebuild() already replaced the index.
    from ..services import missing as missing_svc
    missing_svc.invalidate_all_caches()
    return PresenceStatus(**presence_index.status())


@router.post("/presence/refresh-codes")
async def presence_refresh_codes(codes: list[str] = Body(..., embed=True)):
    """Re-read just these codes' archive folders (1-2 listings each).
    The escape hatch for hand-fixed files: renaming or trashing outside
    the pipeline leaves those codes' rows stale forever, because
    ``refresh_codes`` otherwise only runs when a code lands again. A
    full rebuild stays on /presence/refresh."""
    if not codes:
        raise HTTPException(status_code=400, detail="codes must not be empty")
    if len(codes) > 50:
        raise HTTPException(
            status_code=400,
            detail="at most 50 codes per call; use /presence/refresh for a full walk",
        )
    changed = await presence_index.refresh_codes(codes)
    if changed:
        from ..services import missing as missing_svc
        missing_svc.invalidate_all_caches()
    return {"requested": len(codes), "changed": changed}


@router.get("/presence/detail", response_model=PresenceDetail)
async def presence_detail(refresh: bool = False):
    """Status + which roots were scanned + which leaf folder names didn't
    normalise into a JAV code. Lets the user spot files stored in
    unexpected locations or under odd folder names."""
    if refresh:
        await presence_index.rebuild(force=True)
        from ..services import missing as missing_svc
        missing_svc.invalidate_all_caches()
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


@router.get("/presence/codes/{code}/files", response_model=PresenceCodeFiles)
async def presence_code_files(code: str):
    """Playable PikPak video files for a code. Each entry's ``id`` feeds
    ``GET /api/pikpak/files/{id}/url`` for a streaming link."""
    try:
        result = await video_count_svc.files_for_code(code)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "找不到影片"))
    return PresenceCodeFiles(
        code=result["code"],
        files=result["files"],
        partial=bool(result.get("partial")),
        source=result.get("source", ""),
    )


@router.post("/reorganize")
async def reorganize_endpoint(opts: ReorganizeOptions):
    async def gen():
        try:
            async for event in reorganize_stream(
                dry_run=opts.dry_run, rehome_kinds=opts.rehome_kinds
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/archiver")
async def archiver_status():
    async with SessionLocal() as session:
        abandoned_total = (
            await session.execute(
                select(func.count())
                .select_from(OfflineTaskLog)
                .where(OfflineTaskLog.abandoned.is_(True))
            )
        ).scalar_one()
    return {
        **archiver.state.to_dict(),
        "abandoned_total": int(abandoned_total),
        "throttle_backoff_total": pikpak_service.throttle_backoff_total,
        "throttle_exhausted_total": pikpak_service.throttle_exhausted_total,
    }


@router.post("/archiver/run")
async def archiver_run_now():
    moved = await archiver.archive_once()
    return {"moved": moved, **archiver.state.to_dict()}


@router.post("/folder-twins/merge")
async def merge_folder_twins_now(dry_run: bool = True):
    """Merge series folders that are the same series twice — from name
    drift, or from two archives racing to create the same missing path
    (PikPak allows duplicate folder names). A path resolves to exactly one
    of them, so the other's files are invisible to every path-based read:
    MAS-096.iso sat in a 1-file duplicate and was reported as not
    archived. Biggest folder wins; emptied shells wait out the 30-minute
    move-settle gate before they go."""
    async def gen():
        try:
            async for event in folder_twins_svc.merge_folder_twins_stream(
                pikpak_service, dry_run=dry_run
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/dup-copies/sweep")
async def sweep_dup_copies_now(dry_run: bool = True):
    """Retire the CODE(1).mp4 leftovers an upgrade download leaves beside
    CODE.mp4 — keep the biggest, trash the rest. Recoverable: everything
    goes to the PikPak trash. Real discs (CODE_1/CODE_2) are never
    touched; the (N) suffix is PikPak's own name-collision marker, so it
    only ever appears when two files claimed the same identity."""
    async def gen():
        try:
            async for event in dup_copies_svc.sweep_dup_copies_stream(
                pikpak_service, dry_run=dry_run
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/series-junk/purge")
async def purge_series_junk_now(dry_run: bool = True):
    """Trash the BT junk (ad clips / stray non-video) sitting loose in
    the 系列 folders. finalize only purges inside a 番號 folder, so junk
    that rode along into the flattened layout had no owner. Recoverable:
    everything goes to the PikPak trash."""
    async def gen():
        try:
            async for event in series_junk_svc.purge_series_junk_stream(
                pikpak_service, dry_run=dry_run
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/archiver/sweep")
async def archiver_sweep_now(cleanup_all: bool = True):
    """Force a TASK-folder sweep regardless of the cooldown.

    ``cleanup_all=True`` (the default for the button-triggered call)
    also runs phase-2 cleanup on every tracked series folder, so a
    single click normalises stale BT-prefix names / CD<n> markers /
    half-flattened wrappers across the whole library. Pass
    ``cleanup_all=false`` to do just the move pass."""
    moved = await archiver._sweep_root_once(cleanup_all_targets=cleanup_all)
    return {"moved": moved, **archiver.state.to_dict()}


@router.post("/archiver/sweep-legacy")
async def archiver_sweep_legacy_now():
    """Force a re-evaluation of every code parked in ``pikpak_archive_
    folder`` (default ``AVBT/已完成``). Items whose series / star /
    director / label / studio is now tracked get promoted into the
    proper kind/name folder; everything else stays put.

    Bypasses the ``archive_sweep_interval_seconds`` cooldown so the
    user can trigger it immediately after adding new tracked listings
    without waiting for the background loop's next cycle."""
    moved = await archiver._sweep_legacy_archive_once()
    return {"moved": moved, **archiver.state.to_dict()}


@router.post("/archiver/sweep-legacy/stream")
async def archiver_sweep_legacy_stream():
    """Streaming variant of ``/archiver/sweep-legacy``. Yields NDJSON
    events (``start`` / ``progress`` / ``error`` / ``done``) so the UI
    can show which file is being processed and surface API errors
    instead of just stalling on a single hanging call."""
    async def gen():
        try:
            async for event in archiver._sweep_legacy_archive_stream():
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/archiver/toggle")
async def archiver_toggle(enabled: bool = Body(..., embed=True)):
    archiver.state.enabled = enabled
    return archiver.state.to_dict()


@router.post("/offline/bulk", response_model=list[PikPakTask])
async def offline_download_bulk(items: list[OfflineSubmit]):
    """Submit a list of magnets. Each item rides the global download
    queue, which dedupes against the OfflineTaskLog hash set and against
    other in-flight jobs. Returns one ``PikPakTask`` per input item,
    using ``phase`` to surface the queue outcome (PENDING / DUPLICATE /
    ERROR)."""
    jobs: list[tuple[OfflineSubmit, asyncio.Future]] = []
    for it in items:
        job = Job(
            code=it.code,
            options=SendAllOptions(folder=it.folder),
            source="manual:bulk",
            direct_magnet=it.magnet,
            folder=it.folder,
            force=it.force,
        )
        fut = await download_queue.enqueue(job)
        jobs.append((it, fut))

    tasks: list[PikPakTask] = []
    for it, fut in jobs:
        try:
            result = await fut
        except Exception as exc:  # noqa: BLE001
            tasks.append(
                PikPakTask(
                    id="",
                    name=it.code or it.magnet[:40],
                    phase="ERROR",
                    progress=0,
                    message=str(exc),
                )
            )
            continue

        if result.status == "sent":
            tasks.append(
                PikPakTask(
                    id=result.task_id,
                    name=result.magnet_name or it.code or it.magnet[:40],
                    phase="PHASE_TYPE_PENDING",
                    progress=0,
                )
            )
        elif result.status == "skipped_already_sent":
            tasks.append(
                PikPakTask(
                    id="",
                    name=it.code or it.magnet[:40],
                    phase="DUPLICATE",
                    progress=0,
                    message="已送過，跳過（force=true 可強制送）",
                )
            )
        else:  # failed / skipped_no_magnet / cancelled
            tasks.append(
                PikPakTask(
                    id="",
                    name=it.code or it.magnet[:40],
                    phase="ERROR",
                    progress=0,
                    message=result.message or result.status,
                )
            )
    return tasks


@router.get("/queue")
async def queue_status():
    """Snapshot of the global download queue: pending count, currently-
    processing codes, lifetime totals, recent N jobs."""
    return download_queue.status()
