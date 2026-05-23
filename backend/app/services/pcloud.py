"""Thin async wrapper around the pCloud HTTP API.

pCloud exposes a simple JSON-over-HTTPS API. We talk to it directly with
httpx — no third-party client, no async/sync bridging.

The service is shaped to mirror :class:`PikPakService` so the frontend
can interact with both providers through near-identical endpoints. Only
the cloud-storage management subset is implemented: list, search, move,
rename, create folder, delete, plus a JAV-code cleanup pass that reuses
PikPak's filename-normalisation helpers.

pCloud has two regional API hosts (US ``api.pcloud.com``, EU
``eapi.pcloud.com``) and an account only authenticates against its own
region. On first login we try US, then EU; the detected host is
persisted alongside the auth token so subsequent restarts reuse the
right endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from ..config import settings
from ..schemas import PCloudFile, PCloudQuota
from .jav_code import ext_of, extract_jav_code, extract_jav_code_full, is_video
from .pikpak import _build_video_rename_plan, _uniquify_target


logger = logging.getLogger(__name__)


class PCloudError(RuntimeError):
    """pCloud-side error.

    Carries:
      - ``.result`` — the numeric ``result`` code from the JSON body
        (0 if not from a structured response).
      - ``.payload`` — the full parsed response dict, when available,
        so callers can introspect undocumented hint fields (e.g.
        ``tfatoken``, ``hint``, ``region``) without re-parsing.
    """

    result: int = 0
    payload: Optional[dict] = None


TOKEN_FILE = Path("data/pcloud_token.json")

PCLOUD_HOSTS = (
    "https://api.pcloud.com",   # US
    "https://eapi.pcloud.com",  # EU
)

# pCloud server-side result codes that mean "your stored auth token is
# no longer accepted" — typically expired sessions or manual revocation.
# When we see one of these during a normal API call (i.e. we already
# attached an auth token), drop the cached token and re-login from env
# credentials. ``2000`` is NOT included here on purpose: that's "Log in
# failed" during the initial login flow, not a stale-token signal.
_INVALID_AUTH_RESULTS = frozenset({1000, 2094, 2095})


def _to_pcloud_file(item: dict) -> PCloudFile:
    """Normalise a pCloud listfolder entry into our schema."""
    is_folder = bool(item.get("isfolder"))
    raw_id = item.get("folderid") if is_folder else item.get("fileid")
    parent_raw = item.get("parentfolderid")
    return PCloudFile(
        id=str(raw_id) if raw_id is not None else "",
        name=str(item.get("name", "")),
        kind="folder" if is_folder else "file",
        size=int(item["size"]) if item.get("size") is not None else None,
        parent_id=str(parent_raw) if parent_raw is not None else None,
        created_time=item.get("created"),
    )


class PCloudService:
    def __init__(self) -> None:
        self._auth: Optional[str] = None
        self._host: str = PCLOUD_HOSTS[0]
        self._username: str = ""
        self._userid: Optional[int] = None
        self._lock = asyncio.Lock()

    # ---------- token persistence ----------

    def _load_token(self) -> dict | None:
        if not TOKEN_FILE.exists():
            return None
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("auth"):
            return None
        return data

    def _save_token(self, auth: str, host: str, username: str) -> None:
        if not auth:
            return
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(
            json.dumps({"auth": auth, "host": host, "username": username}),
            encoding="utf-8",
        )
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass

    def _clear_token(self) -> None:
        try:
            TOKEN_FILE.unlink()
        except FileNotFoundError:
            pass

    # ---------- public ----------

    def status(self) -> dict:
        host = self._host or PCLOUD_HOSTS[0]
        region = "eu" if host.startswith("https://eapi.") else "us"
        return {
            "logged_in": bool(self._auth),
            "username": self._username,
            "host": host,
            "region": region,
            "user_id": self._userid or 0,
            "has_stored_token": TOKEN_FILE.exists(),
            "has_env_credentials": bool(
                settings.pcloud_username and settings.pcloud_password
            ),
            "has_env_token": bool(settings.pcloud_access_token),
            "default_folder": settings.pcloud_default_folder or "/",
        }

    def logout(self) -> None:
        self._auth = None
        self._username = ""
        self._userid = None
        self._clear_token()

    # ---------- raw HTTP ----------

    def _client_args(self) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if settings.http_proxy:
            args["proxy"] = settings.http_proxy
        return args

    async def _raw_request(
        self,
        host: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        auth: Optional[str] = None,
    ) -> dict:
        """One pCloud HTTP call. Returns the parsed JSON body or raises
        :class:`PCloudError` when ``result != 0``.

        Caller is responsible for adding ``auth`` (we do it here when
        ``auth`` is provided) and for picking the right host.
        """
        url = f"{host}/{method}"
        q = dict(params or {})
        if auth:
            q["auth"] = auth
        timeout = float(settings.pcloud_api_timeout_seconds or 0) or None
        try:
            async with httpx.AsyncClient(timeout=timeout, **self._client_args()) as client:
                resp = await client.get(url, params=q)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise PCloudError(
                f"pCloud API 逾時 ({settings.pcloud_api_timeout_seconds:.0f}s)"
            ) from exc
        except httpx.HTTPError as exc:
            raise PCloudError(f"pCloud HTTP 失敗: {exc}") from exc

        if not isinstance(data, dict):
            raise PCloudError(f"pCloud 回應非預期格式: {data!r}")
        code = int(data.get("result") or 0)
        if code != 0:
            err = data.get("error") or f"result={code}"
            exc = PCloudError(f"pCloud 錯誤 ({code}): {err}")
            exc.result = code  # type: ignore[attr-defined]
            exc.payload = data  # type: ignore[attr-defined]
            raise exc
        return data

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Call a pCloud method with the current auth, auto-retrying once
        if the token has been invalidated server-side (re-logs in from
        ``.env`` credentials when available)."""
        auth = await self._ensure_auth()
        try:
            return await self._raw_request(self._host, method, params, auth=auth)
        except PCloudError as exc:
            code = getattr(exc, "result", 0)
            if code not in _INVALID_AUTH_RESULTS:
                raise
            logger.warning("pCloud auth invalidated (result=%s); re-logging in", code)
            await self._drop_for_relogin()
            auth = await self._ensure_auth()
            return await self._raw_request(self._host, method, params, auth=auth)

    async def _drop_for_relogin(self) -> None:
        async with self._lock:
            self._auth = None
            self._clear_token()

    # ---------- login ----------

    async def _login_detect_host(
        self, username: str, password: str
    ) -> tuple[str, str, int | None]:
        """Try US first, then EU. Returns ``(auth, host, userid)``.

        Uses pCloud's recommended **digest authentication** instead of
        passing the password in the URL:

          1. ``GET /getdigest`` → fresh nonce
          2. ``passworddigest = sha1(password + sha1_hex(lower(user)) + digest)``
          3. ``GET /userinfo?username=&digest=&passworddigest=&getauth=1``

        This avoids two classes of "Log in failed" we've seen with the
        plain-password flow: passwords containing characters that need
        URL-encoding (the server sometimes mishandles ``+`` / ``%``),
        and longer passwords that get rejected silently when on the query
        string. The hash digest is short, ASCII, and stable.

        pCloud's documented "wrong region" response is ``result: 2321``,
        but in practice it sometimes returns the generic ``result: 2000``
        ("Log in failed") when an EU account hits the US endpoint. So
        we try every host and only surface the **first** error if every
        host rejected us — that way a genuinely-wrong password still
        produces a useful message.

        2FA accounts: pCloud returns ``result: 2229`` (or sometimes
        ``2297``) on the userinfo call when TFA is required. We translate
        those into a clearer Chinese message pointing at the cause.
        """
        first_error: Optional[PCloudError] = None
        user_lower = username.strip().lower()
        for host in PCLOUD_HOSTS:
            host_label = "US" if "eapi" not in host else "EU"
            # Try digest first (works without exposing password in URL),
            # then fall back to plain password over HTTPS. pCloud's docs
            # list both as supported; in practice we've seen accounts
            # where one path fails and the other works, so we try both
            # before declaring the credentials bad.
            attempts: list[tuple[str, dict[str, Any]]] = []
            try:
                digest_resp = await self._raw_request(host, "getdigest")
                digest = str(digest_resp.get("digest") or "")
                if digest:
                    user_hash = hashlib.sha1(
                        user_lower.encode("utf-8")
                    ).hexdigest()
                    pw_digest = hashlib.sha1(
                        (password + user_hash + digest).encode("utf-8")
                    ).hexdigest()
                    attempts.append(
                        (
                            "digest",
                            {
                                "username": username,
                                "digest": digest,
                                "passworddigest": pw_digest,
                                "getauth": 1,
                                "logout": 0,
                            },
                        )
                    )
            except PCloudError as exc:
                logger.warning(
                    "pCloud getdigest failed host=%s err=%s",
                    host_label,
                    exc,
                )
            # Always also queue the plain-password attempt as fallback.
            attempts.append(
                (
                    "plain",
                    {
                        "username": username,
                        "password": password,
                        "getauth": 1,
                        "logout": 0,
                    },
                )
            )

            data: Optional[dict] = None
            last_attempt_error: Optional[PCloudError] = None
            for kind, params in attempts:
                logger.info(
                    "pCloud login attempt user=%s host=%s method=%s pw_len=%d",
                    username,
                    host_label,
                    kind,
                    len(password),
                )
                try:
                    data = await self._raw_request(host, "userinfo", params)
                    break
                except PCloudError as exc:
                    last_attempt_error = exc
                    code = getattr(exc, "result", 0)
                    payload = getattr(exc, "payload", None) or {}
                    logger.warning(
                        "pCloud login rejected user=%s host=%s method=%s "
                        "result=%s payload=%s",
                        username,
                        host_label,
                        kind,
                        code,
                        payload,
                    )
                    # Fast-path the truly terminal cases — no point trying
                    # the alternate method or the other DC.
                    if code in (2229, 2297) or payload.get("tfatoken"):
                        raise PCloudError(
                            "此 pCloud 帳號開啟了 2FA(二步驟驗證),目前不支援。"
                            "請到 pCloud 設定關閉 2FA,或在 pCloud 開發者頁產生 "
                            "Access Token 後用 token 登入。"
                        ) from exc
                    if code == 4000:
                        raise PCloudError(
                            "pCloud 因為太多失敗登入嘗試暫時封鎖此 IP,"
                            "請等幾分鐘後再試,或改用 Access Token 登入。"
                        ) from exc
            if data is None:
                if first_error is None and last_attempt_error is not None:
                    first_error = last_attempt_error
                continue
            auth = str(data.get("auth") or "")
            if not auth:
                # Some 2FA flows return result=0 but no auth + a tfatoken
                # — translate that too.
                if data.get("tfatoken") or data.get("tfa_required"):
                    raise PCloudError(
                        "此 pCloud 帳號需要 2FA 驗證,目前不支援。"
                        "請改用 Access Token 登入。"
                    )
                raise PCloudError("pCloud 登入回應未含 auth token")
            userid = data.get("userid")
            try:
                userid_int = int(userid) if userid is not None else None
            except (TypeError, ValueError):
                userid_int = None
            logger.info(
                "pCloud login success user=%s host=%s userid=%s",
                username,
                host_label,
                userid_int,
            )
            return auth, host, userid_int
        # Every host rejected us. If the rejection was the generic
        # "Log in failed" (2000), surface the three real-world causes so
        # the user can self-diagnose instead of staring at a one-liner.
        if first_error is not None:
            if getattr(first_error, "result", 0) == 2000:
                # Include any extra hint fields pCloud sent — sometimes
                # they include ``hint``, ``message`` or region fields
                # that explain why the same password works on the web
                # but not via the public API (e.g. account-level OAuth
                # enforcement).
                payload = getattr(first_error, "payload", None) or {}
                extra_fields = {
                    k: v
                    for k, v in payload.items()
                    if k not in {"result", "error"} and v not in (None, "", 0)
                }
                hint_block = (
                    f"\nserver 附帶欄位: {extra_fields}" if extra_fields else ""
                )
                raise PCloudError(
                    "pCloud 登入失敗(帳密被拒,digest 與 plain 兩種都試過)。\n"
                    "你能登入 pcloud.com 網頁 → 密碼是對的。\n"
                    "常見原因(網頁能登入但 API 不能):\n"
                    "  1) 此帳號是用「Sign in with Google」OAuth 建立的。"
                    "即使後來設了密碼,pCloud 對這類帳號的 公開 API 密碼登入 "
                    "有時會擋掉(只允許 web / OAuth flow)。\n"
                    "  2) 帳號開了 2FA / device verification。\n"
                    "  3) IP 區域被 pCloud 短期擋住。\n"
                    "建議解法:改用 Access Token。\n"
                    "  → 開瀏覽器登入 pcloud.com,開 DevTools 的 Network,"
                    "重新整理一次首頁,任何 api 請求的 query string 都會帶 "
                    "auth=xxxxxx,複製那串貼到「Access Token」欄位即可。"
                    + hint_block
                ) from first_error
            raise first_error
        raise PCloudError("pCloud 登入失敗:所有資料中心都拒絕了帳號")

    async def _verify_token(self, token: str) -> tuple[str, str, int | None]:
        """Smoke-test a raw auth token by hitting ``userinfo`` on each host
        until one accepts it. Returns ``(auth, host, userid)`` exactly
        like :meth:`_login_detect_host` so callers can reuse the same
        downstream code path."""
        first_error: Optional[PCloudError] = None
        for host in PCLOUD_HOSTS:
            try:
                data = await self._raw_request(host, "userinfo", auth=token)
            except PCloudError as exc:
                if first_error is None:
                    first_error = exc
                continue
            userid = data.get("userid")
            try:
                userid_int = int(userid) if userid is not None else None
            except (TypeError, ValueError):
                userid_int = None
            return token, host, userid_int
        if first_error is not None:
            raise first_error
        raise PCloudError("pCloud token 驗證失敗：所有資料中心都拒絕了")

    async def _ensure_auth(self) -> str:
        async with self._lock:
            if self._auth:
                return self._auth

            stored = self._load_token()
            if stored:
                self._auth = str(stored.get("auth"))
                self._host = str(stored.get("host") or PCLOUD_HOSTS[0])
                self._username = str(stored.get("username") or "")
                return self._auth

            # 1) .env access token wins over username/password.
            env_token = (settings.pcloud_access_token or "").strip()
            if env_token:
                auth, host, userid = await self._verify_token(env_token)
                self._auth = auth
                self._host = host
                self._userid = userid
                # ``userinfo`` returns email — capture for display.
                try:
                    info = await self._raw_request(host, "userinfo", auth=auth)
                    self._username = str(info.get("email") or "")
                except PCloudError:
                    self._username = ""
                self._save_token(auth, host, self._username)
                return auth

            env_user = settings.pcloud_username
            env_pwd = settings.pcloud_password
            if not env_user or not env_pwd:
                raise PCloudError(
                    "pCloud 尚未登入。請到 /pcloud 填入帳密 / token,或在 .env "
                    "設定 PCLOUD_USERNAME+PCLOUD_PASSWORD 或 PCLOUD_ACCESS_TOKEN。"
                )
            auth, host, userid = await self._login_detect_host(env_user, env_pwd)
            self._auth = auth
            self._host = host
            self._username = env_user
            self._userid = userid
            self._save_token(auth, host, env_user)
            return auth

    async def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> dict:
        """Explicit login. Caller can supply either username+password OR
        a raw access token (e.g. from pCloud's developer page). Token
        path verifies the token before caching it."""
        async with self._lock:
            if access_token:
                token = access_token.strip()
                if not token:
                    raise PCloudError("Token 不可空白")
                auth, host, userid = await self._verify_token(token)
                # Pull email so status() shows something meaningful.
                email = ""
                try:
                    info = await self._raw_request(host, "userinfo", auth=auth)
                    email = str(info.get("email") or "")
                except PCloudError:
                    pass
                self._auth = auth
                self._host = host
                self._username = email
                self._userid = userid
                self._save_token(auth, host, email)
                return {"username": email, "userid": userid, "host": host}
            if not username or not password:
                raise PCloudError("請填入帳號與密碼,或提供 access token")
            auth, host, userid = await self._login_detect_host(username, password)
            self._auth = auth
            self._host = host
            self._username = username
            self._userid = userid
            self._save_token(auth, host, username)
        return {"username": username, "userid": userid, "host": host}

    # ---------- quota ----------

    async def quota(self) -> PCloudQuota:
        data = await self._call("userinfo")
        return PCloudQuota(
            used=int(data.get("usedquota") or 0),
            limit=int(data.get("quota") or 0),
        )

    # ---------- files ----------

    @staticmethod
    def _folder_param(parent_id: str) -> int:
        """Convert our string folder id ("0" / "12345") back to an int
        for pCloud. Empty string → root (0). Anything non-numeric
        becomes an error so we don't silently mis-route."""
        s = (parent_id or "0").strip()
        if not s:
            return 0
        try:
            return int(s)
        except ValueError as exc:
            raise PCloudError(f"無效的 pCloud folder id: {parent_id!r}") from exc

    @staticmethod
    def _file_param(file_id: str) -> int:
        try:
            return int((file_id or "").strip())
        except ValueError as exc:
            raise PCloudError(f"無效的 pCloud file id: {file_id!r}") from exc

    async def list_files(
        self, parent_id: str = "0", size: int = 0
    ) -> list[PCloudFile]:
        """List direct children of a folder. ``size`` is accepted for
        signature compatibility with PikPak but ignored — pCloud's
        ``listfolder`` returns the whole listing in one shot."""
        folder = self._folder_param(parent_id)
        data = await self._call("listfolder", {"folderid": folder, "nofiles": 0})
        contents = ((data.get("metadata") or {}).get("contents") or [])
        return [_to_pcloud_file(c) for c in contents]

    async def list_all_files(
        self, parent_id: str = "0", *, cap: int = 5000
    ) -> tuple[list[PCloudFile], bool]:
        """Compatibility shim that matches :meth:`PikPakService.list_all_files`.

        pCloud returns the full listing in one call, so we just hand it
        back. ``partial`` is True only when the returned list reaches
        ``cap`` (defensive — typical folders have far fewer items).
        """
        files = await self.list_files(parent_id)
        if len(files) >= cap:
            return files[:cap], True
        return files, False

    async def search_files(
        self, keyword: str, parent_id: str = "0"
    ) -> list[PCloudFile]:
        """pCloud has no first-class search endpoint; we list the folder
        and filter client-side. Recursion isn't supported here — search
        the current folder only, same as PikPak's fallback path."""
        kw = (keyword or "").lower().strip()
        if not kw:
            return []
        files = await self.list_files(parent_id)
        return [f for f in files if kw in f.name.lower()]

    async def file_links(self, file_id: str) -> dict:
        """Return ``{download_url}`` for a single file (matches the
        PikPak shape so the frontend can reuse the same handler)."""
        data = await self._call("getfilelink", {"fileid": self._file_param(file_id)})
        hosts = data.get("hosts") or []
        path = data.get("path") or ""
        if not hosts or not path:
            return {"download_url": "", "play_url": "", "mime_type": ""}
        url = f"https://{hosts[0]}{path}"
        return {"download_url": url, "play_url": url, "mime_type": ""}

    async def trash_files(self, ids: list[str]) -> dict:
        """Delete one or more items. pCloud has separate endpoints for
        files vs folders, and we don't know the kind from just the id
        — so we fan out per id and try the file endpoint first,
        falling back to folder. The recursive folder delete is used so
        non-empty folders also disappear in one shot."""
        results: list[dict] = []
        for raw in ids:
            fid = self._file_param(raw)
            try:
                results.append(await self._call("deletefile", {"fileid": fid}))
            except PCloudError as exc:
                # Result 2009 = "File not found"; try as folder.
                if getattr(exc, "result", 0) != 2009:
                    raise
                results.append(
                    await self._call("deletefolderrecursive", {"folderid": fid})
                )
        return {"deleted": len(results)}

    async def move_files(self, ids: list[str], to_parent_id: str) -> dict:
        target = self._folder_param(to_parent_id)
        for raw in ids:
            fid = self._file_param(raw)
            try:
                await self._call("renamefile", {"fileid": fid, "tofolderid": target})
            except PCloudError as exc:
                if getattr(exc, "result", 0) != 2009:
                    raise
                await self._call(
                    "renamefolder", {"folderid": fid, "tofolderid": target}
                )
        return {"moved": len(ids)}

    async def rename_file(self, file_id: str, new_name: str) -> dict:
        name = (new_name or "").strip()
        if not name:
            raise PCloudError("新名稱不可空白")
        fid = self._file_param(file_id)
        try:
            return await self._call(
                "renamefile", {"fileid": fid, "toname": name}
            )
        except PCloudError as exc:
            if getattr(exc, "result", 0) != 2009:
                raise
            return await self._call(
                "renamefolder", {"folderid": fid, "toname": name}
            )

    async def create_folder(self, parent_id: str, name: str) -> PCloudFile:
        n = (name or "").strip()
        if not n:
            raise PCloudError("資料夾名稱不可空白")
        data = await self._call(
            "createfolderifnotexists",
            {"folderid": self._folder_param(parent_id), "name": n},
        )
        meta = data.get("metadata") or {}
        return _to_pcloud_file({**meta, "isfolder": True})

    # ---------- cleanup ----------

    async def cleanup_folder_stream(
        self, folder_id: str, *, dry_run: bool = True
    ) -> AsyncIterator[dict]:
        """Walk every direct child of ``folder_id`` and normalise BT-noise
        filenames to ``<JAV_CODE>.<ext>``.

        Unlike the PikPak counterpart this does NOT try to flatten
        wrapper folders or recurse — pCloud isn't a torrent destination,
        so the only useful pass is renaming pre-existing files. Folders
        whose name carries a recognisable code get renamed to just the
        code; everything else gets skipped.

        Reuses PikPak's multipart-rename helpers so multi-file groups
        sharing a canonical name end up as ``<canon>_N.<ext>``.
        """
        try:
            children = await self.list_files(folder_id)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
            return

        taken: set[str] = {c.name for c in children}
        PART_MIN_BYTES = 500 * 1024 * 1024
        multipart_plan, multipart_members = _build_video_rename_plan(
            children, PART_MIN_BYTES, is_video
        )

        summary = {
            "total": len(children),
            "renamed": 0,
            "flattened": 0,  # always 0 for pCloud — kept for UI compat
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
            await asyncio.sleep(0.02)
            kind = "folder" if child.kind == "folder" else "file"
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
                    if child.name in multipart_plan:
                        target = multipart_plan[child.name]
                    elif child.name in multipart_members:
                        summary["skipped"] += 1
                        yield {
                            **base_event,
                            "action": "skip",
                            "target": child.name,
                            "reason": "already_clean",
                        }
                        continue
                    else:
                        target = f"{code_full}{ext_of(child.name)}"
                else:
                    target = code_full

                if target == child.name:
                    summary["skipped"] += 1
                    yield {
                        **base_event,
                        "action": "skip",
                        "target": target,
                        "reason": "already_clean",
                    }
                    continue

                target = _uniquify_target(target, taken)
                if not dry_run:
                    await self.rename_file(child.id, target)
                taken.discard(child.name)
                taken.add(target)
                summary["renamed"] += 1
                yield {**base_event, "action": "rename", "target": target, "reason": None}
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning("pcloud cleanup failed for %s: %s", child.name, exc)
                yield {**base_event, "action": "error", "target": None, "reason": str(exc)}

        yield {"type": "done", "result": summary}

    # ---------- PikPak → pCloud transfer support ----------
    #
    # These are used by ``services.pcloud_transfer`` to make pCloud pull
    # files directly from PikPak's CDN. ``savefilefromurl`` is async on
    # the server side: it returns immediately with an ``upload_id`` and
    # downloads in the background; we poll ``savefilefromurlstatus`` for
    # progress.

    async def ensure_path(self, path: str) -> int:
        """Walk-create ``/a/b/c`` and return the leaf folder id. Empty
        / ``"/"`` returns the root (0). Used to materialise the user's
        chosen destination before submitting a transfer."""
        path = (path or "").strip()
        if not path or path == "/":
            return 0
        segments = [s for s in path.split("/") if s]
        parent_id = 0
        for seg in segments:
            data = await self._call(
                "createfolderifnotexists",
                {"folderid": parent_id, "name": seg},
            )
            meta = data.get("metadata") or {}
            try:
                parent_id = int(meta.get("folderid", 0))
            except (TypeError, ValueError):
                raise PCloudError(
                    f"pCloud 無法建立或解析資料夾: {seg} (path={path})"
                )
        return parent_id

    async def save_file_from_url(
        self,
        url: str,
        folder_id: int,
        *,
        filename: str = "",
    ) -> dict:
        """Kick off an async fetch of ``url`` into pCloud folder
        ``folder_id``. Returns ``{"upload_id": int, "raw": dict}``. The
        underlying pCloud call returns immediately; pCloud then pulls
        the URL on its own bandwidth in the background. Poll progress
        via :meth:`upload_progress`.
        """
        params: dict[str, Any] = {
            "url": url,
            "folderid": folder_id,
            "nopartial": 1,
        }
        if filename:
            params["target"] = filename
        data = await self._call("savefilefromurl", params)
        upload_id = 0
        # pCloud's response shape has shifted across versions; check the
        # few likely keys before falling back to the "uploadlinks" array.
        for k in ("uploadlinkid", "uploadid"):
            v = data.get(k)
            if v:
                try:
                    upload_id = int(v)
                    break
                except (TypeError, ValueError):
                    pass
        if not upload_id:
            links = data.get("uploadlinks") or data.get("uploads") or []
            if isinstance(links, list) and links:
                first = links[0] or {}
                for k in ("uploadlinkid", "uploadid", "id"):
                    if first.get(k):
                        try:
                            upload_id = int(first[k])
                            break
                        except (TypeError, ValueError):
                            pass
        return {"upload_id": upload_id, "raw": data}

    async def upload_progress(self, upload_id: int) -> dict:
        """Poll a savefilefromurl background job. Normalised return:

        - ``{"status": "downloading", "downloaded": int, "size": int}``
        - ``{"status": "done", "metadata": dict, "file_id": int}``
        - ``{"status": "failed", "error": str}``
        - ``{"status": "unknown"}`` — pCloud no longer remembers this id

        We translate the pCloud shape into this small set so the worker
        doesn't have to chase format variants.
        """
        try:
            data = await self._call(
                "savefilefromurlstatus", {"uploadid": upload_id}
            )
        except PCloudError as exc:
            # 2009 = upload not found / completed and reaped.
            if getattr(exc, "result", 0) in (2009, 2003):
                return {"status": "unknown", "error": str(exc)}
            raise
        files = data.get("files") or []
        if isinstance(files, list) and files:
            f = files[0] or {}
            meta = f.get("metadata") or {}
            file_id = 0
            try:
                file_id = int(meta.get("fileid") or 0)
            except (TypeError, ValueError):
                pass
            if file_id:
                return {
                    "status": "done",
                    "metadata": meta,
                    "file_id": file_id,
                }
        if "downloaded" in data or "size" in data:
            return {
                "status": "downloading",
                "downloaded": int(data.get("downloaded") or 0),
                "size": int(data.get("size") or 0),
            }
        if data.get("error"):
            return {"status": "failed", "error": str(data["error"])}
        return {"status": "unknown"}

    async def cancel_upload(self, upload_id: int) -> None:
        try:
            await self._call(
                "savefilefromurlcancel", {"uploadid": upload_id}
            )
        except PCloudError as exc:
            if getattr(exc, "result", 0) in (2009, 2003):
                return
            raise


pcloud_service = PCloudService()
