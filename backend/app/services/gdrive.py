"""Minimal Google Drive v3 REST client (plain httpx, no google SDK).

Covers exactly what the PikPak → Drive move needs:

- OAuth: build the consent URL, exchange the code, refresh access tokens
  (one cached access token per account).
- ``ensure_path``: find-or-create a folder chain, cached per account.
  Scope is ``drive.file``, so the app only ever sees folders/files it
  created itself — which is all the mirror tree consists of.
- Resumable upload sessions: open, query the committed offset, PUT a
  chunk. The transfer queue drives the chunk loop (it owns streaming
  from PikPak and the traffic budget).
- ``about`` for quota/email, ``make_public`` for the embedded player.

Drive has no "fetch this URL yourself" call (unlike pCloud's
``savefilefromurl``), so every byte goes PikPak → backend → Drive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
# Resumable chunks must be a multiple of 256 KiB (except the last one).
CHUNK_ALIGN = 256 * 1024
_FILE_FIELDS = "id,name,size,mimeType,parents"


class GDriveError(RuntimeError):
    """Generic Drive failure; ``status`` is the HTTP status (0 = network)."""

    def __init__(self, message: str, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class GDriveAuthError(GDriveError):
    """Refresh token revoked/expired — the account needs re-authorising."""


class GDriveQuotaError(GDriveError):
    """Daily upload cap / storage full — defer, don't burn retry attempts."""


class SessionExpired(GDriveError):
    """Resumable session URI is gone (404/410) — start a new one from 0."""


_QUOTA_REASONS = {
    "userRateLimitExceeded",
    "rateLimitExceeded",
    "storageQuotaExceeded",
    "quotaExceeded",
    "uploadLimitExceeded",
    "dailyLimitExceeded",
}


def chunk_size() -> int:
    mb = max(1, int(settings.gdrive_chunk_mb or 32))
    raw = mb * 1024 * 1024
    return max(CHUNK_ALIGN, raw - raw % CHUNK_ALIGN)


def oauth_configured() -> bool:
    return bool(settings.gdrive_client_id and settings.gdrive_client_secret)


def build_auth_url(state: str) -> str:
    params = {
        "client_id": settings.gdrive_client_id,
        "redirect_uri": settings.gdrive_redirect_uri,
        "response_type": "code",
        "scope": f"{SCOPES} openid email",
        # offline + consent → Google always returns a refresh_token, also
        # when re-authorising an account that granted access before.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def escape_q(value: str) -> str:
    """Escape a literal for a Drive ``q`` query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def split_path(path: str) -> list[str]:
    return [p for p in (path or "").replace("\\", "/").split("/") if p.strip()]


def drive_path_for(pikpak_path: str) -> str:
    """Mirror a PikPak path under the configured Drive root."""
    root = split_path(settings.gdrive_root_folder)
    return "/".join(root + split_path(pikpak_path))


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""


def file_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""


def file_preview_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/preview" if file_id else ""


def _error_reason(resp: httpx.Response) -> tuple[str, str]:
    try:
        body = resp.json()
    except ValueError:
        return "", resp.text[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        reasons = err.get("errors") or []
        reason = (reasons[0] or {}).get("reason", "") if reasons else ""
        return reason or str(err.get("status") or ""), str(err.get("message") or "")
    if isinstance(err, str):  # OAuth token endpoint shape
        return err, str(body.get("error_description") or err)
    return "", resp.text[:300]


def raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    reason, msg = _error_reason(resp)
    text = f"{what}: HTTP {resp.status_code} {reason} {msg}".strip()
    if resp.status_code in (403, 429) and reason in _QUOTA_REASONS:
        raise GDriveQuotaError(text, resp.status_code, reason)
    if resp.status_code == 401 or reason in ("invalid_grant", "unauthorized_client"):
        raise GDriveAuthError(text, resp.status_code, reason)
    raise GDriveError(text, resp.status_code, reason)


@dataclass
class _Token:
    access: str
    expires_at: float


class GDriveClient:
    """Stateless w.r.t. accounts: callers pass ``(account_id, refresh_token)``.
    Caches access tokens and folder ids keyed by account id."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._tokens: dict[int, _Token] = {}
        self._folders: dict[tuple[int, str], str] = {}
        self._folder_locks: dict[int, asyncio.Lock] = {}

    def http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=20.0),
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def forget(self, account_id: int) -> None:
        self._tokens.pop(account_id, None)
        for key in [k for k in self._folders if k[0] == account_id]:
            self._folders.pop(key, None)

    # ---------- OAuth ----------

    async def exchange_code(self, code: str) -> dict:
        resp = await self.http().post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.gdrive_client_id,
                "client_secret": settings.gdrive_client_secret,
                "redirect_uri": settings.gdrive_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        raise_for(resp, "OAuth 換 token 失敗")
        return resp.json()

    async def access_token(self, account_id: int, refresh_token: str) -> str:
        tok = self._tokens.get(account_id)
        if tok and tok.expires_at - 60 > time.monotonic():
            return tok.access
        resp = await self.http().post(
            TOKEN_URL,
            data={
                "client_id": settings.gdrive_client_id,
                "client_secret": settings.gdrive_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code in (400, 401):
            reason, msg = _error_reason(resp)
            raise GDriveAuthError(
                f"refresh token 失效,請重新授權({reason} {msg})".strip(),
                resp.status_code,
                reason,
            )
        raise_for(resp, "刷新 access token 失敗")
        body = resp.json()
        tok = _Token(body["access_token"], time.monotonic() + float(body.get("expires_in", 3600)))
        self._tokens[account_id] = tok
        return tok.access

    async def _headers(self, account_id: int, refresh_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.access_token(account_id, refresh_token)}"}

    async def _request(
        self, account_id: int, refresh_token: str, method: str, url: str, what: str, **kw
    ) -> httpx.Response:
        headers = {**(kw.pop("headers", None) or {}), **await self._headers(account_id, refresh_token)}
        resp = await self.http().request(method, url, headers=headers, **kw)
        if resp.status_code == 401:
            # Access token revoked early — refresh once and retry.
            self._tokens.pop(account_id, None)
            headers.update(await self._headers(account_id, refresh_token))
            resp = await self.http().request(method, url, headers=headers, **kw)
        raise_for(resp, what)
        return resp

    # ---------- metadata ----------

    @staticmethod
    def _parse_about(body: dict) -> dict:
        quota = body.get("storageQuota") or {}
        user = body.get("user") or {}
        return {
            "email": user.get("emailAddress", ""),
            "name": user.get("displayName", ""),
            "quota_total": int(quota.get("limit") or 0),
            "quota_used": int(quota.get("usage") or 0),
        }

    _ABOUT_FIELDS = "user(emailAddress,displayName),storageQuota"

    async def about(self, account_id: int, refresh_token: str) -> dict:
        resp = await self._request(
            account_id, refresh_token, "GET", f"{API}/about",
            "查詢帳號資訊失敗", params={"fields": self._ABOUT_FIELDS},
        )
        return self._parse_about(resp.json())

    async def about_with_access_token(self, access_token: str) -> dict:
        """Used right after the OAuth exchange, before an account row exists."""
        resp = await self.http().get(
            f"{API}/about",
            params={"fields": self._ABOUT_FIELDS},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        raise_for(resp, "查詢帳號資訊失敗")
        return self._parse_about(resp.json())

    async def get_file(self, account_id: int, refresh_token: str, file_id: str) -> dict:
        resp = await self._request(
            account_id, refresh_token, "GET", f"{API}/files/{file_id}",
            "查詢檔案失敗", params={"fields": _FILE_FIELDS + ",trashed"},
        )
        return resp.json()

    async def make_public(self, account_id: int, refresh_token: str, file_id: str) -> None:
        """Anyone-with-the-link reader, so the /preview iframe plays without
        the viewer being signed into this particular Google account."""
        await self._request(
            account_id, refresh_token, "POST", f"{API}/files/{file_id}/permissions",
            "設定分享權限失敗", json={"type": "anyone", "role": "reader"},
        )

    async def trash(self, account_id: int, refresh_token: str, file_id: str) -> None:
        await self._request(
            account_id, refresh_token, "PATCH", f"{API}/files/{file_id}",
            "刪除檔案失敗", json={"trashed": True},
        )

    # ---------- folders ----------

    async def _find_child_folder(
        self, account_id: int, refresh_token: str, parent: str, name: str
    ) -> str:
        q = (
            f"name = '{escape_q(name)}' and '{parent}' in parents "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        resp = await self._request(
            account_id, refresh_token, "GET", f"{API}/files", "查詢資料夾失敗",
            params={"q": q, "fields": "files(id,name)", "pageSize": 10, "spaces": "drive"},
        )
        files = resp.json().get("files") or []
        return files[0]["id"] if files else ""

    async def _create_folder(
        self, account_id: int, refresh_token: str, parent: str, name: str
    ) -> str:
        resp = await self._request(
            account_id, refresh_token, "POST", f"{API}/files", "建立資料夾失敗",
            params={"fields": "id"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
        )
        return resp.json()["id"]

    async def ensure_path(self, account_id: int, refresh_token: str, path: str) -> str:
        """Folder id for ``path`` (``a/b/c``), creating missing segments.
        Serialised per account so two uploads into the same new folder
        can't both create it (Drive happily allows duplicate names)."""
        segments = split_path(path)
        if not segments:
            return "root"
        lock = self._folder_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            parent = "root"
            walked: list[str] = []
            for seg in segments:
                walked.append(seg)
                key = (account_id, "/".join(walked))
                cached = self._folders.get(key)
                if cached:
                    parent = cached
                    continue
                fid = await self._find_child_folder(account_id, refresh_token, parent, seg)
                if not fid:
                    fid = await self._create_folder(account_id, refresh_token, parent, seg)
                self._folders[key] = fid
                parent = fid
            return parent

    # ---------- resumable upload ----------

    async def start_upload(
        self,
        account_id: int,
        refresh_token: str,
        *,
        name: str,
        folder_id: str,
        size: int,
        mime: str = "application/octet-stream",
    ) -> str:
        resp = await self._request(
            account_id, refresh_token, "POST", UPLOAD_API, "建立上傳工作失敗",
            params={"uploadType": "resumable", "fields": _FILE_FIELDS},
            headers={
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(size),
            },
            json={"name": name, "parents": [folder_id]},
        )
        uri = resp.headers.get("Location", "")
        if not uri:
            raise GDriveError("Drive 沒有回傳上傳 session URI")
        return uri

    @staticmethod
    def _committed(resp: httpx.Response) -> int:
        """Bytes Drive holds, from a 308's ``Range: bytes=0-N`` header."""
        rng = resp.headers.get("Range", "")
        if not rng or "-" not in rng:
            return 0
        try:
            return int(rng.rsplit("-", 1)[1]) + 1
        except ValueError:
            return 0

    async def query_upload(self, session_uri: str, size: int) -> tuple[int, dict | None]:
        """``(committed_bytes, file_or_None)`` for an existing session.
        Session URIs carry their own auth — no bearer token needed."""
        resp = await self.http().put(
            session_uri, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"}
        )
        if resp.status_code in (200, 201):
            return size, resp.json()
        if resp.status_code == 308:
            return self._committed(resp), None
        if resp.status_code in (404, 410):
            raise SessionExpired("上傳 session 已失效", resp.status_code)
        raise_for(resp, "查詢上傳進度失敗")
        raise GDriveError(f"查詢上傳進度: 非預期回應 {resp.status_code}", resp.status_code)

    async def put_chunk(
        self, session_uri: str, data: bytes, offset: int, size: int
    ) -> tuple[int, dict | None]:
        """Upload ``data`` at ``offset``. Returns ``(committed, file_or_None)``;
        ``file`` is set once the last byte lands."""
        end = offset + len(data) - 1
        resp = await self.http().put(
            session_uri,
            content=data,
            headers={
                "Content-Range": f"bytes {offset}-{end}/{size}",
                "Content-Length": str(len(data)),
            },
            timeout=httpx.Timeout(600.0, connect=20.0),
        )
        if resp.status_code in (200, 201):
            return size, resp.json()
        if resp.status_code == 308:
            return self._committed(resp), None
        if resp.status_code in (404, 410):
            raise SessionExpired("上傳 session 已失效", resp.status_code)
        raise_for(resp, "上傳區塊失敗")
        raise GDriveError(f"上傳區塊: 非預期回應 {resp.status_code}", resp.status_code)


gdrive_client = GDriveClient()
