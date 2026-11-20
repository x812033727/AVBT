"""Drive account bookkeeping: OAuth state, credentials, quota, daily caps."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import jwt
from sqlalchemy import func, select

from ..config import settings
from ..database import SessionLocal
from ..models import GDriveAccount, GDriveLocation
from . import gdrive_config, gdrive_crypto
from .auth import _get_secret
from .gdrive import AccountAuth, GDriveAuthError, gdrive_client
from .gdrive_policy import GB, today

logger = logging.getLogger(__name__)

_STATE_PURPOSE = "gdrive_oauth"
_STATE_TTL = 15 * 60


class AccountUnavailable(RuntimeError):
    pass


# ---------- OAuth state ----------
# The callback is a public endpoint (Google redirects the browser there,
# no Authorization header), so the state parameter carries a short-lived
# JWT signed with the login secret: only someone who started the flow
# from the authenticated settings page can complete it.


def make_state(*, client_pk: int, label: str = "", account_id: int | None = None) -> str:
    payload = {
        "p": _STATE_PURPOSE,
        "label": label[:128],
        "aid": account_id or 0,
        "cid": client_pk,
        "exp": int(time.time()) + _STATE_TTL,
    }
    return jwt.encode(payload, _get_secret(), algorithm="HS256")


def read_state(state: str) -> dict | None:
    try:
        payload = jwt.decode(state, _get_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("p") != _STATE_PURPOSE:
        return None
    return payload


# ---------- serialisation ----------


def remaining_today(acc: GDriveAccount, day: str | None = None) -> int:
    cap = int(max(0.0, settings.gdrive_account_daily_limit_gb) * GB)
    used = acc.daily_uploaded_bytes if acc.daily_day == (day or today()) else 0
    return max(0, cap - used)


def to_public(acc: GDriveAccount, *, location_files: int = 0, location_bytes: int = 0) -> dict:
    day = today()
    return {
        "id": acc.id,
        "label": acc.label,
        "email": acc.email,
        "enabled": acc.enabled,
        "authorized": bool(acc.refresh_token_enc),
        "oauth_client_id": acc.oauth_client_id,
        "quota_total": acc.quota_total,
        "quota_used": acc.quota_used,
        "quota_checked_at": acc.quota_checked_at.isoformat() + "Z" if acc.quota_checked_at else None,
        "uploaded_today": acc.daily_uploaded_bytes if acc.daily_day == day else 0,
        "remaining_today": remaining_today(acc, day),
        "last_error": acc.last_error,
        "stored_files": location_files,
        "stored_bytes": location_bytes,
    }


async def list_public() -> list[dict]:
    async with SessionLocal() as session:
        accounts = (
            await session.execute(select(GDriveAccount).order_by(GDriveAccount.id))
        ).scalars().all()
        stats = dict(
            (aid, (n, b or 0))
            for aid, n, b in (
                await session.execute(
                    select(
                        GDriveLocation.account_id,
                        func.count(),
                        func.sum(GDriveLocation.size),
                    ).group_by(GDriveLocation.account_id)
                )
            ).all()
        )
    out = []
    for a in accounts:
        n, b = stats.get(a.id, (0, 0))
        out.append(to_public(a, location_files=int(n), location_bytes=int(b)))
    return out


# ---------- credentials ----------


async def credentials(account_id: int) -> AccountAuth:
    async with SessionLocal() as session:
        acc = await session.get(GDriveAccount, account_id)
    if acc is None:
        raise AccountUnavailable(f"Drive 帳號 #{account_id} 不存在")
    if not acc.enabled:
        raise AccountUnavailable(f"Drive 帳號「{acc.label or acc.email}」已停用")
    if not acc.refresh_token_enc:
        raise AccountUnavailable(f"Drive 帳號「{acc.label or acc.email}」尚未授權")
    try:
        token = gdrive_crypto.decrypt(acc.refresh_token_enc)
    except gdrive_crypto.TokenKeyError as exc:
        raise AccountUnavailable(str(exc)) from exc
    try:
        creds = await gdrive_config.creds_for(acc.oauth_client_id)
    except gdrive_config.ConfigConflict as exc:
        raise AccountUnavailable(f"Drive 帳號「{acc.label or acc.email}」: {exc}") from exc
    return AccountAuth(account_id=acc.id, refresh_token=token, creds=creds)


async def set_error(account_id: int, message: str) -> None:
    async with SessionLocal() as session:
        acc = await session.get(GDriveAccount, account_id)
        if acc is not None:
            acc.last_error = message[:1000]
            await session.commit()


async def add_uploaded(account_id: int, nbytes: int) -> None:
    if nbytes <= 0:
        return
    day = today()
    async with SessionLocal() as session:
        acc = await session.get(GDriveAccount, account_id)
        if acc is None:
            return
        if acc.daily_day != day:
            acc.daily_day = day
            acc.daily_uploaded_bytes = 0
        acc.daily_uploaded_bytes = (acc.daily_uploaded_bytes or 0) + int(nbytes)
        acc.quota_used = (acc.quota_used or 0) + int(nbytes)
        await session.commit()


async def mark_daily_cap_hit(account_id: int) -> None:
    """Google said "upload limit" before our own counter got there —
    trust Google and park the account until tomorrow."""
    day = today()
    cap = int(max(0.0, settings.gdrive_account_daily_limit_gb) * GB)
    async with SessionLocal() as session:
        acc = await session.get(GDriveAccount, account_id)
        if acc is not None:
            acc.daily_day = day
            acc.daily_uploaded_bytes = max(acc.daily_uploaded_bytes or 0, cap)
            await session.commit()


async def refresh_quota(account_id: int) -> dict:
    auth = await credentials(account_id)
    try:
        info = await gdrive_client.about(auth)
    except GDriveAuthError as exc:
        await set_error(account_id, str(exc))
        raise
    async with SessionLocal() as session:
        row = await session.get(GDriveAccount, account_id)
        if row is not None:
            row.quota_total = info["quota_total"]
            row.quota_used = info["quota_used"]
            row.quota_checked_at = datetime.utcnow()
            if info["email"]:
                row.email = info["email"]
            row.last_error = ""
            await session.commit()
            return to_public(row)
    raise AccountUnavailable(f"Drive 帳號 #{account_id} 不存在")


async def upsert_from_oauth(
    *, code: str, label: str, account_id: int | None, client_pk: int
) -> GDriveAccount:
    """Finish the OAuth flow with OAuth client ``client_pk``: exchange the
    code, look up the email, and either overwrite the token of
    ``account_id`` (re-authorise, possibly switching client) or create /
    update the account with the same email."""
    creds = await gdrive_config.creds_for(client_pk)
    redirect, _src = await gdrive_config.redirect_uri()
    body = await gdrive_client.exchange_code(code, creds, redirect)
    refresh = body.get("refresh_token") or ""
    if not refresh:
        raise RuntimeError("Google 沒有回傳 refresh token(請在 Google 帳號移除此 app 授權後重試)")
    access = body.get("access_token") or ""
    sealed = gdrive_crypto.encrypt(refresh)

    info = await gdrive_client.about_with_access_token(access)

    async with SessionLocal() as session:
        acc = None
        if account_id:
            acc = await session.get(GDriveAccount, account_id)
        if acc is None and info["email"]:
            acc = (
                await session.execute(
                    select(GDriveAccount).where(GDriveAccount.email == info["email"])
                )
            ).scalars().first()
        if acc is None:
            acc = GDriveAccount(label=label or info["email"])
            session.add(acc)
        elif label:
            acc.label = label
        acc.email = info["email"] or acc.email
        acc.refresh_token_enc = sealed
        acc.oauth_client_id = client_pk
        acc.quota_total = info["quota_total"]
        acc.quota_used = info["quota_used"]
        acc.quota_checked_at = datetime.utcnow()
        acc.last_error = ""
        acc.enabled = True
        await session.commit()
        await session.refresh(acc)
    gdrive_client.forget(acc.id)
    return acc
