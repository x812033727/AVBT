from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import insert

from ..database import SessionLocal
from ..models import OfflineTaskLog
from ..schemas import (
    OfflineSubmit,
    PikPakFile,
    PikPakLogin,
    PikPakQuota,
    PikPakTask,
)
from sqlalchemy import select

from ..database import SessionLocal
from ..models import OfflineTaskLog
from ..scrapers.javbus import extract_btih
from ..services import archiver
from ..services.pikpak import PikPakError, pikpak_service


class DuplicateMagnetError(PikPakError):
    """Raised when a magnet hash is already in offline_task_log and the
    caller did not opt in via ``force=true``."""


async def _is_duplicate(magnet: str) -> bool:
    h = extract_btih(magnet)
    if not h:
        return False
    async with SessionLocal() as session:
        rows = (await session.execute(select(OfflineTaskLog.magnet))).scalars().all()
    return any(extract_btih(m or "") == h for m in rows)

router = APIRouter(prefix="/api/pikpak", tags=["pikpak"])


def _wrap(exc: Exception) -> HTTPException:
    code = 400 if isinstance(exc, PikPakError) else 502
    return HTTPException(status_code=code, detail=f"PikPak 錯誤: {exc}")


@router.post("/login")
async def login(payload: PikPakLogin):
    try:
        return await pikpak_service.login(payload.username, payload.password)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


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


@router.get("/files/{file_id}/url")
async def file_url(file_id: str):
    try:
        url = await pikpak_service.download_url(file_id)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return {"url": url}


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
    # batch without N round-trips.
    async with SessionLocal() as session:
        sent_rows = (await session.execute(select(OfflineTaskLog.magnet))).scalars().all()
    sent_hashes = {h for m in sent_rows if (h := extract_btih(m or ""))}

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
