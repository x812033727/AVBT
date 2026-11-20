"""Settings-page configuration for Google Drive OAuth.

- OAuth clients (several Google Cloud projects) live in
  ``gdrive_oauth_client``; their secrets are Fernet-encrypted with the
  key from ``gdrive_crypto`` (env or ``data/`` file, never the DB).
- The redirect URI is one per site (``app_meta`` ``gdrive:redirect_uri``,
  env override) — every client registers the same callback.
- ``rotate_key`` re-encrypts every stored secret under a new key.
- ``import_env_client`` turns a legacy ``GDRIVE_CLIENT_ID/SECRET`` pair
  from ``.env`` into a client row once (``migrated:gdrive_env_client``).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select, update

from ..config import settings
from ..database import SessionLocal
from ..models import AppMeta, GDriveAccount, GDriveOAuthClient
from . import gdrive_crypto
from .gdrive import OAuthCreds

logger = logging.getLogger(__name__)

KEY_REDIRECT = "gdrive:redirect_uri"
CALLBACK_PATH = "/api/gdrive/oauth/callback"
DEFAULT_REDIRECT = f"http://localhost:8000{CALLBACK_PATH}"


class ConfigError(ValueError):
    """Invalid input from the settings page (→ HTTP 400)."""


class ConfigConflict(RuntimeError):
    """Operation not allowed in the current state (→ HTTP 409)."""


# ---------- redirect URI ----------


def validate_redirect(uri: str) -> str:
    uri = (uri or "").strip()
    if not (uri.startswith("https://") or uri.startswith("http://")):
        raise ConfigError("重新導向網址需以 http:// 或 https:// 開頭")
    if not uri.rstrip("/").endswith(CALLBACK_PATH):
        raise ConfigError(f"重新導向網址需以 {CALLBACK_PATH} 結尾")
    return uri.rstrip("/")


async def redirect_uri() -> tuple[str, str]:
    """``(uri, source)`` with source ``env`` | ``settings`` | ``default``.
    The env var only counts when explicitly set to a non-default value,
    since config.py ships the localhost default."""
    env = (settings.gdrive_redirect_uri or "").strip()
    if env and env != DEFAULT_REDIRECT:
        return env, "env"
    async with SessionLocal() as session:
        row = await session.get(AppMeta, KEY_REDIRECT)
    if row is not None and row.value:
        return row.value, "settings"
    return DEFAULT_REDIRECT, "default"


async def set_redirect_uri(uri: str) -> str:
    uri = validate_redirect(uri)
    async with SessionLocal() as session:
        row = await session.get(AppMeta, KEY_REDIRECT)
        if row is None:
            session.add(AppMeta(key=KEY_REDIRECT, value=uri))
        else:
            row.value = uri
        await session.commit()
    return uri


# ---------- token key ----------


async def set_or_rotate_key(new_key: str | None) -> str:
    """No key yet → write it. Key already in data/ → re-encrypt every
    stored secret under the new key (one transaction), then swap the
    file. Returns the resulting source. ``new_key=None`` = generate."""
    if gdrive_crypto.key_source() == "env":
        raise ConfigConflict("金鑰目前來自 .env(GDRIVE_TOKEN_KEY),請改 .env 後重啟")
    try:
        key = gdrive_crypto.validate_key(new_key) if new_key else gdrive_crypto.generate_key()
    except gdrive_crypto.TokenKeyError as exc:
        raise ConfigError(str(exc)) from exc
    old = gdrive_crypto.current_key()
    if not old:
        gdrive_crypto.write_key_file(key)
        return "file"
    if old == key:
        return "file"
    await _reencrypt(old, key)
    gdrive_crypto.write_key_file(key)
    return "file"


async def _reencrypt(old: str, new: str) -> None:
    """Decrypt every secret with ``old`` and seal with ``new``. Any value
    that won't decrypt aborts the whole rotation (nothing is written)."""
    async with SessionLocal() as session:
        accounts = (await session.execute(select(GDriveAccount))).scalars().all()
        clients = (await session.execute(select(GDriveOAuthClient))).scalars().all()
        try:
            for a in accounts:
                if a.refresh_token_enc:
                    a.refresh_token_enc = gdrive_crypto.encrypt(
                        gdrive_crypto.decrypt(a.refresh_token_enc, old), new
                    )
            for c in clients:
                if c.client_secret_enc:
                    c.client_secret_enc = gdrive_crypto.encrypt(
                        gdrive_crypto.decrypt(c.client_secret_enc, old), new
                    )
        except gdrive_crypto.TokenKeyError as exc:
            await session.rollback()
            raise ConfigConflict(f"更換金鑰中止:有資料無法用目前金鑰解密({exc})") from exc
        await session.commit()


# ---------- OAuth clients ----------


def _mask(secret: str) -> str:
    return f"••••{secret[-4:]}" if len(secret) >= 4 else "••••"


def client_public(c: GDriveOAuthClient, *, account_count: int = 0) -> dict:
    try:
        masked = _mask(gdrive_crypto.decrypt(c.client_secret_enc)) if c.client_secret_enc else ""
        secret_ok = bool(c.client_secret_enc)
    except gdrive_crypto.TokenKeyError:
        masked, secret_ok = "(無法解密)", False
    return {
        "id": c.id,
        "label": c.label,
        "client_id": c.client_id,
        "client_secret_masked": masked,
        "secret_ok": secret_ok,
        "enabled": c.enabled,
        "account_count": account_count,
    }


async def list_clients() -> list[dict]:
    async with SessionLocal() as session:
        clients = (
            await session.execute(select(GDriveOAuthClient).order_by(GDriveOAuthClient.id))
        ).scalars().all()
        counts = dict(
            (
                await session.execute(
                    select(GDriveAccount.oauth_client_id, func.count()).group_by(
                        GDriveAccount.oauth_client_id
                    )
                )
            ).all()
        )
    return [client_public(c, account_count=int(counts.get(c.id, 0))) for c in clients]


def _clean_client_id(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ConfigError("請填用戶端 ID")
    if any(ch.isspace() for ch in v):
        raise ConfigError("用戶端 ID 不可含空白")
    return v


async def create_client(*, label: str, client_id: str, client_secret: str) -> dict:
    cid = _clean_client_id(client_id)
    secret = (client_secret or "").strip()
    if not secret:
        raise ConfigError("請填用戶端密鑰")
    try:
        sealed = gdrive_crypto.encrypt(secret)
    except gdrive_crypto.TokenKeyError as exc:
        raise ConfigConflict(str(exc)) from exc
    async with SessionLocal() as session:
        row = GDriveOAuthClient(
            label=(label or "").strip()[:128] or cid.split("-")[0],
            client_id=cid,
            client_secret_enc=sealed,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return client_public(row)


async def update_client(
    client_pk: int,
    *,
    label: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    enabled: bool | None = None,
) -> dict:
    async with SessionLocal() as session:
        row = await session.get(GDriveOAuthClient, client_pk)
        if row is None:
            raise ConfigError("用戶端不存在")
        if label is not None:
            row.label = label.strip()[:128]
        if client_id is not None and client_id.strip():
            row.client_id = _clean_client_id(client_id)
        if client_secret is not None and client_secret.strip():
            try:
                row.client_secret_enc = gdrive_crypto.encrypt(client_secret.strip())
            except gdrive_crypto.TokenKeyError as exc:
                raise ConfigConflict(str(exc)) from exc
        if enabled is not None:
            row.enabled = enabled
        row.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(row)
        n = (
            await session.execute(
                select(func.count()).select_from(GDriveAccount).where(
                    GDriveAccount.oauth_client_id == client_pk
                )
            )
        ).scalar_one()
    return client_public(row, account_count=int(n))


async def delete_client(client_pk: int) -> None:
    async with SessionLocal() as session:
        row = await session.get(GDriveOAuthClient, client_pk)
        if row is None:
            raise ConfigError("用戶端不存在")
        n = (
            await session.execute(
                select(func.count()).select_from(GDriveAccount).where(
                    GDriveAccount.oauth_client_id == client_pk
                )
            )
        ).scalar_one()
        if n:
            raise ConfigConflict(f"還有 {n} 個 Drive 帳號使用這組用戶端,請先刪除或改用其他用戶端重新授權")
        await session.delete(row)
        await session.commit()


async def creds_for(client_pk: int) -> OAuthCreds:
    """Decrypted creds of an enabled client. Raises ConfigConflict."""
    async with SessionLocal() as session:
        row = await session.get(GDriveOAuthClient, client_pk) if client_pk else None
    if row is None:
        raise ConfigConflict("找不到對應的 OAuth 用戶端(可能已刪除),請改用其他用戶端重新授權")
    if not row.enabled:
        raise ConfigConflict(f"OAuth 用戶端「{row.label}」已停用")
    try:
        secret = gdrive_crypto.decrypt(row.client_secret_enc)
    except gdrive_crypto.TokenKeyError as exc:
        raise ConfigConflict(str(exc)) from exc
    return OAuthCreds(client_id=row.client_id, client_secret=secret)


async def any_client_enabled() -> bool:
    async with SessionLocal() as session:
        n = (
            await session.execute(
                select(func.count()).select_from(GDriveOAuthClient).where(GDriveOAuthClient.enabled)
            )
        ).scalar_one()
    return bool(n)


# ---------- legacy .env import ----------


async def import_env_client() -> None:
    """One-time: ``GDRIVE_CLIENT_ID/SECRET`` from .env → a client row, and
    bind accounts created before clients existed (oauth_client_id=0).
    Raises when it can't run yet (no key) so ``_run_once`` retries."""
    cid = (settings.gdrive_client_id or "").strip()
    secret = (settings.gdrive_client_secret or "").strip()
    if not cid or not secret:
        return  # nothing to import; flag gets set
    if not gdrive_crypto.key_configured():
        raise RuntimeError("加密金鑰尚未設定,稍後再匯入 .env 的 OAuth 用戶端")
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(GDriveOAuthClient).where(GDriveOAuthClient.client_id == cid)
            )
        ).scalars().first()
        if row is None:
            row = GDriveOAuthClient(
                label=".env 匯入",
                client_id=cid,
                client_secret_enc=gdrive_crypto.encrypt(secret),
            )
            session.add(row)
            await session.flush()
        await session.execute(
            update(GDriveAccount)
            .where(GDriveAccount.oauth_client_id == 0)
            .values(oauth_client_id=row.id)
        )
        await session.commit()
