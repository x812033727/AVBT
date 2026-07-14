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
import time
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


# Credential-login cooldowns. Background loops (archiver sweep, tracker,
# download queue) funnel through ``_ensure``; without a cooldown, a dead
# token + throttled login means every cycle re-hits PikPak's login API,
# refreshing the "operation too frequent" window forever — the account
# never recovers and manual logins keep failing too.
_LOGIN_COOLDOWN_GENERIC = 300  # wrong password / network / unknown
_LOGIN_COOLDOWN_TOO_FREQUENT = 1800  # throttled: start at 30 min...
_LOGIN_COOLDOWN_MAX = 6 * 3600  # ...doubling up to 6 h


def _is_too_frequent_error(exc: BaseException) -> bool:
    """True when PikPak throttled us ("operation is too frequent")."""
    return "too frequent" in str(exc).lower()


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

# How deep "整理此資料夾" recurses through grouping (no-code) folders to
# reach 番號 leaves. 製作商(0)→studio(1)→系列(2)→番號 leaf(3), +1 slack.
_ORGANIZE_MAX_DEPTH = 4


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
        # Login-failure cooldown state (see module constants above).
        self._login_blocked_until: float = 0.0
        self._login_block_reason: str = ""
        self._too_frequent_streak: int = 0

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

    # ---------- login cooldown ----------

    def _login_cooldown_remaining(self) -> float:
        return max(0.0, self._login_blocked_until - time.monotonic())

    def _raise_if_login_blocked(self, *, explicit: bool) -> None:
        """Fail fast while a login cooldown is active, without touching
        the network. Explicit (user-supplied credential) logins bypass
        the generic cooldown — the user may have just fixed a typo'd
        password — but still respect a too-frequent cooldown, because
        every attempt inside PikPak's throttle window refreshes it."""
        remaining = self._login_cooldown_remaining()
        if remaining <= 0:
            return
        if explicit and self._too_frequent_streak == 0:
            return
        minutes = max(1, int(remaining // 60) + (1 if remaining % 60 else 0))
        raise PikPakError(
            f"登入冷卻中(約剩 {minutes} 分鐘)。"
            f"上次失敗原因: {self._login_block_reason}"
        )

    def _note_login_failure(self, exc: BaseException) -> None:
        if _is_too_frequent_error(exc):
            self._too_frequent_streak += 1
            cooldown = min(
                _LOGIN_COOLDOWN_MAX,
                _LOGIN_COOLDOWN_TOO_FREQUENT
                * 2 ** (self._too_frequent_streak - 1),
            )
        else:
            self._too_frequent_streak = 0
            cooldown = _LOGIN_COOLDOWN_GENERIC
        self._login_blocked_until = time.monotonic() + cooldown
        self._login_block_reason = str(exc)
        logger.warning(
            "PikPak login failed (%s); cooling down %ds", exc, cooldown
        )

    def _clear_login_cooldown(self) -> None:
        self._login_blocked_until = 0.0
        self._login_block_reason = ""
        self._too_frequent_streak = 0

    # ---------- public ----------

    def status(self) -> dict:
        return {
            "logged_in": self._client is not None,
            "username": self._username,
            "has_stored_token": TOKEN_FILE.exists(),
            "has_env_credentials": bool(
                settings.pikpak_username and settings.pikpak_password
            ),
            "login_cooldown_seconds": int(self._login_cooldown_remaining()),
            "login_block_reason": self._login_block_reason,
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
                self._raise_if_login_blocked(explicit=True)
                client = PikPakApi(
                    **self._build_kwargs(username=username, password=password)
                )
                try:
                    await client.login()
                except Exception as exc:  # noqa: BLE001
                    self._note_login_failure(exc)
                    raise
                self._clear_login_cooldown()
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

            self._raise_if_login_blocked(explicit=False)
            client = PikPakApi(
                **self._build_kwargs(username=env_user, password=env_pwd)
            )
            try:
                await client.login()
            except Exception as exc:  # noqa: BLE001
                self._note_login_failure(exc)
                raise
            self._clear_login_cooldown()
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
            # A working token ends any credential-login cooldown — the
            # account is demonstrably usable again.
            self._clear_login_cooldown()
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
        Returns ``""`` when the path doesn't exist.

        pikpakapi's ``path_to_id(create=False)`` resolves as far as it
        can and returns the PARTIAL segment list when something in the
        middle is missing — i.e. the deepest existing ancestor, not a
        miss. Callers here treat a non-empty result as "the folder
        exists", so accepting a partial match silently redirects them
        at the ancestor (e.g. a missing ``…/未分類/CODE-1`` resolving
        to the whole ``未分類`` folder). Require a full-length,
        folder-typed resolution."""
        if not name:
            return ""
        if name in self._folder_cache:
            return self._folder_cache[name]
        path = name if name.startswith("/") else f"/{name}"
        try:
            result = await self._call(lambda c: c.path_to_id(path, create=False))
        except Exception:  # noqa: BLE001
            return ""
        segments = [p for p in path.split("/") if p.strip()]
        folder_id = ""
        if isinstance(result, list) and len(result) == len(segments):
            leaf = result[-1] or {}
            if leaf.get("file_type") != "file":
                folder_id = leaf.get("id", "")
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
            phase=f.get("phase", "") or "",
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

    async def delete_forever(self, ids: list[str]) -> dict:
        """Permanently delete files — NOT recoverable. Trash first, then
        purge: that's the sequence the PikPak web UI uses and the one the
        API honours reliably. Only the finalize junk-purge path (non-video
        files / ad clips) should ever call this."""
        await self._run_batch(ids, lambda c, ch: c.delete_to_trash(ch))
        return await self._run_batch(ids, lambda c, ch: c.delete_forever(ch))

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
            elif getattr(c, "phase", "") not in ("", "PHASE_TYPE_COMPLETE"):
                # Still being written by an offline task — its final size
                # is unknown, so count it as a main video. That blocks the
                # single-video flatten (which would trash the wrapper with
                # the half-transferred file inside) until the task lands.
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

    async def _trash_if_empty(
        self, folder_id: str, *, protect_ids: frozenset[str] = frozenset()
    ) -> bool:
        """Re-list ``folder_id`` and trash it (recoverable ~30 days) only
        when it holds zero children and is not protected. Returns True if
        trashed. The fresh re-list is the final safety gate before any
        deletion, so a folder another process just repopulated is spared;
        a non-empty or protected folder is never touched."""
        if not folder_id or folder_id in protect_ids:
            return False
        try:
            kids, _partial = await self.list_all_files(folder_id)
        except Exception:  # noqa: BLE001
            return False
        if kids:
            return False
        try:
            await self.trash_files([folder_id])
        except Exception:  # noqa: BLE001
            return False
        return True

    async def cleanup_folder_stream(
        self,
        folder_id: str,
        *,
        dry_run: bool = True,
        recursive: bool = True,
        _depth: int = 0,
        _organize: bool = True,
        _protect_ids: frozenset[str] | None = None,
        _summary: dict | None = None,
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

        # One summary dict is shared across the whole recursion so the
        # final ``done`` reflects every level's work. ``total`` is a
        # running counter (recursion makes the true total unknown up
        # front); the ``start`` event's total is just the top-level child
        # count — a lower bound the UI clamps against.
        if _summary is None:
            _summary = {
                "total": 0,
                "renamed": 0,
                "flattened": 0,
                "moved": 0,
                "skipped": 0,
                "trashed": 0,
                "errors": 0,
                "dry_run": dry_run,
                "partial": partial,
            }
        summary = _summary
        if partial:
            summary["partial"] = True

        # Folders we must never trash even when empty: the caller's
        # selected root, the AVBT download root, the archive fallback,
        # and every kind base (e.g. ``AVBT/製作商``). Built once at the
        # top level via a no-create lookup so it's dry-run safe.
        if _protect_ids is None:
            from ..config import all_kind_paths
            protect: set[str] = {folder_id}
            for p in ("AVBT", settings.pikpak_archive_folder or "AVBT/已完成"):
                pid = await self.lookup_folder_id(p)
                if pid:
                    protect.add(pid)
            for _k, kp in all_kind_paths():
                kid = await self.lookup_folder_id(kp)
                if kid:
                    protect.add(kid)
            _protect_ids = frozenset(protect)

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

        # Per-level tallies drive empty-folder detection (works in
        # dry-run, where we can't re-list a not-yet-mutated folder).
        level_removed = 0    # child moved / flattened / recursed-empty out
        level_remaining = 0  # child still present after processing

        for child in children:
            await asyncio.sleep(0.05)
            summary["total"] += 1
            kind = "folder" if child.kind == "drive#folder" else "file"
            code = extract_jav_code(child.name)
            code_full = extract_jav_code_full(child.name) or code
            base_event = {
                "type": "progress",
                "current": summary["total"],
                "kind": kind,
                "source": child.name,
            }

            # No recognisable 番號: descend into grouping sub-folders
            # (製作商/廠商/系列) to reach the leaves; loose codeless files
            # are left in place. A grouping folder emptied by the descent
            # is trashed (recoverable).
            if not code:
                if (
                    kind == "folder"
                    and recursive
                    and _depth < _ORGANIZE_MAX_DEPTH
                    and child.id not in _protect_ids
                ):
                    became_empty = False
                    async for evt in self.cleanup_folder_stream(
                        child.id, dry_run=dry_run, recursive=recursive,
                        _depth=_depth + 1, _organize=_organize,
                        _protect_ids=_protect_ids, _summary=summary,
                    ):
                        if evt.get("type") == "_became_empty":
                            became_empty = bool(evt.get("empty"))
                            continue
                        if evt.get("type") == "progress":
                            yield {**evt, "nested_in": child.name}
                        else:
                            yield evt
                    if became_empty:
                        did_trash = (
                            child.id not in _protect_ids if dry_run
                            else await self._trash_if_empty(
                                child.id, protect_ids=_protect_ids
                            )
                        )
                        if did_trash:
                            summary["trashed"] += 1
                            level_removed += 1
                            yield {**base_event, "action": "trash",
                                   "target": None, "reason": "空資料夾已刪除"}
                        else:
                            level_remaining += 1
                    else:
                        level_remaining += 1
                    continue
                summary["skipped"] += 1
                level_remaining += 1
                yield {**base_event, "action": "skip", "target": None, "reason": "no_code"}
                continue

            try:
                # Resolve this code's correct archive folder. If the item
                # is sitting elsewhere, move it there before normalising.
                # "Already here" is decided by comparing the target parent
                # FOLDER ID to the current folder id — never path strings —
                # so we never issue a redundant move that PikPak rejects
                # with "don't move to current folder". A missing target
                # (lookup returns "") is treated as not-misplaced so
                # dry-run and real-run agree (no folder is auto-created
                # just to detect misplacement).
                effective_parent_id = folder_id
                target_parent_path = ""
                target_parent_id = ""
                try:
                    from ..services.archiver import (
                        _resolve_archive_path_by_code,
                    )
                    _target_path = await _resolve_archive_path_by_code(code)
                    if "/" in _target_path:
                        target_parent_path, _tleaf = _target_path.rsplit("/", 1)
                        target_parent_id = (
                            await self.lookup_folder_id(target_parent_path) or ""
                        )
                except Exception:  # noqa: BLE001
                    target_parent_id = ""
                # ``_organize`` is off while we clean the *inside* of a
                # known 番號 wrapper — its videos are already home relative
                # to that code, so don't yank them one level up.
                misplaced = (
                    _organize
                    and bool(target_parent_id)
                    and target_parent_id != folder_id
                )

                if misplaced and kind == "file":
                    dest_leaf = f"{code_full}{ext_of(child.name)}"
                    display_target = f"{target_parent_path}/{dest_leaf}"
                    if not dry_run:
                        await self.move_files([child.id], target_parent_id)
                        if child.name != dest_leaf:
                            try:
                                await self.rename_file(child.id, dest_leaf)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "rename after move %s → %s failed: %s",
                                    child.name, dest_leaf, exc,
                                )
                    taken.discard(child.name)
                    summary["moved"] += 1
                    level_removed += 1
                    yield {**base_event, "action": "move",
                           "target": display_target, "reason": None}
                    continue
                if misplaced and kind == "folder":
                    # Flatten will pull the video straight into the correct
                    # 製作商/<studio>/<系列> folder instead of here.
                    effective_parent_id = target_parent_id

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
                        level_remaining += 1
                        yield {**base_event, "action": "skip", "target": child.name, "reason": "already_clean"}
                        continue
                    else:
                        target = f"{code_full}{ext_of(child.name)}"
                    if target == child.name:
                        summary["skipped"] += 1
                        level_remaining += 1
                        yield {**base_event, "action": "skip", "target": target, "reason": "already_clean"}
                        continue
                    # Auto-dedupe on collision instead of silently skipping.
                    target = _uniquify_target(target, taken)
                    if not dry_run:
                        await self.rename_file(child.id, target)
                    taken.discard(child.name)
                    taken.add(target)
                    summary["renamed"] += 1
                    level_remaining += 1
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
                            await self.move_files([video.id], effective_parent_id)
                        taken.add(target)
                        moved.append(target)

                    if not dry_run:
                        # Wrapper trash takes leftover junk + any
                        # dropped lower-resolution duplicates.
                        await self.trash_files([child.id])
                    taken.discard(child.name)
                    summary["flattened"] += 1
                    # In place, the video stays in this folder (still has
                    # content); only a flatten to a *different* target
                    # actually empties this folder of the wrapper.
                    if misplaced:
                        level_removed += 1
                    else:
                        level_remaining += 1
                    reason_bits: list[str] = []
                    if len(keepers) > 1:
                        reason_bits.append(f"分集 {len(keepers)} 部")
                    if dropped_count:
                        reason_bits.append(f"丟掉 {dropped_count} 個低解析重複")
                    loc = f"{target_parent_path}/" if misplaced else ""
                    yield {
                        **base_event,
                        "action": "flatten",
                        "target": " / ".join(f"{loc}{m}" for m in moved),
                        "reason": "・".join(reason_bits) or None,
                    }
                    continue

                # Mixed / weird contents (2+ main videos, or none). If the
                # wrapper is in the wrong place, move it whole to its
                # correct 製作商/系列 folder; otherwise rename in place.
                # Then recurse to clean its insides — with organize OFF, so
                # its own videos stay in the wrapper — and trash it if the
                # recursion leaves it empty.
                child_moved_out = False
                if misplaced:
                    dest_leaf = code_full
                    display_target = f"{target_parent_path}/{dest_leaf}"
                    if not dry_run:
                        await self.move_files([child.id], target_parent_id)
                        if child.name != dest_leaf:
                            try:
                                await self.rename_file(child.id, dest_leaf)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "rename after move %s → %s failed: %s",
                                    child.name, dest_leaf, exc,
                                )
                    taken.discard(child.name)
                    summary["moved"] += 1
                    level_removed += 1
                    child_moved_out = True
                    yield {**base_event, "action": "move",
                           "target": display_target, "reason": None}
                else:
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

                child_empty = False
                if recursive and _depth < _ORGANIZE_MAX_DEPTH:
                    async for evt in self.cleanup_folder_stream(
                        child.id, dry_run=dry_run, recursive=recursive,
                        _depth=_depth + 1, _organize=False,
                        _protect_ids=_protect_ids, _summary=summary,
                    ):
                        if evt.get("type") == "_became_empty":
                            child_empty = bool(evt.get("empty"))
                            continue
                        # Bubble inner progress up with a "nested:" tag
                        # so the UI shows which wrapper they belong to.
                        if evt.get("type") == "progress":
                            yield {**evt, "nested_in": child.name}
                        elif evt.get("type") != "_became_empty":
                            yield evt

                if child_empty and child.id not in _protect_ids:
                    did_trash = (
                        True if dry_run
                        else await self._trash_if_empty(
                            child.id, protect_ids=_protect_ids
                        )
                    )
                    if did_trash:
                        summary["trashed"] += 1
                        if not child_moved_out:
                            level_removed += 1
                        yield {**base_event, "action": "trash",
                               "target": None, "reason": "空資料夾已刪除"}
                    elif not child_moved_out:
                        level_remaining += 1
                elif not child_moved_out:
                    level_remaining += 1

            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                level_remaining += 1
                logger.warning("cleanup failed for %s: %s", child.name, exc)
                yield {**base_event, "action": "error", "target": None, "reason": str(exc)}

        # Tell the parent whether this folder is now empty (every child was
        # moved/flattened/trashed away and none remained), so it can trash
        # the shell. Private sentinel — filtered out before the router.
        if _depth > 0:
            yield {
                "type": "_became_empty",
                "empty": level_remaining == 0
                and level_removed > 0
                and len(children) > 0,
            }
        if _depth == 0:
            yield {"type": "done", "result": summary}


pikpak_service = PikPakService()
