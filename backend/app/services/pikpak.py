"""Thin async wrapper around PikPakAPI.

The library exposes a single ``PikPakApi`` class. We cache a singleton
instance (re-using its refresh token across requests) and expose only the
operations we need: login, offline_download, list tasks/files, delete, etc.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from PikPakAPI import PikPakApi

from ..config import settings
from ..schemas import OfflineSubmit, PikPakFile, PikPakQuota, PikPakTask


class PikPakError(RuntimeError):
    pass


class PikPakService:
    def __init__(self) -> None:
        self._client: Optional[PikPakApi] = None
        self._lock = asyncio.Lock()
        self._folder_cache: dict[str, str] = {}

    async def _ensure(self, username: Optional[str] = None, password: Optional[str] = None) -> PikPakApi:
        async with self._lock:
            user = username or settings.pikpak_username
            pwd = password or settings.pikpak_password
            if not user or not pwd:
                raise PikPakError("PikPak credentials are not configured")

            if self._client is None or getattr(self._client, "username", None) != user:
                kwargs: dict[str, Any] = {"username": user, "password": pwd}
                if settings.http_proxy:
                    # PikPakAPI accepts an optional ``proxy`` kw – swallow
                    # TypeError if the installed version doesn't.
                    try:
                        self._client = PikPakApi(proxy=settings.http_proxy, **kwargs)
                    except TypeError:
                        self._client = PikPakApi(**kwargs)
                else:
                    self._client = PikPakApi(**kwargs)
                await self._client.login()
                self._folder_cache.clear()
            return self._client

    async def login(self, username: Optional[str] = None, password: Optional[str] = None) -> dict:
        client = await self._ensure(username, password)
        return {"username": client.username, "user_id": getattr(client, "user_id", None)}

    async def quota(self) -> PikPakQuota:
        client = await self._ensure()
        data = await client.get_quota_info()
        quota = data.get("quota", data) if isinstance(data, dict) else {}
        return PikPakQuota(
            used=int(quota.get("usage") or quota.get("used") or 0),
            limit=int(quota.get("limit") or 0),
            expire=quota.get("expires_at") or quota.get("expire"),
        )

    async def folder_id(self, name: Optional[str]) -> str:
        if not name:
            return ""
        if name in self._folder_cache:
            return self._folder_cache[name]
        client = await self._ensure()
        path = name if name.startswith("/") else f"/{name}"
        folder_id = await client.path_to_id(path, create=True)
        # path_to_id returns a list of path-segments in newer versions
        if isinstance(folder_id, list) and folder_id:
            folder_id = folder_id[-1].get("id", "")
        self._folder_cache[name] = folder_id or ""
        return self._folder_cache[name]

    async def offline_download(self, payload: OfflineSubmit) -> PikPakTask:
        client = await self._ensure()
        folder = payload.folder or settings.pikpak_download_folder
        parent_id = await self.folder_id(folder)
        resp = await client.offline_download(payload.magnet, parent_id=parent_id or None)
        task = resp.get("task") if isinstance(resp, dict) else None
        task = task or (resp if isinstance(resp, dict) else {})
        return PikPakTask(
            id=task.get("id", ""),
            name=task.get("name", ""),
            phase=task.get("phase", ""),
            progress=task.get("progress"),
            file_id=task.get("file_id"),
            file_size=task.get("file_size"),
            message=task.get("message"),
            created_time=task.get("created_time"),
        )

    async def list_tasks(self, size: int = 100) -> list[PikPakTask]:
        client = await self._ensure()
        resp = await client.offline_list(size=size)
        tasks_raw = resp.get("tasks", []) if isinstance(resp, dict) else []
        tasks: list[PikPakTask] = []
        for t in tasks_raw:
            tasks.append(
                PikPakTask(
                    id=t.get("id", ""),
                    name=t.get("name", ""),
                    phase=t.get("phase", ""),
                    progress=int(t.get("progress") or 0),
                    file_id=t.get("file_id"),
                    file_size=int(t.get("file_size") or 0),
                    message=t.get("message"),
                    created_time=t.get("created_time"),
                )
            )
        return tasks

    async def retry_task(self, task_id: str) -> dict:
        client = await self._ensure()
        return await client.offline_task_retry(task_id)

    async def delete_tasks(self, task_ids: list[str], delete_files: bool = False) -> dict:
        client = await self._ensure()
        return await client.delete_tasks(task_ids, delete_files=delete_files)

    async def list_files(self, parent_id: str = "", size: int = 100) -> list[PikPakFile]:
        client = await self._ensure()
        resp = await client.file_list(parent_id=parent_id, size=size)
        files_raw = resp.get("files", []) if isinstance(resp, dict) else []
        files: list[PikPakFile] = []
        for f in files_raw:
            files.append(
                PikPakFile(
                    id=f.get("id", ""),
                    name=f.get("name", ""),
                    kind=f.get("kind", ""),
                    size=int(f.get("size")) if f.get("size") else None,
                    parent_id=f.get("parent_id"),
                    created_time=f.get("created_time"),
                    thumbnail_link=f.get("thumbnail_link"),
                )
            )
        return files

    async def trash_files(self, ids: list[str]) -> dict:
        client = await self._ensure()
        return await client.delete_to_trash(ids)

    async def download_url(self, file_id: str) -> str:
        client = await self._ensure()
        resp = await client.get_download_url(file_id)
        if isinstance(resp, dict):
            return (
                resp.get("web_content_link")
                or resp.get("download_url")
                or resp.get("link")
                or ""
            )
        return str(resp or "")

    async def search_files(self, keyword: str, parent_id: str = "") -> list[PikPakFile]:
        client = await self._ensure()
        # PikPakAPI exposes file_list_search or similar; fall back to a
        # client-side filter if not available in the installed version.
        if hasattr(client, "file_search"):
            resp = await client.file_search(keyword, parent_id=parent_id)
            files_raw = resp.get("files", []) if isinstance(resp, dict) else []
        else:
            resp = await client.file_list(parent_id=parent_id, size=500)
            all_files = resp.get("files", []) if isinstance(resp, dict) else []
            kw = keyword.lower()
            files_raw = [f for f in all_files if kw in (f.get("name") or "").lower()]
        return [
            PikPakFile(
                id=f.get("id", ""),
                name=f.get("name", ""),
                kind=f.get("kind", ""),
                size=int(f.get("size")) if f.get("size") else None,
                parent_id=f.get("parent_id"),
                created_time=f.get("created_time"),
                thumbnail_link=f.get("thumbnail_link"),
            )
            for f in files_raw
        ]

    async def create_share(self, file_ids: list[str], pass_code_option: str = "NOT_REQUIRED") -> dict:
        """Create a share link. Returns {url, pass_code}."""
        client = await self._ensure()
        if not hasattr(client, "file_batch_share"):
            raise PikPakError("此版本 PikPakAPI 不支援建立分享連結")
        resp = await client.file_batch_share(file_ids, pass_code_option=pass_code_option)
        if not isinstance(resp, dict):
            resp = {"raw": resp}
        return {
            "url": resp.get("share_url") or resp.get("url") or "",
            "pass_code": resp.get("pass_code") or "",
            "share_id": resp.get("share_id") or "",
        }


pikpak_service = PikPakService()
