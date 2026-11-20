"""Fernet key for the Drive secrets stored in the DB (refresh tokens and
OAuth client secrets).

The key never goes into the DB. Resolution order:

1. env ``GDRIVE_TOKEN_KEY`` (``.env``) — wins, read-only from the UI;
2. ``data/gdrive_token_key.txt`` — written by the settings page (0600),
   same pattern as ``data/auth_secret.txt``. ``data/`` is the persistent
   volume; the host's ``.env`` isn't visible inside the container.

A DB leak alone therefore leaks no usable Google credential.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

logger = logging.getLogger(__name__)

KEY_FILE = Path("data/gdrive_token_key.txt")

_cache: str | None = None


class TokenKeyError(RuntimeError):
    """Key missing/invalid, or a value was sealed with another key."""


def validate_key(key: str) -> str:
    key = (key or "").strip()
    try:
        Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise TokenKeyError("金鑰格式錯誤(需為 44 字元的 Fernet 金鑰)") from exc
    return key


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _env_key() -> str:
    return (settings.gdrive_token_key or "").strip()


def _file_key() -> str:
    global _cache
    if _cache is not None:
        return _cache
    try:
        if KEY_FILE.is_file():
            _cache = KEY_FILE.read_text(encoding="utf-8").strip()
            return _cache
    except OSError as exc:
        logger.warning("無法讀取 %s: %s", KEY_FILE, exc)
    return ""


def key_source() -> str:
    """``env`` | ``file`` | ``none``."""
    if _env_key():
        return "env"
    if _file_key():
        return "file"
    return "none"


def current_key() -> str:
    return _env_key() or _file_key()


def _fernet(key: str | None = None) -> Fernet:
    k = key if key is not None else current_key()
    if not k:
        raise TokenKeyError("尚未設定加密金鑰(設定頁 → Google Drive → 加密金鑰)")
    try:
        return Fernet(k.encode())
    except (ValueError, TypeError) as exc:
        raise TokenKeyError(f"加密金鑰格式錯誤: {exc}") from exc


def key_configured() -> bool:
    try:
        _fernet()
    except TokenKeyError:
        return False
    return True


def write_key_file(key: str) -> None:
    """Persist a (validated) key to data/ with 0600 permissions."""
    global _cache
    key = validate_key(key)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEY_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    os.replace(tmp, KEY_FILE)
    _cache = key


def reset_cache() -> None:
    global _cache
    _cache = None


def encrypt(plain: str, key: str | None = None) -> str:
    return _fernet(key).encrypt(plain.encode()).decode()


def decrypt(sealed: str, key: str | None = None) -> str:
    try:
        return _fernet(key).decrypt(sealed.encode()).decode()
    except InvalidToken as exc:
        raise TokenKeyError("無法解密(加密金鑰可能已更換,請重新授權)") from exc
