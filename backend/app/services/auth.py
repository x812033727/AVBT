"""Single-account login gate.

AVBT is a personal, single-user tool, so this is intentionally NOT a
multi-user system: there is exactly one admin account (``AuthAccount``
row id=1), set up once on first launch via ``/api/auth/setup``. Passwords
are stored as PBKDF2-SHA256 digests (stdlib only — no bcrypt build dep),
and sessions are stateless HS256 JWTs verified by the ``require_auth``
FastAPI dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import AuthAccount

logger = logging.getLogger(__name__)

# Mirror services/pikpak.py's data/ persistence: a generated secret is
# written here when AUTH_SECRET isn't set in the environment.
_SECRET_FILE = Path("data/auth_secret.txt")
# Self-service password reset for a forgotten password: whoever can
# create this file already owns the data dir (same trust boundary as
# editing the DB by hand, just without needing sqlite3). Checked once
# at startup: account row is dropped, the file removed, and /setup
# takes over again.
_RESET_SENTINEL = Path("data/reset_password")
_PBKDF2_ITERATIONS = 240_000
_JWT_ALG = "HS256"

_secret_cache: str | None = None


async def apply_reset_sentinel() -> bool:
    """Startup hook (called from lifespan, after init_db): if the reset
    sentinel file exists, drop the admin account so /setup re-arms.
    Returns True when a reset happened."""
    if not _RESET_SENTINEL.exists():
        return False
    from ..database import SessionLocal  # local: avoid import cycle

    async with SessionLocal() as session:
        account = await get_account(session)
        if account is not None:
            await session.delete(account)
            await session.commit()
    try:
        _RESET_SENTINEL.unlink()
    except OSError:
        # If the file can't be removed we'd reset again on every boot —
        # scream, but don't block startup.
        logger.error("無法刪除 %s,下次啟動會再次重設帳號!", _RESET_SENTINEL)
    _set_pwd_changed_epoch(None)
    logger.warning("偵測到 %s——管理員帳號已重設,請到 /setup 重新建立", _RESET_SENTINEL)
    return True


# ----- signing secret -----

def _get_secret() -> str:
    """Resolve the JWT signing secret.

    Order: env ``AUTH_SECRET`` → cached → ``data/auth_secret.txt`` →
    freshly generated (and persisted). Persisting means tokens survive a
    backend restart without the user having to configure anything.
    """
    global _secret_cache
    if settings.auth_secret:
        return settings.auth_secret
    if _secret_cache:
        return _secret_cache
    try:
        if _SECRET_FILE.exists():
            existing = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if existing:
                _secret_cache = existing
                return existing
    except OSError:
        logger.warning("無法讀取 %s,改用記憶體中的臨時密鑰", _SECRET_FILE)
    generated = secrets.token_urlsafe(32)
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(generated, encoding="utf-8")
    except OSError:
        # Read-only data dir: fall back to a process-lifetime secret.
        # Tokens then invalidate on restart, but login still works.
        logger.warning("無法寫入 %s,token 將在重啟後失效", _SECRET_FILE)
    _secret_cache = generated
    return generated


# ----- password hashing (PBKDF2-SHA256, stdlib only) -----

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


# ----- tokens -----

def create_token(username: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(hours=settings.auth_token_ttl_hours),
    }
    return jwt.encode(payload, _get_secret(), algorithm=_JWT_ALG)


def decode_token_payload(token: str) -> dict | None:
    try:
        return jwt.decode(token, _get_secret(), algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        return None


def decode_token(token: str) -> str | None:
    """Return the username from a valid token, or None if invalid/expired."""
    payload = decode_token_payload(token)
    if payload is None:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


# ----- account (single row) -----

async def get_account(session: AsyncSession) -> AuthAccount | None:
    return (
        await session.execute(select(AuthAccount).limit(1))
    ).scalar_one_or_none()


async def is_configured(session: AsyncSession) -> bool:
    return await get_account(session) is not None


async def create_account(
    session: AsyncSession, username: str, password: str
) -> AuthAccount:
    """Create the one admin account. Raises ValueError if one already
    exists (the router maps that to HTTP 409)."""
    if await is_configured(session):
        raise ValueError("帳號已設定")
    account = AuthAccount(
        id=1, username=username, password_hash=hash_password(password)
    )
    session.add(account)
    await session.commit()
    return account


# ----- login throttle -----
# Single account → a single global counter is enough. In-memory on
# purpose: a restart clearing the lock is acceptable, and there's no
# per-IP tracking to get wrong behind a reverse proxy.

_LOCKOUT_THRESHOLD = 5
_LOCKOUT_SECONDS = 60.0

_failed_logins = 0
_locked_until = 0.0


def login_locked_for() -> float:
    """Seconds until login unlocks (0 = not locked)."""
    return max(0.0, _locked_until - time.monotonic())


def _record_login_result(ok: bool) -> None:
    global _failed_logins, _locked_until
    if ok:
        _failed_logins = 0
        _locked_until = 0.0
        return
    _failed_logins += 1
    if _failed_logins >= _LOCKOUT_THRESHOLD:
        _locked_until = time.monotonic() + _LOCKOUT_SECONDS
        _failed_logins = 0
        logger.warning("連續登入失敗 %d 次,鎖定 %.0f 秒", _LOCKOUT_THRESHOLD, _LOCKOUT_SECONDS)


async def verify_login(
    session: AsyncSession, username: str, password: str
) -> bool:
    account = await get_account(session)
    if account is None or account.username != username:
        _record_login_result(False)
        return False
    ok = verify_password(password, account.password_hash)
    _record_login_result(ok)
    return ok


async def update_password(
    session: AsyncSession, old_password: str, new_password: str
) -> bool:
    account = await get_account(session)
    if account is None or not verify_password(old_password, account.password_hash):
        return False
    account.password_hash = hash_password(new_password)
    account.password_changed_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()
    _set_pwd_changed_epoch(account.password_changed_at)
    return True


# ----- token revocation on password change -----
# require_auth stays off the DB on the hot path: password_changed_at is
# loaded once per process (lazily) and refreshed in-place by
# update_password. Tokens whose iat predates it are rejected.

_pwd_changed_epoch: float | None = None
_pwd_changed_loaded = False
_pwd_changed_lock = asyncio.Lock()

# Grace so the token minted right after a password change (same second)
# isn't killed by iat/changed_at second-level truncation.
_IAT_LEEWAY_S = 2.0


def _set_pwd_changed_epoch(changed: datetime | None) -> None:
    global _pwd_changed_epoch, _pwd_changed_loaded
    # DB stores naive UTC datetimes (project-wide utcnow convention).
    _pwd_changed_epoch = (
        changed.replace(tzinfo=UTC).timestamp() if changed else None
    )
    _pwd_changed_loaded = True


async def _get_pwd_changed_epoch() -> float | None:
    if _pwd_changed_loaded:
        return _pwd_changed_epoch
    async with _pwd_changed_lock:
        if _pwd_changed_loaded:
            return _pwd_changed_epoch
        from ..database import SessionLocal  # local: avoid import cycle

        async with SessionLocal() as session:
            account = await get_account(session)
        _set_pwd_changed_epoch(account.password_changed_at if account else None)
    return _pwd_changed_epoch


# ----- FastAPI dependency -----

_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Guard dependency for protected routers. Verifies the Bearer JWT
    (signature + expiry + not-revoked-by-password-change) and returns
    the username. Raises 401 otherwise. No DB hit on the hot path — the
    revocation cutoff is a process-level cache."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="需要登入")
    payload = decode_token_payload(credentials.credentials)
    username = payload.get("sub") if payload else None
    if not isinstance(username, str) or not username:
        raise HTTPException(status_code=401, detail="登入已失效,請重新登入")
    changed = await _get_pwd_changed_epoch()
    if changed is not None:
        iat = payload.get("iat")
        if not isinstance(iat, (int, float)) or iat + _IAT_LEEWAY_S < changed:
            raise HTTPException(
                status_code=401, detail="密碼已變更,請重新登入"
            )
    return username
