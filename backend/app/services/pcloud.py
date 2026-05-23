"""pCloud HTTP API client (async).

pCloud exposes a REST-ish HTTP API at:
  - https://api.pcloud.com  (US region)
  - https://eapi.pcloud.com (EU region)

We only need a thin slice: authentication, folder browsing/creation, the
``savefilefromurl`` async "fetch a URL into pCloud" endpoint, and its
status / cancel companions. That endpoint is exactly what makes a true
zero-bandwidth PikPak → pCloud transfer possible: pCloud pulls the file
itself from PikPak's CDN — we never proxy bytes through our server.

Token persistence works the same as the PikPak service: the user can
either (a) paste an ``access_token`` from pCloud's developer page, or
(b) supply username + password and we exchange them for a token via
``/userinfo?getauth=1``. Either way the resulting auth token is cached
to ``data/pcloud_token.txt`` so the next process start skips re-login.

Region auto-detection: ``pcloud_region=auto`` first tries the US
endpoint; if the server replies with ``result=2321`` (account lives on
EU) we transparently switch to ``eapi.pcloud.com`` and persist the
detected region so subsequent calls go straight there.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class PCloudError(RuntimeError):
    """Raised when pCloud returns ``result != 0`` or the HTTP call fails.

    The user-facing error string is the localized ``error`` field from
    pCloud (e.g. ``"Log in failed."``); the numeric code lives on
    ``self.code`` for callers that want to special-case e.g. ``2321``
    (wrong region)."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


TOKEN_FILE = Path("data/pcloud_token.txt")
REGION_FILE = Path("data/pcloud_region.txt")

_API_HOSTS = {
    "us": "https://api.pcloud.com",
    "eu": "https://eapi.pcloud.com",
}

# pCloud error codes worth special-casing.
_ERR_WRONG_REGION = 2321  # "Please use eapi.pcloud.com"
_ERR_INVALID_TOKEN = (1000, 2000, 2094, 2003)  # login failed / token invalid


class PCloudService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._token: str = ""
        self._region: str = ""  # resolved: "us" or "eu"
        self._username: str = ""
        self._userid: int = 0

    # ---------- token / region persistence ----------

    def _load_token(self) -> str:
        if TOKEN_FILE.exists():
            try:
                return TOKEN_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""

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

    def _load_region(self) -> str:
        if REGION_FILE.exists():
            try:
                v = REGION_FILE.read_text(encoding="utf-8").strip().lower()
                if v in ("us", "eu"):
                    return v
            except OSError:
                pass
        return ""

    def _save_region(self, region: str) -> None:
        if region not in ("us", "eu"):
            return
        REGION_FILE.parent.mkdir(parents=True, exist_ok=True)
        REGION_FILE.write_text(region, encoding="utf-8")

    # ---------- low-level HTTP ----------

    def _proxy(self) -> dict:
        if settings.http_proxy:
            return {"proxy": settings.http_proxy}
        return {}

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        force_region: Optional[str] = None,
    ) -> dict:
        """Hit pCloud ``method`` with ``params``. Returns the parsed JSON.

        On ``result=2321`` (wrong region) and we're in ``auto`` mode, this
        retries on the other host and persists the detected region. On
        any other non-zero ``result`` it raises :class:`PCloudError`.
        """
        region = force_region or self._region or self._resolve_region_hint()
        host = _API_HOSTS[region]
        async with httpx.AsyncClient(timeout=30.0, **self._proxy()) as client:
            try:
                resp = await client.get(f"{host}/{method}", params=params)
            except httpx.HTTPError as exc:
                raise PCloudError(f"pCloud 連線失敗: {exc}") from exc
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise PCloudError(
                f"pCloud 回傳非 JSON ({resp.status_code}): {resp.text[:200]}"
            ) from exc
        result = int(data.get("result", 0))
        if result == 0:
            self._region = region
            return data
        # Wrong region → retry on the other host once.
        if (
            result == _ERR_WRONG_REGION
            and (settings.pcloud_region or "auto").lower() == "auto"
            and not force_region
        ):
            other = "eu" if region == "us" else "us"
            logger.info("pCloud reported wrong region; retrying on %s", other)
            self._save_region(other)
            self._region = other
            return await self._request(method, params, force_region=other)
        msg = data.get("error") or f"pCloud error {result}"
        raise PCloudError(f"{msg} (code={result})", code=result)

    def _resolve_region_hint(self) -> str:
        """Pick the initial region to try when none is cached yet."""
        configured = (settings.pcloud_region or "auto").lower()
        if configured in ("us", "eu"):
            return configured
        cached = self._load_region()
        if cached:
            return cached
        return "us"

    # ---------- auth ----------

    async def _ensure_auth(self) -> str:
        """Return a usable auth token, logging in / loading from disk as
        needed. Concurrent callers converge through ``self._lock``."""
        async with self._lock:
            if self._token:
                return self._token

            # 1. .env access token wins outright.
            env_tok = (settings.pcloud_access_token or "").strip()
            if env_tok:
                await self._verify_token(env_tok)
                self._token = env_tok
                self._save_token(env_tok)
                return self._token

            # 2. Cached token from a previous run.
            disk_tok = self._load_token()
            if disk_tok:
                try:
                    await self._verify_token(disk_tok)
                    self._token = disk_tok
                    return self._token
                except PCloudError as exc:
                    logger.info("Cached pCloud token rejected: %s", exc)
                    self._clear_token()

            # 3. Fall back to env credentials → exchange for token.
            user = (settings.pcloud_username or "").strip()
            pwd = settings.pcloud_password or ""
            if not user or not pwd:
                raise PCloudError(
                    "pCloud 尚未登入。請到 /pcloud 填入帳密 / token,或在 .env "
                    "設定 PCLOUD_USERNAME+PCLOUD_PASSWORD 或 PCLOUD_ACCESS_TOKEN。"
                )
            tok = await self._login_user_pass(user, pwd)
            self._token = tok
            self._save_token(tok)
            return self._token

    async def _verify_token(self, token: str) -> None:
        """Smoke-test ``token`` via ``/userinfo``. Populates username /
        userid as a side-effect."""
        data = await self._request("userinfo", {"auth": token})
        self._username = data.get("email") or ""
        try:
            self._userid = int(data.get("userid") or 0)
        except (TypeError, ValueError):
            self._userid = 0

    async def _login_user_pass(self, username: str, password: str) -> str:
        """Username + password → fresh auth token. Also fills username
        / userid so callers can report who we ended up logged in as."""
        params = {
            "username": username,
            "password": password,
            "getauth": 1,
            # 0 = never expires; keep the session pinned to the device.
            "logout": 0,
        }
        data = await self._request("userinfo", params)
        token = (data.get("auth") or "").strip()
        if not token:
            raise PCloudError("pCloud 未回傳 auth token")
        self._username = data.get("email") or username
        try:
            self._userid = int(data.get("userid") or 0)
        except (TypeError, ValueError):
            self._userid = 0
        return token

    # ---------- public ----------

    def status(self) -> dict:
        return {
            "logged_in": bool(self._token),
            "username": self._username,
            "user_id": self._userid,
            "region": self._region or self._resolve_region_hint(),
            "has_stored_token": TOKEN_FILE.exists(),
            "has_env_credentials": bool(
                settings.pcloud_username and settings.pcloud_password
            ),
            "has_env_token": bool(settings.pcloud_access_token),
            "default_folder": settings.pcloud_default_folder or "/",
        }

    def logout(self) -> None:
        self._token = ""
        self._username = ""
        self._userid = 0
        self._clear_token()

    async def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> dict:
        """Explicit login with overrides. Caller-provided creds win
        over .env / cache and replace whatever token we had."""
        async with self._lock:
            if access_token:
                token = access_token.strip()
                if not token:
                    raise PCloudError("Token 不可空白")
                await self._verify_token(token)
                self._token = token
                self._save_token(token)
            elif username and password:
                token = await self._login_user_pass(username, password)
                self._token = token
                self._save_token(token)
            else:
                raise PCloudError("請提供 username+password 或 access_token")
        return self.status()

    async def call(self, method: str, **params: Any) -> dict:
        """Authenticated round-trip. Adds the ``auth`` param automatically
        and recovers from a token that turns out to be invalid by clearing
        the cache + retrying once via ``_ensure_auth``."""
        token = await self._ensure_auth()
        try:
            return await self._request(method, {**params, "auth": token})
        except PCloudError as exc:
            if exc.code in _ERR_INVALID_TOKEN:
                logger.info("pCloud token invalidated (%s); re-authing", exc)
                async with self._lock:
                    self._token = ""
                    self._clear_token()
                token = await self._ensure_auth()
                return await self._request(method, {**params, "auth": token})
            raise

    # ---------- folder helpers ----------

    async def userinfo(self) -> dict:
        return await self.call("userinfo")

    async def list_folder(self, folder_id: int = 0) -> dict:
        """``folder_id=0`` = root. Returns the raw ``metadata`` dict with
        a ``contents`` array of children."""
        data = await self.call("listfolder", folderid=folder_id, nofiles=0)
        return data.get("metadata") or {}

    async def create_folder_if_not_exists(
        self, parent_id: int, name: str
    ) -> dict:
        """Create — or return existing — folder ``name`` under ``parent_id``.
        Returns the new/existing folder's metadata dict."""
        data = await self.call(
            "createfolderifnotexists", folderid=parent_id, name=name
        )
        return data.get("metadata") or {}

    async def ensure_path(self, path: str) -> int:
        """Walk / create a ``/a/b/c`` path under the root and return the
        leaf folder's id. Empty / ``"/"`` returns the root (0)."""
        path = (path or "").strip()
        if not path or path == "/":
            return 0
        # Normalize: collapse repeated slashes, strip trailing slash.
        segments = [s for s in path.split("/") if s]
        parent_id = 0
        for seg in segments:
            meta = await self.create_folder_if_not_exists(parent_id, seg)
            try:
                parent_id = int(meta.get("folderid", 0))
            except (TypeError, ValueError):
                raise PCloudError(
                    f"pCloud 無法建立或解析資料夾: {seg} (path={path})"
                )
        return parent_id

    async def resolve_path(self, path: str) -> int:
        """Like ``ensure_path`` but does NOT auto-create. Returns 0 if
        the path doesn't fully exist (caller can decide to fail or
        create)."""
        path = (path or "").strip()
        if not path or path == "/":
            return 0
        try:
            data = await self.call("listfolder", path=path, nofiles=1)
            meta = data.get("metadata") or {}
            return int(meta.get("folderid", 0))
        except PCloudError:
            return 0

    # ---------- savefilefromurl ----------

    async def save_file_from_url(
        self,
        url: str,
        folder_id: int,
        *,
        filename: str = "",
    ) -> dict:
        """Kick off an async fetch of ``url`` into pCloud folder
        ``folder_id``. Returns ``{"uploadid": int, "uploadlinkid": ...}``.

        pCloud takes it from here — it pulls the URL on its own bandwidth
        in the background. Poll via :meth:`upload_progress` to watch it.
        """
        params: dict[str, Any] = {
            "url": url,
            "folderid": folder_id,
            "nopartial": 1,
        }
        if filename:
            params["target"] = filename
        data = await self.call("savefilefromurl", **params)
        # Newer responses wrap the id in "uploadlinks": [{"uploadlinkid": ...}],
        # older ones return "uploadid" / "uploadlinks" mixed. Normalize:
        upload_id = 0
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
        """Poll the savefilefromurl background job. Returns one of:

        - ``{"status": "downloading", "downloaded": int, "size": int}``
        - ``{"status": "done", "metadata": {...}, "file_id": int}``
        - ``{"status": "failed", "error": str}``
        - ``{"status": "unknown"}`` — pCloud doesn't remember this id

        pCloud's ``savefilefromurlstatus`` returns a flat dict; we
        translate it into the shape above so the caller doesn't have to
        chase format variants.
        """
        try:
            data = await self.call("savefilefromurlstatus", uploadid=upload_id)
        except PCloudError as exc:
            # 2009 = upload not found → treat as unknown/lost
            if exc.code in (2009, 2003):
                return {"status": "unknown", "error": str(exc)}
            raise
        # pCloud surfaces several shapes; flatten them.
        files = data.get("files") or []
        if files and isinstance(files, list):
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
        # In-progress shape uses "downloaded" / "size" at top level.
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
            await self.call("savefilefromurlcancel", uploadid=upload_id)
        except PCloudError as exc:
            # Already gone / never existed — treat as success.
            if exc.code in (2009, 2003):
                return
            raise


pcloud_service = PCloudService()
