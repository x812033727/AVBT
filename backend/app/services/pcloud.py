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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from ..config import kind_base_path, settings
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
    payload: dict | None = None


TOKEN_FILE = Path("data/pcloud_token.json")

PCLOUD_HOSTS = (
    "https://api.pcloud.com",   # US
    "https://eapi.pcloud.com",  # EU
)

# BT releases bundle tiny ad mp4s alongside the real video. Anything well
# below 300 MB is junk; the real episode is almost always larger. Used
# by the organize-flatten pass when extracting the main video out of a
# wrapper folder. Matches the PikPak reorganize threshold.
_JUNK_BYTES = 300 * 1024 * 1024

# How deep the organize-flatten pass walks into a wrapper subfolder when
# hunting for the main video. Real-world nesting is usually 1-2 levels
# (``<code>/<torrent name>/<file>``); 6 leaves generous headroom for
# double-wrapped releases while still bounding the listfolder fan-out.
_ORGANIZE_MAX_DEPTH = 6

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
        self._auth: str | None = None
        self._host: str = PCLOUD_HOSTS[0]
        self._username: str = ""
        self._userid: int | None = None
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
        auth: str | None = None,
    ) -> dict:
        """One pCloud HTTP call. Returns the parsed JSON body or raises
        :class:`PCloudError` when ``result != 0``.

        Caller is responsible for adding ``auth`` (we do it here when
        ``auth`` is provided) and for picking the right host.

        Switches to **POST + form body** automatically when the
        serialised query would exceed ~4 KB. pCloud accepts either
        GET or POST for every method, but their edge / CDN layer
        rejects very long URLs (e.g. ``savefilefromurl`` with a long
        signed PikPak CDN URL) with a bare HTTP 404 instead of the
        usual ``result != 0`` JSON. POST sidesteps the URL length
        limit by carrying parameters in the request body.
        """
        url = f"{host}/{method}"
        q = dict(params or {})
        if auth:
            q["auth"] = auth
        timeout = float(settings.pcloud_api_timeout_seconds or 0) or None
        # Estimate query string size. httpx will URL-encode each value,
        # so we conservatively count the encoded length; 4 KB leaves
        # comfortable headroom under typical edge limits (8 KB) for the
        # base URL, auth, headers, etc.
        approx_qs_len = sum(len(str(k)) + len(str(v)) + 2 for k, v in q.items())
        use_post = approx_qs_len > 4096
        try:
            async with httpx.AsyncClient(timeout=timeout, **self._client_args()) as client:
                if use_post:
                    # pCloud accepts standard form-encoded POST. We move
                    # everything (including auth) into the body so the
                    # URL itself stays short.
                    resp = await client.post(url, data=q)
                else:
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
        first_error: PCloudError | None = None
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
                    # Match the pCloud docs example **exactly** —
                    # ``logout=1`` is what they show, ``getauth=1`` and
                    # ``device`` as a User-Agent-like identifier. Some
                    # accounts (notably Crypto-enabled premium plans)
                    # silently drop ``auth`` from the response when our
                    # request doesn't match the expected shape closely.
                    digest_creds = {
                        "username": username,
                        "digest": digest,
                        "passworddigest": pw_digest,
                    }
                    attempts.append(
                        (
                            "digest+logout1+device",
                            {
                                **digest_creds,
                                "getauth": 1,
                                "logout": 1,
                                "device": "AVBT/1.0",
                            },
                        )
                    )
                    attempts.append(
                        (
                            "digest+nologout",
                            {**digest_creds, "getauth": 1},
                        )
                    )
            except PCloudError as exc:
                logger.warning(
                    "pCloud getdigest failed host=%s err=%s",
                    host_label,
                    exc,
                )
            # Also queue plain-password attempts in the same two shapes.
            plain_creds = {"username": username, "password": password}
            attempts.append(
                (
                    "plain+logout1+device",
                    {
                        **plain_creds,
                        "getauth": 1,
                        "logout": 1,
                        "device": "AVBT/1.0",
                    },
                )
            )
            attempts.append(
                (
                    "plain+nologout",
                    {**plain_creds, "getauth": 1},
                )
            )

            data: dict | None = None
            last_attempt_error: PCloudError | None = None
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
            # Accept whichever field pCloud actually populated. Their
            # docs say ``auth``, but the live API has been observed using
            # other names on different code paths.
            auth = str(
                data.get("auth")
                or data.get("authtoken")
                or data.get("accesstoken")
                or data.get("apikey")
                or ""
            )
            if not auth:
                # pCloud accepted the credentials (result=0) but didn't
                # return an ``auth`` token. The well-known cases:
                #
                #   - 2FA pending: response carries ``tfatoken`` (snake
                #     case) or rarely ``tfaToken``. The web client then
                #     calls ``/tfa_login`` with the code from email/app
                #     — a flow we don't implement yet.
                #   - Email verification pending after password reset:
                #     response may carry ``verifyrequired`` or similar.
                #   - pCloud rolled out a new field name we don't read
                #     yet — in that case we want the raw payload visible
                #     so we can ship a fix without another guess.
                tfa_token = (
                    data.get("tfatoken")
                    or data.get("tfaToken")
                    or data.get("tfa_required")
                )
                logger.warning(
                    "pCloud login result=0 but no auth user=%s host=%s "
                    "method=%s response_keys=%s",
                    username,
                    host_label,
                    kind,
                    sorted(data.keys()),
                )
                if tfa_token:
                    raise PCloudError(
                        "此 pCloud 帳號需要 2FA 驗證(server 回傳 tfatoken),"
                        "目前不支援密碼+2FA 的兩步驟登入流程。\n"
                        "請改用 Access Token:登入 pcloud.com 後從 DevTools "
                        "Network 抓任一 api 請求 query string 的 auth=xxxxx。"
                    )
                # No auth token despite a successful credentials check.
                # This is a known pCloud behaviour for some account
                # types — most notably **Crypto-enabled premium plans**,
                # which pCloud restricts to OAuth / web sessions for API
                # token issuance. We've already retried with 4 different
                # parameter shapes (digest/plain × with/without logout
                # and device), so further parameter tweaking is unlikely
                # to help. Direct the user to the working path.
                is_crypto_account = bool(
                    data.get("cryptosetup") or data.get("cryptosubscription")
                )
                hint = (
                    "  → 你的帳號有 Crypto Folder 訂閱(cryptosubscription=True)。"
                    "pCloud 對這類付費 Crypto 帳號的「公開 API 密碼登入」會在驗證"
                    "密碼後刻意不發 API token,只允許走 OAuth / web session。"
                    "這是 server 端政策,目前無法用帳密在本工具登入。\n\n"
                    if is_crypto_account
                    else ""
                )
                # Trim the payload so the error stays readable in toasts.
                # We've already logged the full thing above.
                interesting_fields = {
                    k: data.get(k)
                    for k in [
                        "userid",
                        "email",
                        "emailverified",
                        "haspassword",
                        "premium",
                        "cryptosetup",
                        "cryptosubscription",
                        "business",
                    ]
                    if k in data
                }
                raise PCloudError(
                    "pCloud 已驗證密碼(result=0),但拒絕發 API token。\n"
                    + hint
                    + "請改用 Access Token 登入(2 分鐘):\n"
                    "  1. Chrome 開 https://my.pcloud.com 登入\n"
                    "  2. F12 → Network 分頁 → 重新整理首頁\n"
                    "  3. 點任一 api.pcloud.com 或 eapi.pcloud.com 的請求\n"
                    "  4. 在 Request URL 找 auth=XXXXXXXXXX 那串(60 字元)\n"
                    "  5. 複製 = 後面那串貼到本頁「Access Token」分頁\n\n"
                    f"server 回應重點欄位: {interesting_fields}"
                )
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
        downstream code path.

        On success, opportunistically asks pCloud for a fresh token with
        the **maximum TTL** (1 year absolute + 1 year inactivity sliding
        window) and returns the longer-lived token if pCloud issues one.
        This matters for tokens pulled out of a browser session, whose
        original TTL is whatever pcloud.com decided — typically days.
        The exchange is a best-effort: any failure leaves the original
        token intact so the user never ends up worse off.
        """
        # pCloud accepts authexpire up to ~1 year (31536000s). Setting
        # both authexpire and authinactiveexpire to that ceiling gives
        # the longest possible lifetime: 1 year hard cap, with each API
        # call sliding the inactivity window forward.
        MAX_TTL = 31_536_000
        first_error: PCloudError | None = None
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

            # Try to swap for a long-lived token. If pCloud either
            # rejects the extension request or returns no new auth, we
            # silently keep the original token.
            chosen_token = token
            host_label = "US" if "eapi" not in host else "EU"
            try:
                renewed = await self._raw_request(
                    host,
                    "userinfo",
                    {
                        "getauth": 1,
                        "authexpire": MAX_TTL,
                        "authinactiveexpire": MAX_TTL,
                    },
                    auth=token,
                )
                new_auth = str(renewed.get("auth") or "")
                if new_auth and new_auth != token:
                    chosen_token = new_auth
                    logger.info(
                        "pCloud token extended to max TTL host=%s userid=%s",
                        host_label,
                        userid_int,
                    )
                else:
                    logger.info(
                        "pCloud token extension returned no new auth "
                        "(server kept original) host=%s",
                        host_label,
                    )
            except PCloudError as exc:
                logger.info(
                    "pCloud token extension skipped (server refused): %s",
                    exc,
                )
            return chosen_token, host, userid_int
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
        username: str | None = None,
        password: str | None = None,
        access_token: str | None = None,
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
        """Tidy every direct child of ``folder_id`` *in place*:

        - **Files** get their BT-noise name normalised to
          ``<JAV_CODE>.<ext>`` (multi-file groups → ``<canon>_N.<ext>``).
        - **Wrapper folders** are flattened: we walk the folder's subtree
          (recursively, up to :data:`_ORGANIZE_MAX_DEPTH`), pull each
          distinct work's main video OUT into ``folder_id`` renamed
          ``<code>.<ext>``, and trash the now-empty wrapper. This is the
          "我把整個 <番號>/ 資料夾丟進來,影片卻埋在裡面" case — cleanup now
          lifts the video out instead of leaving it nested.

        Unlike the ``organize`` pass this stays within ``folder_id`` (no
        JavBus, no AVBT/<類別>/<名稱>/ categorisation) — it just makes the
        folder's own contents flat and cleanly named. Use ``organize`` to
        additionally sort into category folders.

        For safety the wrapper is only trashed when **every** substantial
        (≥ ``_JUNK_BYTES``) video was successfully extracted; if some
        couldn't be placed or a move failed, the wrapper is left intact.
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

        # Monotonic key — a wrapper can fan out into several extractions,
        # so the per-child index is no longer unique.
        seq = 0
        here = self._folder_param(str(folder_id))

        for _idx, child in enumerate(children, start=1):
            await asyncio.sleep(0.02)
            kind = "folder" if child.kind == "folder" else "file"
            code = extract_jav_code(child.name)
            code_full = extract_jav_code_full(child.name) or code

            try:
                # ---- Folder children: flatten the wrapper in place ----
                if kind == "folder":
                    videos, _ = await self._collect_videos_in_subtree(child.id)
                    if videos:
                        # Group by base code (video's own, falling back to
                        # the wrapper name's). Extract the largest of each
                        # group out to THIS folder, renamed to its full
                        # code; trash the wrapper only if nothing
                        # substantial is left behind.
                        groups: dict[str, list[PCloudFile]] = {}
                        for v in videos:
                            vcode = extract_jav_code(v.name) or code
                            if vcode:
                                groups.setdefault(vcode, []).append(v)
                        substantial_total = sum(
                            1 for v in videos
                            if v.size is not None and v.size >= _JUNK_BYTES
                        )

                        if groups:
                            substantial_done = 0
                            extracted_any = False
                            move_failed = False
                            for gcode, gvids in groups.items():
                                keeper = max(
                                    gvids, key=lambda v: int(v.size or 0)
                                )
                                kcode = (
                                    extract_jav_code_full(keeper.name)
                                    or code_full or gcode
                                )
                                seq += 1
                                ev = {
                                    "type": "progress",
                                    "current": seq,
                                    "kind": "folder",
                                    "source": child.name,
                                }
                                final_name = await self._move_keeper_to_target(
                                    keeper, kcode, here, taken, dry_run=dry_run
                                )
                                if final_name is None:
                                    move_failed = True
                                    summary["errors"] += 1
                                    yield {**ev, "action": "error",
                                           "target": None, "reason": "搬移失敗"}
                                    continue
                                extracted_any = True
                                if keeper.size is not None and keeper.size >= _JUNK_BYTES:
                                    substantial_done += 1
                                summary["flattened"] += 1
                                yield {**ev, "action": "flatten",
                                       "target": final_name, "reason": None}

                            # Trash only when every substantial video was
                            # pulled out — never delete a work we couldn't
                            # place (orphan / dup / multi-part remainder).
                            if (
                                extracted_any
                                and not move_failed
                                and substantial_done == substantial_total
                                and not dry_run
                            ):
                                await self._trash_folder(child)
                            continue
                        # No video carried a resolvable code → fall through
                        # to the plain folder-rename below.

                # ---- Files, and video-less folders: normalise the name ----
                if not code:
                    seq += 1
                    summary["skipped"] += 1
                    yield {"type": "progress", "current": seq, "kind": kind,
                           "source": child.name, "action": "skip",
                           "target": None, "reason": "no_code"}
                    continue

                seq += 1
                base_event = {
                    "type": "progress",
                    "current": seq,
                    "kind": kind,
                    "source": child.name,
                }

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
                seq += 1
                yield {"type": "progress", "current": seq, "kind": kind,
                       "source": child.name, "action": "error",
                       "target": None, "reason": str(exc)}

        yield {"type": "done", "result": summary}

    async def _collect_videos_in_subtree(
        self, folder_id: str, *, max_depth: int = _ORGANIZE_MAX_DEPTH
    ) -> tuple[list[PCloudFile], int]:
        """Walk the subtree rooted at ``folder_id`` (bounded by
        ``max_depth``) and return ``(all_video_files, direct_child_count)``.

        Unlike the old one-level peek, this recurses so a video buried
        under several torrent-name dirs (``MIUM-1104/<torrent>/<rls>/MIUM-1104.mp4``)
        is still found. ``direct_child_count`` counts only the immediate
        children of ``folder_id`` — used for the cosmetic extras tally.
        """
        try:
            items = await self.list_files(folder_id)
        except PCloudError:
            return [], 0
        direct_count = len(items)
        videos: list[PCloudFile] = []
        for it in items:
            if it.kind == "folder":
                if max_depth > 0:
                    sub, _ = await self._collect_videos_in_subtree(
                        it.id, max_depth=max_depth - 1
                    )
                    videos.extend(sub)
            elif is_video(it.name):
                videos.append(it)
        return videos, direct_count

    async def _resolve_target_id(
        self,
        target_path: str,
        target_cache: dict[str, tuple[int | None, set[str]]],
        *,
        dry_run: bool,
    ) -> tuple[int | None, set[str]]:
        """Resolve ``target_path`` to ``(folder_id, sibling_names)``,
        memoised in ``target_cache``. ``dry_run`` uses read-only
        :meth:`lookup_path` (returns ``(None, set())`` when the folder
        doesn't exist yet); live mode :meth:`ensure_path`-creates it."""
        if target_path not in target_cache:
            if dry_run:
                tid = await self.lookup_path(target_path)
            else:
                tid = await self.ensure_path(target_path)
            if tid is None:
                target_cache[target_path] = (None, set())
            else:
                siblings = await self.list_files(str(tid))
                target_cache[target_path] = (tid, {s.name for s in siblings})
        return target_cache[target_path]

    async def _resolve_listing_with_retry(
        self, code: str, timeout: float
    ) -> tuple[str, tuple[str, str] | None]:
        """Two-attempt JavBus lookup wrapping :func:`resolve_listing_loose`.

        Returns ``(status, resolved)``:
          - ``("ok", (kind, name))`` — JavBus categorised the code.
          - ``("none", None)`` — JavBus answered but has no series /
            label / studio for it (no retry; a None answer is stable).
          - ``("timeout", None)`` — both attempts timed out.

        The scraper already does its own 429 backoff; the single retry
        here only rescues an unlucky first attempt that blew our
        per-code wall-clock budget.
        """
        # Lazy import to break the archiver ↔ pcloud import cycle.
        from .archiver import resolve_listing_loose

        for attempt in (1, 2):
            try:
                resolved = await asyncio.wait_for(
                    resolve_listing_loose(code), timeout=timeout
                )
                return ("ok", resolved) if resolved is not None else ("none", None)
            except TimeoutError:
                if attempt == 1:
                    logger.info(
                        "pCloud organize: JavBus timeout for %s, "
                        "retrying after 2s",
                        code,
                    )
                    await asyncio.sleep(2)
        return ("timeout", None)

    async def _move_keeper_to_target(
        self,
        keeper: PCloudFile,
        code: str,
        target_folder_id: int,
        taken: set[str],
        *,
        dry_run: bool,
    ) -> str | None:
        """Move ``keeper`` to ``target_folder_id`` renamed ``<code>.<ext>``.

        Returns the final name on success, or ``None`` if the move call
        failed. Does **not** trash anything — the caller trashes the
        wrapper once, after every keeper has been pulled out, so a
        multi-video wrapper never loses the works we didn't extract yet.
        ``dry_run`` skips the API call but still reserves the name.
        """
        canonical = f"{code}{ext_of(keeper.name)}"
        final_name = _uniquify_target(canonical, taken)

        if not dry_run:
            params: dict[str, Any] = {
                "fileid": self._file_param(keeper.id),
                "tofolderid": target_folder_id,
            }
            if keeper.name != final_name:
                params["toname"] = final_name
            try:
                await self._call("renamefile", params)
            except PCloudError as exc:
                logger.warning(
                    "flatten move keeper %s → %s failed: %s",
                    keeper.name, final_name, exc,
                )
                return None

        taken.add(final_name)
        return final_name

    async def _trash_folder(self, folder: PCloudFile) -> None:
        """Trash ``folder`` recursively. Best-effort: a failure here only
        leaves a now-junk-only wrapper behind, which is cosmetically ugly
        but not fatal — the keepers are already extracted. pCloud trash is
        recoverable for the account's retention window (15-30 days)."""
        try:
            await self._call(
                "deletefolderrecursive",
                {"folderid": self._file_param(folder.id)},
            )
        except PCloudError as exc:
            logger.warning(
                "flatten trash wrapper %s failed: %s", folder.name, exc
            )

    async def organize_folder_stream(
        self, folder_id: str, *, dry_run: bool = True
    ) -> AsyncIterator[dict]:
        """Move each direct child of ``folder_id`` to the canonical
        archive path ``/<kind_base>/<tracked_name>/`` based on the JAV
        code in its name and JavBus metadata.

        Folder children are always opened and walked **recursively** (up
        to :data:`_ORGANIZE_MAX_DEPTH` levels) to find the main video —
        so a wrapper whose own name has no code, or whose video is buried
        several torrent-name dirs deep, still gets flattened. The code is
        taken from the wrapper name, falling back to the extracted
        video's own name. The keeper video is renamed ``<code>.<ext>``
        and the wrapper trashed.

        Where the video ends up depends on JavBus:
          - **Categorised** (``series → label → studio``) → moved to
            ``AVBT/<類別>/<名稱>/``.
          - **Uncategorised** (JavBus has no listing for the code) → the
            video is still pulled out of its wrapper *in place* (into the
            folder being organised) so it's no longer buried. Previously
            these were skipped with ``no_listing`` and the video stayed
            stuck inside the wrapper.

        **Tracked listings are NOT required.** Items with no recognisable
        code anywhere, or that already live at the resolved target, are
        skipped with a structured ``reason``; a JavBus timeout is an
        ``error`` so the user can retry just that code.

        ``dry_run`` mode uses :meth:`lookup_path` (read-only) so a
        preview never materialises empty target folders.
        """
        try:
            children = await self.list_files(folder_id)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
            return

        summary = {
            "total": len(children),
            "moved": 0,
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

        # Per-run cache: target path → (target_id, taken_names). Avoids
        # re-walking ``lookup_path`` and re-listing siblings for every
        # child that maps to the same tracked listing.
        target_cache: dict[str, tuple[int | None, set[str]]] = {}

        # Names already present at ``folder_id`` itself — the collision
        # set for uncategorised in-place flattens (video pulled out of a
        # wrapper but JavBus couldn't categorise it). Seeded from the
        # current listing and grown as we extract, so two uncategorised
        # wrappers don't both land on ``<code>.<ext>``.
        inplace_taken: set[str] = {c.name for c in children}

        # Per-code JavBus timeout. The scraper itself does up to 4
        # attempts with exponential 429 backoff and a 30s per-request
        # timeout — total worst-case ~2 minutes. We can't reasonably
        # wait that long per file, but ``settings.pcloud_organize_javbus_timeout``
        # defaults to 60s so a single transient slow response (first
        # attempt + one retry) doesn't error-skip an otherwise valid
        # code. Bump via env if your JavBus access is unusually slow.
        JAVBUS_TIMEOUT_SECONDS = settings.pcloud_organize_javbus_timeout
        TIMEOUT_REASON = (
            f"JavBus 兩次查詢都逾時（各 {int(JAVBUS_TIMEOUT_SECONDS)}s）。"
            "該番號頁面可能持續慢或被 429 限流 — "
            "點「再來一次」隔一陣子重試;若常常逾時可在 .env 設 "
            "PCLOUD_ORGANIZE_JAVBUS_TIMEOUT=120"
        )

        # Monotonic key for each emitted progress event. A single folder
        # child can fan out into several extractions, so the per-child
        # ``idx`` is no longer unique — ``seq`` is.
        seq = 0

        for idx, child in enumerate(children, start=1):
            await asyncio.sleep(0.02)
            kind = "folder" if child.kind == "folder" else "file"

            # Heartbeat BEFORE any await — so the UI sees activity even
            # when the very first child triggers a slow JavBus lookup.
            # Without this, a 15s timeout on item 1 leaves the modal
            # stuck on "waiting for first event" the whole time.
            yield {
                "type": "processing",
                "current": idx,
                "total": len(children),
                "source": child.name,
                "kind": kind,
            }

            # Defined before the try so the except handler can report it
            # even if the very first lookup raises.
            code = extract_jav_code(child.name)

            try:
                # ---- Folder children: recursively pull out EVERY work ----
                # Walk the subtree, group videos by code (each video's own,
                # falling back to the wrapper name's), and extract the
                # largest of each code group renamed ``<code>.<ext>``. This
                # reaches videos nested several torrent-dirs deep AND lifts
                # a folder that bundles multiple different works — not just
                # one. Same-code extras are resolution dups, trashed with
                # the wrapper at the end.
                if kind == "folder":
                    videos, direct_count = await self._collect_videos_in_subtree(
                        child.id
                    )
                    if videos:
                        groups: dict[str, list[PCloudFile]] = {}
                        # True if we can't safely trash the wrapper after
                        # extraction (a substantial video we couldn't place,
                        # or a keeper whose move/lookup didn't complete).
                        leftover = False
                        for v in videos:
                            vcode = extract_jav_code(v.name) or code
                            if not vcode:
                                if v.size is not None and v.size >= _JUNK_BYTES:
                                    leftover = True
                                continue
                            groups.setdefault(vcode, []).append(v)

                        if groups:
                            extracted_any = False
                            extras_count = max(0, direct_count - 1)
                            for gcode, gvids in groups.items():
                                keeper = max(
                                    gvids, key=lambda v: int(v.size or 0)
                                )
                                status, resolved = (
                                    await self._resolve_listing_with_retry(
                                        gcode, JAVBUS_TIMEOUT_SECONDS
                                    )
                                )
                                seq += 1
                                ev = {
                                    "type": "progress",
                                    "current": seq,
                                    "kind": "folder",
                                    "source": child.name,
                                }
                                if status == "timeout":
                                    leftover = True  # keeper untouched
                                    summary["errors"] += 1
                                    yield {**ev, "action": "error",
                                           "code": gcode, "reason": TIMEOUT_REASON}
                                    continue

                                if status == "ok":
                                    listing_kind, listing_name = resolved  # type: ignore[misc]
                                    target_path = (
                                        f"{kind_base_path(listing_kind)}/{listing_name}"
                                    )
                                    target_id, taken = await self._resolve_target_id(
                                        target_path, target_cache, dry_run=dry_run
                                    )
                                    move_to = (
                                        self._folder_param(str(target_id))
                                        if target_id is not None else 0
                                    )
                                    move_taken = taken
                                else:
                                    # JavBus can't categorise → pull the
                                    # video out of its wrapper *in place*.
                                    listing_kind = listing_name = None
                                    target_path = None
                                    target_id = None
                                    move_to = self._folder_param(str(folder_id))
                                    move_taken = inplace_taken

                                final_name = await self._move_keeper_to_target(
                                    keeper, gcode, move_to, move_taken,
                                    dry_run=dry_run,
                                )
                                if final_name is None:
                                    leftover = True  # keeper still inside
                                    summary["errors"] += 1
                                    yield {**ev, "action": "error",
                                           "code": gcode, "reason": "搬移失敗"}
                                    continue

                                extracted_any = True
                                summary["flattened"] += 1
                                out = {
                                    **ev,
                                    "action": "flatten",
                                    "code": gcode,
                                    "listing_kind": listing_kind,
                                    "listing_name": listing_name,
                                    "target_path": target_path,
                                    "target_name": final_name,
                                    "extras_count": extras_count,
                                }
                                if status == "ok" and target_id is None:
                                    out["would_create"] = True
                                if status != "ok":
                                    out["uncategorized"] = True
                                yield out

                            # Trash the wrapper once, after every keeper is
                            # out — but only when nothing substantial was
                            # left behind (an un-placed video, a failed
                            # move, a timeout). pCloud trash is recoverable
                            # but we still avoid deleting un-extracted works.
                            if extracted_any and not leftover and not dry_run:
                                await self._trash_folder(child)
                            continue
                        # No video had a resolvable code → fall through to
                        # the wrapper-as-is / skip paths using the name code.

                # ---- File children, or video-less / code-less folders ----
                if not code:
                    seq += 1
                    summary["skipped"] += 1
                    yield {"type": "progress", "current": seq, "kind": kind,
                           "source": child.name, "action": "skip",
                           "reason": "no_code"}
                    continue

                status, resolved = await self._resolve_listing_with_retry(
                    code, JAVBUS_TIMEOUT_SECONDS
                )
                seq += 1
                base_event = {
                    "type": "progress",
                    "current": seq,
                    "kind": kind,
                    "source": child.name,
                }
                if status == "timeout":
                    summary["errors"] += 1
                    yield {**base_event, "action": "error",
                           "code": code, "reason": TIMEOUT_REASON}
                    continue
                if status != "ok":
                    # Uncategorised file / video-less wrapper: nowhere
                    # categorised to go, so it stays put.
                    summary["skipped"] += 1
                    yield {**base_event, "action": "skip",
                           "code": code, "reason": "no_listing"}
                    continue

                listing_kind, listing_name = resolved  # type: ignore[misc]
                target_path = f"{kind_base_path(listing_kind)}/{listing_name}"
                target_id, taken = await self._resolve_target_id(
                    target_path, target_cache, dry_run=dry_run
                )

                # Same-folder no-op: child already lives at the resolved
                # target. ``target_id is None`` (dry_run, target doesn't
                # exist) naturally fails this check.
                if target_id is not None and str(target_id) == str(folder_id):
                    summary["skipped"] += 1
                    yield {
                        **base_event,
                        "action": "skip",
                        "code": code,
                        "listing_kind": listing_kind,
                        "listing_name": listing_name,
                        "target_path": target_path,
                        "reason": "already_organized",
                    }
                    continue

                # dry_run + target doesn't exist yet → report as
                # `would_create` so the UI can flag it without an actual id.
                if target_id is None:
                    summary["moved"] += 1
                    yield {
                        **base_event,
                        "action": "move",
                        "code": code,
                        "listing_kind": listing_kind,
                        "listing_name": listing_name,
                        "target_path": target_path,
                        "target_name": child.name,
                        "would_create": True,
                    }
                    continue

                new_name = _uniquify_target(child.name, taken)

                if not dry_run:
                    # pCloud's renamefile/renamefolder accepts tofolderid
                    # + toname in the same call, so we move and (if
                    # needed) rename atomically. This avoids a brief
                    # window where the source folder would hold a
                    # duplicate name.
                    fid_int = self._file_param(child.id)
                    params: dict[str, Any] = {
                        "tofolderid": self._folder_param(str(target_id)),
                    }
                    if new_name != child.name:
                        params["toname"] = new_name
                    if kind == "file":
                        params["fileid"] = fid_int
                        try:
                            await self._call("renamefile", params)
                        except PCloudError as exc:
                            # 2009 = "file does not exist" — fall back
                            # to renamefolder when our heuristic kind
                            # guess was wrong (e.g. listfolder returned
                            # a folder we treated as a file).
                            if getattr(exc, "result", 0) != 2009:
                                raise
                            params.pop("fileid", None)
                            params["folderid"] = fid_int
                            await self._call("renamefolder", params)
                    else:
                        params["folderid"] = fid_int
                        await self._call("renamefolder", params)

                taken.add(new_name)
                summary["moved"] += 1
                yield {
                    **base_event,
                    "action": "move",
                    "code": code,
                    "listing_kind": listing_kind,
                    "listing_name": listing_name,
                    "target_path": target_path,
                    "target_name": new_name,
                }
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning(
                    "pcloud organize failed for %s: %s", child.name, exc
                )
                seq += 1
                yield {
                    "type": "progress",
                    "current": seq,
                    "kind": kind,
                    "source": child.name,
                    "action": "error",
                    "code": code,
                    "reason": str(exc),
                }

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
            except (TypeError, ValueError) as exc:
                raise PCloudError(
                    f"pCloud 無法建立或解析資料夾: {seg} (path={path})"
                ) from exc
        return parent_id

    async def lookup_path(self, path: str) -> int | None:
        """Read-only twin of :meth:`ensure_path`.

        Walks ``/a/b/c`` segment-by-segment and returns the leaf folder
        id, or ``None`` as soon as any segment doesn't exist. Never
        creates anything — that's the whole point. Used by
        :meth:`organize_folder_stream` in ``dry_run`` mode so previewing
        doesn't leave empty target folders littered around the account.
        """
        path = (path or "").strip()
        if not path or path == "/":
            return 0
        segments = [s for s in path.split("/") if s]
        parent_id = 0
        for seg in segments:
            try:
                children = await self.list_files(str(parent_id))
            except PCloudError:
                return None
            match = next(
                (
                    c
                    for c in children
                    if c.kind == "folder" and c.name == seg
                ),
                None,
            )
            if match is None:
                return None
            try:
                parent_id = int(match.id)
            except (TypeError, ValueError):
                return None
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
