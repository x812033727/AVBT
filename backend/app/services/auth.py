"""Single-account login gate.

AVBT is a personal, single-user tool, so this is intentionally NOT a
multi-user system: there is exactly one admin account (``AuthAccount``
row id=1), set up once on first launch via ``/api/auth/setup``. Passwords
are stored as PBKDF2-SHA256 digests (stdlib only — no bcrypt build dep),
and sessions are stateless HS256 JWTs verified by the ``require_auth``
FastAPI dependency.
"""

from __future__ import annotations

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
_PBKDF2_ITERATIONS = 240_000
_JWT_ALG = "HS256"

_secret_cache: str | None = None


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


def decode_token(token: str) -> str | None:
    """Return the username from a valid token, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
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
    await session.commit()
    return True


# ----- FastAPI dependency -----

_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Guard dependency for protected routers. Verifies the Bearer JWT
    (signature + expiry) and returns the username. Raises 401 otherwise.

    Stateless on purpose: it does not hit the DB, so a password change
    leaves previously-issued tokens valid until they expire."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="需要登入")
    username = decode_token(credentials.credentials)
    if username is None:
        raise HTTPException(status_code=401, detail="登入已失效,請重新登入")
    return username
