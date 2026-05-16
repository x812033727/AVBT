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
from ..services.pikpak import PikPakError, pikpak_service

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


@router.get("/quota", response_model=PikPakQuota)
async def quota():
    try:
        return await pikpak_service.quota()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/offline", response_model=PikPakTask)
async def offline_download(payload: OfflineSubmit):
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
    pass_code_option: str = Body("NOT_REQUIRED", embed=True),
):
    try:
        return await pikpak_service.create_share(file_ids, pass_code_option=pass_code_option)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


@router.post("/offline/bulk", response_model=list[PikPakTask])
async def offline_download_bulk(items: list[OfflineSubmit]):
    tasks: list[PikPakTask] = []
    for it in items:
        try:
            tasks.append(await pikpak_service.offline_download(it))
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
