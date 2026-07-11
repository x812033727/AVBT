"""Thin async wrapper around PikPakAPI.

The library exposes a single ``PikPakApi`` class. We cache a singleton
instance (re-using its refresh token across requests) and expose only the
operations we need: login, offline_download, list tasks/files, delete, etc.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pikpakapi import PikPakApi

from ..config import settings, task_folder_path
from ..schemas import OfflineSubmit, PikPakFile, PikPakQuota, PikPakTask
from .jav_code import ext_of, extract_jav_code, extract_jav_code_full, is_video

# Rename-plan helpers moved to services/rename_plan.py; re-exported
# here so existing import sites (pcloud, episode_finder, reorganize)
# keep working unchanged.
from .rename_plan import (  # noqa: F401
    _PART_INDEX_RE,
    _build_video_rename_plan,
    _canonical_video_name,
    _dup_sort_index,
    _part_marker_index,
    _uniquify_target,
)

logger = logging.getLogger(__name__)


class PikPakError(RuntimeError):
    pass


TOKEN_FILE = Path("data/pikpak_token.txt")


# Substrings that mark "your refresh token got rotated by another session"
# in PikPak's server response. The exact message has shifted over time so
# we match on stable fragments: the literal "refresh" + "redis" pair only
# appears on this specific class of error.
_INVALID_TOKEN_MARKERS = (
    "invalid refresh token",
    "refreshed by other process",
    "invalid_grant",
    "captcha_invalid",
    "token has been disabled",
)


def _backfill_user_id(client: PikPakApi) -> None:
    """pikpakapi's ``decode_token()`` restores only the access/refresh
    tokens and leaves ``user_id`` as None. ``captcha_init`` then sends
    ``"user_id": null`` and PikPak's server rejects it with a proto
    error — breaking ``get_download_url`` (the only captcha-gated call,
    i.e. every playback/download link) on every token-restored client.
    Recover the id from the access-token JWT's ``sub`` claim; a later
    token refresh re-sets it from the server response anyway."""
    if getattr(client, "user_id", None):
        return
    try:
        payload = (client.access_token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub") or ""
        if sub:
            client.user_id = sub
    except Exception:  # noqa: BLE001 — leave unset; refresh will fill it
        logger.debug("could not backfill PikPak user_id from access token")


def _is_invalid_token_error(exc: BaseException) -> bool:
    """True when PikPak's server told us our refresh token is no longer
    valid — usually because the same account refreshed elsewhere (phone
    app, another container, manual login). Recovery is to drop the
    cached client + stored token and re-login from env credentials."""
    msg = str(exc).lower()
    if not msg:
        return False
    return any(m in msg for m in _INVALID_TOKEN_MARKERS)


# PikPak's batch file operations (trash / move) cap how many file ids may
# ride in a single request; past the cap the server rejects the whole call
# with "Count of operating files is exceeded". We split large id lists into
# chunks under this cap. The exact cap isn't published and has shifted over
# time, so this is just a safe default — ``_run_batch`` also halves and
# retries any chunk the server still refuses, so correctness doesn't hinge
# on the value being exactly right.
_BATCH_OP_LIMIT = 100


def _chunked(items: list, size: int):
    """Yield ``items`` in consecutive slices of at most ``size``."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _is_count_exceeded_error(exc: BaseException) -> bool:
    """True when PikPak rejected a batch op for carrying too many file ids
    in one request (the "Count of operating files is exceeded" error)."""
    return "operating files" in str(exc).lower()


class PikPakService:
    def __init__(self) -> None:
        self._client: PikPakApi | None = None
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

    async def _on_token_refresh(self, client: PikPakApi, **_: Any) -> None:
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
        self, username: str | None = None, password: str | None = None
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
                    _backfill_user_id(self._client)
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

    async def _drop_for_relogin(self, current: PikPakApi | None) -> None:
        """Forget the cached client + stored token so the next ``_ensure``
        forces a fresh login from env credentials. Only acts when the
        passed-in client is still the one we have cached — protects
        against double-recovery when several callers race on the same
        invalidation."""
        async with self._lock:
            if current is None or self._client is current:
                self._client = None
                self._clear_token()
                self._folder_cache.clear()

    async def _call(self, op):
        """Run ``await op(client)`` with one auto-retry when PikPak's
        server says our refresh token has been invalidated by another
        session. Concurrent callers all converge on a single re-login
        through ``_ensure``'s lock, so the retry doesn't fan out into
        N parallel logins.

        Each round-trip is wrapped in ``asyncio.wait_for`` with
        ``settings.pikpak_api_timeout_seconds`` so a hung connection
        surfaces as a ``PikPakError`` instead of stalling the whole
        sweep / archive loop. A timeout of 0 disables the cap (legacy
        behaviour)."""
        timeout = float(settings.pikpak_api_timeout_seconds or 0)

        async def _run(c):
            if timeout > 0:
                try:
                    return await asyncio.wait_for(op(c), timeout=timeout)
                except TimeoutError as exc:
                    raise PikPakError(
                        f"PikPak API 逾時 ({timeout:.0f}s)"
                    ) from exc
            return await op(c)

        client = await self._ensure()
        try:
            return await _run(client)
        except Exception as exc:  # noqa: BLE001
            if not _is_invalid_token_error(exc):
                raise
            logger.warning(
                "PikPak refresh token invalidated by another session "
                "(%s); re-logging in", exc,
            )
            await self._drop_for_relogin(client)
            client = await self._ensure()
            return await _run(client)

    async def login(
        self, username: str | None = None, password: str | None = None
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
        data = await self._call(lambda c: c.get_quota_info())
        quota = data.get("quota", data) if isinstance(data, dict) else {}
        return PikPakQuota(
            used=int(quota.get("usage") or quota.get("used") or 0),
            limit=int(quota.get("limit") or 0),
            expire=quota.get("expires_at") or quota.get("expire"),
        )

    async def folder_id(self, name: str | None) -> str:
        if not name:
            return ""
        if name in self._folder_cache:
            return self._folder_cache[name]
        path = name if name.startswith("/") else f"/{name}"
        folder_id = await self._call(lambda c: c.path_to_id(path, create=True))
        # path_to_id returns a list of path-segments in newer versions
        if isinstance(folder_id, list) and folder_id:
            folder_id = folder_id[-1].get("id", "")
        self._folder_cache[name] = folder_id or ""
        return self._folder_cache[name]

    async def lookup_folder_id(self, name: str | None) -> str:
        """Like ``folder_id`` but does NOT auto-create missing segments.
        Returns ``""`` when the path doesn't exist."""
        if not name:
            return ""
        if name in self._folder_cache:
            return self._folder_cache[name]
        path = name if name.startswith("/") else f"/{name}"
        try:
            result = await self._call(lambda c: c.path_to_id(path, create=False))
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(result, list):
            folder_id = result[-1].get("id", "") if result else ""
        else:
            folder_id = result or ""
        if folder_id:
            self._folder_cache[name] = folder_id
        return folder_id or ""

    async def offline_download(self, payload: OfflineSubmit) -> PikPakTask:
        # Default to the dedicated task folder (AVBT/TASK) instead of the
        # download root — keeps BT-noise wrappers from polluting AVBT/.
        folder = payload.folder or task_folder_path()
        parent_id = await self.folder_id(folder)
        resp = await self._call(
            lambda c: c.offline_download(payload.magnet, parent_id=parent_id or None)
        )
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
        resp = await self._call(lambda c: c.offline_list(size=size))
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
        return await self._call(lambda c: c.offline_task_retry(task_id))

    async def delete_tasks(self, task_ids: list[str], delete_files: bool = False) -> dict:
        return await self._call(
            lambda c: c.delete_tasks(task_ids, delete_files=delete_files)
        )

    async def list_files(self, parent_id: str = "", size: int = 100) -> list[PikPakFile]:
        resp = await self._call(
            lambda c: c.file_list(parent_id=parent_id, size=size)
        )
        files_raw = resp.get("files", []) if isinstance(resp, dict) else []
        return [self._file_from_raw(f) for f in files_raw]

    @staticmethod
    def _file_from_raw(f: dict) -> PikPakFile:
        return PikPakFile(
            id=f.get("id", ""),
            name=f.get("name", ""),
            kind=f.get("kind", ""),
            size=int(f.get("size")) if f.get("size") else None,
            parent_id=f.get("parent_id"),
            created_time=f.get("created_time"),
            thumbnail_link=f.get("thumbnail_link"),
        )

    async def list_all_files(
        self, parent_id: str = "", *, cap: int = 5000
    ) -> tuple[list[PikPakFile], bool]:
        """Page through every child of ``parent_id`` up to ``cap`` items.

        Returns ``(files, partial)``. ``partial`` is True when:
        - we hit the cap, or
        - the installed pikpakapi doesn't expose ``next_page_token`` so we
          fell back to a single page.
        """
        files: list[PikPakFile] = []
        token = ""
        size = 500

        try:
            while True:
                resp = await self._call(
                    lambda c, t=token: c.file_list(
                        parent_id=parent_id, size=size, next_page_token=t
                    )
                )
                if not isinstance(resp, dict):
                    break
                batch = resp.get("files", []) or []
                for f in batch:
                    files.append(self._file_from_raw(f))
                    if len(files) >= cap:
                        return files, True
                token = resp.get("next_page_token") or ""
                if not token or not batch:
                    break
        except TypeError:
            # Older pikpakapi: file_list doesn't accept next_page_token.
            resp = await self._call(
                lambda c: c.file_list(parent_id=parent_id, size=size)
            )
            if isinstance(resp, dict):
                for f in resp.get("files", []) or []:
                    files.append(self._file_from_raw(f))
            partial = bool(resp.get("next_page_token")) if isinstance(resp, dict) else False
            return files, partial

        return files, False

    async def _run_batch(self, ids: list[str], call) -> dict:
        """Apply a PikPak batch op across ``ids`` in chunks small enough to
        stay under the server's per-request file-count cap. ``call(client,
        chunk)`` builds the awaitable for one request.

        If the server still rejects a chunk with "Count of operating files
        is exceeded", split that chunk in half and retry, so the whole set
        is processed regardless of the exact (unpublished) cap. Other
        errors propagate unchanged.

        Returns the last chunk's response — callers ignore the body and the
        single-chunk path is identical to a plain ``_call``. An empty
        ``ids`` is a no-op that issues no request."""
        last: dict = {}
        pending = list(_chunked(list(ids), _BATCH_OP_LIMIT))
        while pending:
            chunk = pending.pop(0)
            try:
                resp = await self._call(lambda c, ch=chunk: call(c, ch))
            except Exception as exc:  # noqa: BLE001
                if len(chunk) > 1 and _is_count_exceeded_error(exc):
                    mid = len(chunk) // 2
                    pending[:0] = [chunk[:mid], chunk[mid:]]
                    continue
                raise
            if isinstance(resp, dict):
                last = resp
        return last

    async def trash_files(self, ids: list[str]) -> dict:
        return await self._run_batch(ids, lambda c, ch: c.delete_to_trash(ch))

    async def move_files(self, ids: list[str], to_parent_id: str) -> dict:
        return await self._run_batch(
            ids, lambda c, ch: c.file_batch_move(ch, to_parent_id)
        )

    async def rename_file(self, file_id: str, new_name: str) -> dict:
        return await self._call(lambda c: c.file_rename(file_id, new_name))

    async def file_links(self, file_id: str) -> dict:
        """Return ``{download_url, play_url, mime_type}`` for a single file.

        - ``download_url`` is the progressive download link (good for non-video
          files and as a fallback when ``<video>`` can't decode the format).
        - ``play_url`` is the high-speed streaming link from ``medias[0].link``
          (falls back to ``download_url`` if PikPak didn't surface one).
        """
        resp = await self._call(lambda c: c.get_download_url(file_id))
        if not isinstance(resp, dict):
            url = str(resp or "")
            return {"download_url": url, "play_url": url, "mime_type": ""}
        download_url = (
            resp.get("web_content_link")
            or resp.get("download_url")
            or resp.get("link")
            or ""
        )
        play_url = ""
        medias = resp.get("medias") or []
        if isinstance(medias, list) and medias:
            link = (medias[0] or {}).get("link") or {}
            if isinstance(link, dict):
                play_url = link.get("url", "") or ""
        return {
            "download_url": download_url,
            "play_url": play_url or download_url,
            "mime_type": resp.get("mime_type", "") or "",
        }

    async def download_url(self, file_id: str) -> str:
        return (await self.file_links(file_id))["download_url"]

    async def file_meta(self, file_id: str) -> dict:
        """Name/kind metadata for a single file id. Rides the same
        files/{id} lookup as ``file_links`` — the response carries the
        file object, which is all we need to tell a bare file from a
        folder without listing its parent."""
        resp = await self._call(lambda c: c.get_download_url(file_id))
        if not isinstance(resp, dict):
            return {}
        return {
            "id": resp.get("id", file_id) or file_id,
            "name": resp.get("name", "") or "",
            "kind": resp.get("kind", "") or "",
        }

    async def search_files(self, keyword: str, parent_id: str = "") -> list[PikPakFile]:
        # PikPakAPI exposes file_list_search or similar; fall back to a
        # client-side filter if not available in the installed version.
        client = await self._ensure()
        has_search = hasattr(client, "file_search")
        if has_search:
            resp = await self._call(
                lambda c: c.file_search(keyword, parent_id=parent_id)
            )
            files_raw = resp.get("files", []) if isinstance(resp, dict) else []
        else:
            resp = await self._call(
                lambda c: c.file_list(parent_id=parent_id, size=500)
            )
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
        resp = await self._call(
            lambda c: c.file_batch_share(
                file_ids,
                need_password=need_password,
                expiration_days=expiration_days,
            )
        )
        if not isinstance(resp, dict):
            resp = {"raw": resp}
        return {
            "url": resp.get("share_url") or resp.get("url") or "",
            "pass_code": resp.get("pass_code") or "",
            "share_id": resp.get("share_id") or "",
        }


    async def _collect_main_videos(
        self, folder_id: str, junk_bytes: int, *, max_depth: int = 2
    ) -> tuple[list[PikPakFile], int]:
        """Walk ``folder_id`` recursively (up to ``max_depth`` levels) and
        return ``(top_level_main_videos, total_main_count)``.

        The first element is just the direct-child videos at the wrapper
        level (used when we flatten); the count covers every descendant
        so callers can refuse to flatten when nested CD2/disc2 content
        would be lost."""
        try:
            inner = await self.list_files(folder_id, size=100)
        except Exception:  # noqa: BLE001
            return [], 0
        top_videos: list[PikPakFile] = []
        total_count = 0
        sub_jobs = []
        for c in inner:
            if c.kind == "drive#folder":
                if max_depth > 1:
                    sub_jobs.append(
                        self._collect_main_videos(
                            c.id, junk_bytes, max_depth=max_depth - 1
                        )
                    )
            elif is_video(c.name) and (c.size is None or c.size >= junk_bytes):
                top_videos.append(c)
                total_count += 1
        if sub_jobs:
            sub_results = await asyncio.gather(*sub_jobs, return_exceptions=True)
            for r in sub_results:
                if isinstance(r, tuple):
                    nested_videos, nested_count = r
                    total_count += nested_count
                    # Promote nested videos to the "flattenable" list so
                    # callers can pick the single video even when it's
                    # buried inside a Sample/ wrapper or similar.
                    top_videos.extend(nested_videos)
        return top_videos, total_count

    async def cleanup_folder_stream(
        self,
        folder_id: str,
        *,
        dry_run: bool = True,
        recursive: bool = True,
        _depth: int = 0,
    ) -> AsyncIterator[dict]:
        """Walk every direct child of ``folder_id`` and try to normalise its
        name to a clean JAV code.

        - File: rename to ``<code>.<ext>`` (preserves variant letter)
        - Folder with exactly one main video (≥300 MB) anywhere up to
          two levels deep: flatten — pull the inner video out, rename
          to ``<code>.<ext>``, trash the whole wrapper (junk + Sample
          subfolders go with it).
        - Folder with 2+ main videos OR none: rename the wrapper to
          ``<code>`` and (when ``recursive``) clean its insides too.

        Naming uses :func:`extract_jav_code_full` so variant letters are
        kept (``SDMM-14903C`` stays as ``SDMM-14903C``) and multiple
        variants of the same base code coexist. When a target name does
        collide we deduplicate with ``" (2)"`` / ``" (3)"`` suffixes
        instead of silently skipping.

        Yields NDJSON-shaped events: ``start`` / ``progress`` / ``done``.
        When ``dry_run`` is true the function emits the same events but
        performs no mutations on PikPak.
        """
        try:
            children, partial = await self.list_all_files(folder_id)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
            return

        taken: set[str] = {c.name for c in children}

        # Pre-scan flat video files for two corrections:
        # - lonely variant letters (e.g. SDMM-14903A alone) → strip the
        #   letter so the file becomes SDMM-14903.<ext>
        # - real multi-part groups (2+ substantial-size files sharing a
        #   canonical) → number them ``<canon>_N.<ext>`` instead of
        #   keeping PikPak's "(2)/(3)" auto-dedupe form
        #
        # ``multipart_members`` also includes already-correctly-named
        # members so the file branch leaves them alone instead of
        # collapsing them via the default single-file naming.
        PART_MIN_BYTES = 500 * 1024 * 1024
        multipart_plan, multipart_members = _build_video_rename_plan(
            children, PART_MIN_BYTES, is_video
        )

        summary = {
            "total": len(children),
            "renamed": 0,
            "flattened": 0,
            "skipped": 0,
            "errors": 0,
            "dry_run": dry_run,
            "partial": partial,
        }

        # The top-level call emits start/done; recursive calls just emit
        # progress events so the existing UI can show them in line.
        if _depth == 0:
            yield {
                "type": "start",
                "total": len(children),
                "dry_run": dry_run,
                "folder_id": folder_id,
                "partial": partial,
            }
            if partial:
                yield {
                    "type": "warn",
                    "message": "此資料夾項目過多,可能僅處理部分子項",
                }

        for idx, child in enumerate(children, start=1):
            await asyncio.sleep(0.05)
            kind = "folder" if child.kind == "drive#folder" else "file"
            code = extract_jav_code(child.name)
            code_full = extract_jav_code_full(child.name) or code
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
                    # Multi-part rename plan wins over the default
                    # single-file naming: when the file is part of a
                    # group of substantial same-canonical videos we
                    # give it a ``<canon>_N.<ext>`` slot.
                    if child.name in multipart_plan:
                        target = multipart_plan[child.name]
                    elif child.name in multipart_members:
                        # Already-correctly-named member of a multi-part
                        # group — leaving it alone is critical, otherwise
                        # the default ``<code_full>.<ext>`` would
                        # collapse all variants on every cleanup re-run.
                        summary["skipped"] += 1
                        yield {**base_event, "action": "skip", "target": child.name, "reason": "already_clean"}
                        continue
                    else:
                        target = f"{code_full}{ext_of(child.name)}"
                    if target == child.name:
                        summary["skipped"] += 1
                        yield {**base_event, "action": "skip", "target": target, "reason": "already_clean"}
                        continue
                    # Auto-dedupe on collision instead of silently skipping.
                    target = _uniquify_target(target, taken)
                    if not dry_run:
                        await self.rename_file(child.id, target)
                    taken.discard(child.name)
                    taken.add(target)
                    summary["renamed"] += 1
                    yield {**base_event, "action": "rename", "target": target, "reason": None}
                    continue

                # ---- folder ----
                # Real JAV episodes are ≥500MB. BT releases bundle tiny
                # ad mp4s alongside the real video. Anything well under
                # 500MB is junk; 300MB threshold gives a 200MB buffer
                # for unusual encodes. None size → assume legit.
                JUNK_BYTES = 300 * 1024 * 1024
                # When 2+ files share a canonical name, we use this
                # higher bar to decide whether they're real episodes
                # (both substantial → keep all) vs a resolution
                # duplicate or ad clip (one much smaller → drop it).
                PART_MIN_BYTES = 500 * 1024 * 1024

                # Walk up to two levels deep so flatten still works for
                # the common BT shape ``300MIUM-1098/Sample/...`` (one
                # main video at the top, junk subfolder beside it).
                main_videos, _total_main_count = await self._collect_main_videos(
                    child.id, JUNK_BYTES, max_depth=2
                )

                if main_videos:
                    # Group by canonical name. Files in the same group
                    # share a base identity (PikPak "(N)" or HD/720p
                    # markers stripped). Different canonical = different
                    # part (CD1/CD2, A/B, -1/-2) — always kept.
                    groups: dict[str, list[PikPakFile]] = {}
                    for v in main_videos:
                        groups.setdefault(
                            _canonical_video_name(v.name), []
                        ).append(v)
                    keepers: list[tuple[str, PikPakFile]] = []
                    dropped_count = 0
                    for canon, vids in groups.items():
                        vids.sort(key=lambda v: v.size or 0, reverse=True)
                        if len(vids) == 1:
                            keepers.append((canon, vids[0]))
                            continue
                        # Same canonical, multiple files: real parts
                        # if ALL ≥ 500MB; otherwise treat smaller ones
                        # as resolution dups / leftover ads and drop.
                        all_substantial = all(
                            (v.size or 0) >= PART_MIN_BYTES for v in vids
                        )
                        if all_substantial:
                            for v in vids:
                                keepers.append((canon, v))
                        else:
                            keepers.append((canon, vids[0]))
                            dropped_count += len(vids) - 1

                    # Decide naming:
                    #  - 1 keeper total → wrapper's code_full
                    #  - multiple keepers, all distinct canonicals
                    #    (CD1/CD2/A/B style) → canonical preserves them
                    #  - multiple keepers sharing a canonical (real
                    #    parts that look-alike) → ``<canon>_N.<ext>``
                    canon_group_size: dict[str, int] = {}
                    canon_seq: dict[str, int] = {}
                    for canon, _v in keepers:
                        canon_group_size[canon] = canon_group_size.get(canon, 0) + 1
                    moved: list[str] = []
                    for canon, video in keepers:
                        if len(keepers) == 1:
                            target = f"{code_full}{ext_of(video.name)}"
                        elif canon_group_size[canon] > 1:
                            canon_seq[canon] = canon_seq.get(canon, 0) + 1
                            target = f"{canon}_{canon_seq[canon]}{ext_of(video.name)}"
                        else:
                            target = f"{canon}{ext_of(video.name)}"
                        if target != child.name:
                            target = _uniquify_target(target, taken)
                        if not dry_run:
                            if video.name != target:
                                await self.rename_file(video.id, target)
                            await self.move_files([video.id], folder_id)
                        taken.add(target)
                        moved.append(target)

                    if not dry_run:
                        # Wrapper trash takes leftover junk + any
                        # dropped lower-resolution duplicates.
                        await self.trash_files([child.id])
                    taken.discard(child.name)
                    summary["flattened"] += 1
                    reason_bits: list[str] = []
                    if len(keepers) > 1:
                        reason_bits.append(f"分集 {len(keepers)} 部")
                    if dropped_count:
                        reason_bits.append(f"丟掉 {dropped_count} 個低解析重複")
                    yield {
                        **base_event,
                        "action": "flatten",
                        "target": " / ".join(moved),
                        "reason": "・".join(reason_bits) or None,
                    }
                    continue

                # Mixed / weird contents: rename the wrapper, then
                # recursively clean its insides so nested junk gets
                # normalised too. (Skip recursion at max depth.)
                target_name = code_full
                if child.name != target_name:
                    target_name = _uniquify_target(target_name, taken)
                    if not dry_run:
                        await self.rename_file(child.id, target_name)
                    taken.discard(child.name)
                    taken.add(target_name)
                    summary["renamed"] += 1
                    yield {**base_event, "action": "rename", "target": target_name, "reason": None}
                else:
                    summary["skipped"] += 1
                    yield {**base_event, "action": "skip", "target": target_name, "reason": "already_clean"}

                if recursive and _depth < 1:
                    async for evt in self.cleanup_folder_stream(
                        child.id, dry_run=dry_run, recursive=recursive,
                        _depth=_depth + 1,
                    ):
                        # Bubble inner progress up with a "nested:" tag
                        # so the UI shows which wrapper they belong to.
                        if evt.get("type") == "progress":
                            yield {**evt, "nested_in": child.name}
                        # Skip inner start/done — the outer one represents
                        # the whole tree.

            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning("cleanup failed for %s: %s", child.name, exc)
                yield {**base_event, "action": "error", "target": None, "reason": str(exc)}

        if _depth == 0:
            yield {"type": "done", "result": summary}


pikpak_service = PikPakService()
