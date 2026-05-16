"""Thin async wrapper around PikPakAPI.

The library exposes a single ``PikPakApi`` class. We cache a singleton
instance (re-using its refresh token across requests) and expose only the
operations we need: login, offline_download, list tasks/files, delete, etc.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from pikpakapi import PikPakApi

from ..config import settings
from ..schemas import OfflineSubmit, PikPakFile, PikPakQuota, PikPakTask
from .jav_code import ext_of, extract_jav_code, is_video


logger = logging.getLogger(__name__)


class PikPakError(RuntimeError):
    pass


TOKEN_FILE = Path("data/pikpak_token.txt")


class PikPakService:
    def __init__(self) -> None:
        self._client: Optional[PikPakApi] = None
        self._lock = asyncio.Lock()
        self._folder_cache: dict[str, str] = {}
        self._username: str = ""

    # ---------- token persistence ----------

    def _load_token(self) -> str | None:
        if TOKEN_FILE.exists():
            try:
                return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
        return None

    def _save_token(self, token: str) -> None:
        if not token:
            return
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass

    def _clear_token(self) -> None:
        try:
            TOKEN_FILE.unlink()
        except FileNotFoundError:
            pass

    def _maybe_encode_token(self, client: PikPakApi) -> None:
        if hasattr(client, "encode_token"):
            try:
                token = client.encode_token()
                if token:
                    self._save_token(token)
            except Exception:  # noqa: BLE001
                pass

    def _build_kwargs(self, **base: Any) -> dict[str, Any]:
        kwargs = dict(base)
        if settings.http_proxy:
            kwargs["httpx_client_args"] = {"proxy": settings.http_proxy}
        # Persist the refreshed token back to disk every time pikpakapi
        # rotates access/refresh tokens internally, so the file we read
        # on the next startup is always current.
        kwargs.setdefault("token_refresh_callback", self._on_token_refresh)
        return kwargs

    async def _on_token_refresh(self, client: "PikPakApi", **_: Any) -> None:
        try:
            token = getattr(client, "encoded_token", "") or ""
            if token:
                self._save_token(token)
        except Exception:  # noqa: BLE001
            pass

    # ---------- public ----------

    def status(self) -> dict:
        return {
            "logged_in": self._client is not None,
            "username": self._username,
            "has_stored_token": TOKEN_FILE.exists(),
            "has_env_credentials": bool(
                settings.pikpak_username and settings.pikpak_password
            ),
        }

    def logout(self) -> None:
        self._client = None
        self._username = ""
        self._folder_cache.clear()
        self._clear_token()

    async def _ensure(
        self, username: Optional[str] = None, password: Optional[str] = None
    ) -> PikPakApi:
        async with self._lock:
            # Explicit credentials → force re-login
            if username and password:
                client = PikPakApi(
                    **self._build_kwargs(username=username, password=password)
                )
                await client.login()
                self._maybe_encode_token(client)
                self._client = client
                self._username = username
                self._folder_cache.clear()
                return self._client

            if self._client is not None:
                return self._client

            # Try stored token first.
            token = self._load_token()
            if token:
                try:
                    self._client = PikPakApi(**self._build_kwargs(encoded_token=token))
                    self._username = getattr(self._client, "username", "") or ""
                    return self._client
                except Exception:  # noqa: BLE001
                    self._clear_token()
                    self._client = None

            # Fall back to .env credentials.
            env_user = settings.pikpak_username
            env_pwd = settings.pikpak_password
            if not env_user or not env_pwd:
                raise PikPakError(
                    "PikPak 尚未登入。請到 /settings 填入帳密，或在 .env 設定 "
                    "PIKPAK_USERNAME / PIKPAK_PASSWORD。"
                )

            client = PikPakApi(
                **self._build_kwargs(username=env_user, password=env_pwd)
            )
            await client.login()
            self._maybe_encode_token(client)
            self._client = client
            self._username = env_user
            self._folder_cache.clear()
            return self._client

    async def login(
        self, username: Optional[str] = None, password: Optional[str] = None
    ) -> dict:
        client = await self._ensure(username, password)
        return {
            "username": getattr(client, "username", "") or self._username,
            "user_id": getattr(client, "user_id", None),
        }

    async def login_with_token(self, encoded_token: str) -> dict:
        """Skip username/password — build a client straight from the
        encoded token. Verifies it works by hitting get_user_info()."""
        token = (encoded_token or "").strip()
        if not token:
            raise PikPakError("Token 不可空白")

        async with self._lock:
            client = PikPakApi(**self._build_kwargs(encoded_token=token))
            try:
                info = await client.get_user_info()
            except Exception as exc:  # noqa: BLE001
                raise PikPakError(f"Token 無效或已過期: {exc}") from exc
            self._client = client
            self._username = (
                getattr(client, "username", "")
                or (info.get("name") if isinstance(info, dict) else "")
                or ""
            )
            self._folder_cache.clear()
            # Prefer the library's freshly-re-encoded token (it may have
            # refreshed access). Fall back to whatever the user pasted.
            self._maybe_encode_token(client)
            if not TOKEN_FILE.exists() or not self._load_token():
                self._save_token(token)

        return {
            "username": self._username,
            "user_id": getattr(client, "user_id", None),
        }

    def export_token(self) -> str:
        """Return the currently stored token (for backup/copy)."""
        return self._load_token() or ""

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

    async def move_files(self, ids: list[str], to_parent_id: str) -> dict:
        client = await self._ensure()
        return await client.file_batch_move(ids, to_parent_id)

    async def rename_file(self, file_id: str, new_name: str) -> dict:
        client = await self._ensure()
        return await client.file_rename(file_id, new_name)

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

    async def create_share(
        self,
        file_ids: list[str],
        need_password: bool = False,
        expiration_days: int = -1,
    ) -> dict:
        """Create a share link. Returns {url, pass_code, share_id}."""
        client = await self._ensure()
        if not hasattr(client, "file_batch_share"):
            raise PikPakError("此版本 PikPakAPI 不支援建立分享連結")
        resp = await client.file_batch_share(
            file_ids,
            need_password=need_password,
            expiration_days=expiration_days,
        )
        if not isinstance(resp, dict):
            resp = {"raw": resp}
        return {
            "url": resp.get("share_url") or resp.get("url") or "",
            "pass_code": resp.get("pass_code") or "",
            "share_id": resp.get("share_id") or "",
        }


    async def cleanup_folder_stream(
        self, folder_id: str, *, dry_run: bool = True
    ) -> AsyncIterator[dict]:
        """Walk every direct child of ``folder_id`` and try to normalise its
        name to a clean JAV code.

        - File: rename to ``<code>.<ext>``
        - Folder containing exactly one video and no subfolders: flatten
          (rename the inner video, move it to the outer folder, trash the
          empty wrapper)
        - Otherwise (mixed contents): rename the folder itself to ``<code>``

        Yields NDJSON-shaped events: ``start`` / ``progress`` / ``done``.
        When ``dry_run`` is true the function emits the same events but
        performs no mutations on PikPak.
        """
        try:
            children = await self.list_files(folder_id, size=500)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
            return

        taken: set[str] = {c.name for c in children}
        summary = {
            "total": len(children),
            "renamed": 0,
            "flattened": 0,
            "skipped": 0,
            "errors": 0,
            "dry_run": dry_run,
        }

        yield {
            "type": "start",
            "total": len(children),
            "dry_run": dry_run,
            "folder_id": folder_id,
        }

        for idx, child in enumerate(children, start=1):
            await asyncio.sleep(0.1)
            kind = "folder" if child.kind == "drive#folder" else "file"
            code = extract_jav_code(child.name)
            base_event = {
                "type": "progress",
                "current": idx,
                "kind": kind,
                "source": child.name,
            }

            if not code:
                summary["skipped"] += 1
                yield {**base_event, "action": "skip", "target": None, "reason": "no_code"}
                continue

            try:
                if kind == "file":
                    target = f"{code}{ext_of(child.name)}"
                    if target == child.name:
                        summary["skipped"] += 1
                        yield {**base_event, "action": "skip", "target": target, "reason": "already_clean"}
                        continue
                    if target in taken:
                        summary["skipped"] += 1
                        yield {**base_event, "action": "skip", "target": target, "reason": "conflict"}
                        continue
                    if not dry_run:
                        await self.rename_file(child.id, target)
                    taken.discard(child.name)
                    taken.add(target)
                    summary["renamed"] += 1
                    yield {**base_event, "action": "rename", "target": target, "reason": None}
                    continue

                # ---- folder ----
                inner = await self.list_files(child.id, size=50)
                videos = [
                    i for i in inner
                    if i.kind != "drive#folder" and is_video(i.name)
                ]
                subfolders = [i for i in inner if i.kind == "drive#folder"]

                # Real JAV episodes are ≥500MB. BT releases bundle tiny
                # ad mp4s alongside the real video. Anything well under
                # 500MB is junk; 300MB threshold gives a 200MB buffer
                # for unusual encodes. None size → assume legit.
                JUNK_BYTES = 300 * 1024 * 1024
                main_videos = [
                    v for v in videos
                    if v.size is None or v.size >= JUNK_BYTES
                ]

                if len(main_videos) == 1 and not subfolders:
                    # Flatten: rename inner video → move to outer → trash
                    # any leftover junk → trash empty wrapper.
                    video = main_videos[0]
                    target = f"{code}{ext_of(video.name)}"
                    if target in taken and target != child.name:
                        summary["skipped"] += 1
                        yield {**base_event, "action": "skip", "target": target, "reason": "conflict"}
                        continue
                    leftover_ids = [i.id for i in inner if i.id != video.id]
                    if not dry_run:
                        if video.name != target:
                            await self.rename_file(video.id, target)
                        await self.move_files([video.id], folder_id)
                        if leftover_ids:
                            await self.trash_files(leftover_ids)
                        await self.trash_files([child.id])
                    taken.discard(child.name)
                    taken.add(target)
                    summary["flattened"] += 1
                    reason = f"順手清掉 {len(leftover_ids)} 個垃圾檔" if leftover_ids else None
                    yield {**base_event, "action": "flatten", "target": target, "reason": reason}
                    continue

                # Mixed contents: just rename the wrapper folder.
                if child.name == code:
                    summary["skipped"] += 1
                    yield {**base_event, "action": "skip", "target": code, "reason": "already_clean"}
                    continue
                if code in taken:
                    summary["skipped"] += 1
                    yield {**base_event, "action": "skip", "target": code, "reason": "conflict"}
                    continue
                if not dry_run:
                    await self.rename_file(child.id, code)
                taken.discard(child.name)
                taken.add(code)
                summary["renamed"] += 1
                yield {**base_event, "action": "rename", "target": code, "reason": None}

            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning("cleanup failed for %s: %s", child.name, exc)
                yield {**base_event, "action": "error", "target": None, "reason": str(exc)}

        yield {"type": "done", "result": summary}


pikpak_service = PikPakService()
