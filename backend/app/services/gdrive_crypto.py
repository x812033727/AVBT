"""Fernet wrapper for the Drive refresh tokens stored in ``gdrive_account``.

The key lives only in env (``GDRIVE_TOKEN_KEY``); the DB holds ciphertext.
A DB leak without the env therefore leaks no usable Google credential.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


class TokenKeyError(RuntimeError):
    """GDRIVE_TOKEN_KEY missing/invalid, or a token was sealed with another key."""


def _fernet() -> Fernet:
    key = (settings.gdrive_token_key or "").strip()
    if not key:
        raise TokenKeyError("未設定 GDRIVE_TOKEN_KEY")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise TokenKeyError(f"GDRIVE_TOKEN_KEY 格式錯誤: {exc}") from exc


def key_configured() -> bool:
    try:
        _fernet()
    except TokenKeyError:
        return False
    return True


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(sealed: str) -> str:
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except InvalidToken as exc:
        raise TokenKeyError("token 無法解密(GDRIVE_TOKEN_KEY 可能已更換,請重新授權)") from exc
