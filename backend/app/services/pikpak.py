"""Thin async wrapper around PikPakAPI.

The library exposes a single ``PikPakApi`` class. We cache a singleton
instance (re-using its refresh token across requests) and expose only the
operations we need: login, offline_download, list tasks/files, delete, etc.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from pikpakapi import PikPakApi

from ..config import settings, task_folder_path
from ..schemas import OfflineSubmit, PikPakFile, PikPakQuota, PikPakTask
from .jav_code import ext_of, extract_jav_code, extract_jav_code_full, is_video


def _uniquify_target(target: str, taken: set[str]) -> str:
    """Return ``target`` if free, otherwise ``<stem> (2).<ext>`` / (3) /
    (4) … until it doesn't collide with anything in ``taken``."""
    if target not in taken:
        return target
    if "." in target:
        stem, _, ext = target.rpartition(".")
        ext = f".{ext}"
    else:
        stem, ext = target, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}){ext}"
        if candidate not in taken:
            return candidate
        n += 1


# Strip suffixes that mark a file as a re-download / variant / part of
# the SAME canonical work:
#   "(N)"   — PikPak's auto-dedupe on download collision
#   "_N"    — our preferred multi-part convention (so we stay idempotent
#             once files have been renamed once)
#   quality tags — HD / 720p / 1080p / 高清 / …
# CD1/CD2 / variant letters A/B/C live on the BASE side of the regex
# and survive (they mark different content).
_DUP_SUFFIX_RE = re.compile(
    r"\s*(?:\(\d+\)|_\d+|HD|FHD|UHD|4K|2K|8K|720P|1080P|2160P|4320P|高清|超清)\s*$",
    re.IGNORECASE,
)


def _canonical_video_name(name: str) -> str:
    """Return ``name`` with extension + resolution / dup / part-index
    suffixes stripped, upper-cased. Files whose canonical form matches
    are treated as the same canonical work — they get grouped, large
    ones become parts, small ones get dropped."""
    stem = name
    # Strip extension once.
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", stem)
    if m:
        stem = stem[: m.start()]
    # Repeatedly strip dup suffixes (e.g. "name (2) HD" → "name").
    prev = None
    while prev != stem:
        prev = stem
        stem = _DUP_SUFFIX_RE.sub("", stem).strip()
    return stem.upper()


def _dup_sort_index(name: str) -> int:
    """Extract the PikPak ``(N)`` suffix as an int; "no suffix" = 0.
    Used to order files within a multi-part group so the bare-name one
    becomes ``_1`` and ``(2)``/``(3)``/... follow naturally."""
    m = re.search(r"\((\d+)\)", name)
    return int(m.group(1)) if m else 0


_PART_INDEX_RE = re.compile(r"^(.+)_(\d+)$")


def _build_video_rename_plan(
    children: list,  # list[PikPakFile]; type kept loose to avoid forward ref
    min_size: int,
    is_video_fn,
) -> tuple[dict[str, str], set[str]]:
    """Pre-scan video children and return ``(plan, group_members)``:

    - ``plan`` — ``{current_name: target_name}`` covering two corrections:

      1. **Lonely variant** — if a base code has ≤ 1 file with a trailing
         variant letter (``SDMM-14903A`` alone, no ``B`` companion), the
         letter is meaningless and gets stripped so the file becomes
         ``<base>.<ext>``.
      2. **Multi-part group** — when 2+ video files share a canonical and
         all of them are ≥ ``min_size``, rename them to
         ``<canonical>_N.<ext>`` (sorted by PikPak ``(N)`` suffix; bare
         name = 0 = ``_1``). Files already in this form keep their slot
         so re-running cleanup is a no-op.

    - ``group_members`` — every filename that belongs to a multi-part
      group (whether or not it's in ``plan``). The caller uses this to
      avoid blindly applying the single-file default name to a member
      that's already correctly named — without this guard, on a second
      run ``SDMM-053_1.mp4``, ``_2``, ``_3``, ``_4`` would all collapse
      to ``SDMM-053.mp4`` + ``(2)/(3)/(4)`` dedup suffixes.
    """
    # Pass 1: count files-with-variant per base code, so we know which
    # variants are "lonely" and should be stripped.
    variant_count: dict[str, int] = {}
    for c in children:
        if getattr(c, "kind", "") == "drive#folder" or not is_video_fn(c.name):
            continue
        base = extract_jav_code(c.name)
        if not base:
            continue
        full = extract_jav_code_full(c.name) or base
        if full != base:
            variant_count[base] = variant_count.get(base, 0) + 1

    # Pass 2: compute each file's effective canonical (variant possibly
    # stripped) and bucket files by it.
    file_effective: dict[str, str] = {}  # name → effective_full_code
    groups: dict[str, list] = {}
    for c in children:
        if getattr(c, "kind", "") == "drive#folder" or not is_video_fn(c.name):
            continue
        base = extract_jav_code(c.name) or ""
        full = extract_jav_code_full(c.name) or base
        is_lonely = bool(base) and full != base and variant_count.get(base, 0) <= 1
        effective = base if is_lonely else full
        file_effective[c.name] = effective
        canon = _canonical_video_name(c.name)
        if is_lonely and full and full.upper() in canon:
            canon = canon.replace(full.upper(), base.upper(), 1)
        groups.setdefault(canon, []).append(c)

    plan: dict[str, str] = {}
    group_members: set[str] = set()
    for canon, files in groups.items():
        if len(files) == 1:
            # Singleton: only acts if it has a lonely variant to strip.
            c = files[0]
            effective = file_effective[c.name]
            full = extract_jav_code_full(c.name) or ""
            if effective and full and effective.upper() != full.upper():
                ext = ext_of(c.name)
                target = f"{effective}{ext}"
                if target != c.name:
                    plan[c.name] = target
            continue
        # Multi-file group: multipart naming if all substantial.
        if not all((f.size or 0) >= min_size for f in files):
            continue
        # Members get protected from the single-file default-name path.
        for f in files:
            group_members.add(f.name)
        used_indices: set[int] = set()
        unnamed: list = []
        for f in files:
            ext = ext_of(f.name)
            stem = f.name[: -len(ext)] if ext else f.name
            m = _PART_INDEX_RE.match(stem)
            if m and m.group(1).upper() == canon:
                used_indices.add(int(m.group(2)))
            else:
                unnamed.append(f)
        if not unnamed:
            continue  # already fully named
        unnamed.sort(key=lambda f: (_dup_sort_index(f.name), f.name))
        n = 1
        for f in unnamed:
            while n in used_indices:
                n += 1
            ext = ext_of(f.name)
            plan[f.name] = f"{canon}_{n}{ext}"
            used_indices.add(n)
            n += 1
    return plan, group_members


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

    async def lookup_folder_id(self, name: Optional[str]) -> str:
        """Like ``folder_id`` but does NOT auto-create missing segments.
        Returns ``""`` when the path doesn't exist."""
        if not name:
            return ""
        if name in self._folder_cache:
            return self._folder_cache[name]
        client = await self._ensure()
        path = name if name.startswith("/") else f"/{name}"
        try:
            result = await client.path_to_id(path, create=False)
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
        client = await self._ensure()
        # Default to the dedicated task folder (AVBT/TASK) instead of the
        # download root — keeps BT-noise wrappers from polluting AVBT/.
        folder = payload.folder or task_folder_path()
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
        client = await self._ensure()
        files: list[PikPakFile] = []
        token = ""
        size = 500

        try:
            while True:
                resp = await client.file_list(
                    parent_id=parent_id, size=size, next_page_token=token
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
            resp = await client.file_list(parent_id=parent_id, size=size)
            if isinstance(resp, dict):
                for f in resp.get("files", []) or []:
                    files.append(self._file_from_raw(f))
            partial = bool(resp.get("next_page_token")) if isinstance(resp, dict) else False
            return files, partial

        return files, False

    async def trash_files(self, ids: list[str]) -> dict:
        client = await self._ensure()
        return await client.delete_to_trash(ids)

    async def move_files(self, ids: list[str], to_parent_id: str) -> dict:
        client = await self._ensure()
        return await client.file_batch_move(ids, to_parent_id)

    async def rename_file(self, file_id: str, new_name: str) -> dict:
        client = await self._ensure()
        return await client.file_rename(file_id, new_name)

    async def file_links(self, file_id: str) -> dict:
        """Return ``{download_url, play_url, mime_type}`` for a single file.

        - ``download_url`` is the progressive download link (good for non-video
          files and as a fallback when ``<video>`` can't decode the format).
        - ``play_url`` is the high-speed streaming link from ``medias[0].link``
          (falls back to ``download_url`` if PikPak didn't surface one).
        """
        client = await self._ensure()
        resp = await client.get_download_url(file_id)
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
