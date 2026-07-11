"""Token-restored PikPak clients must recover user_id from the JWT.

pikpakapi's decode_token() leaves user_id=None, which makes captcha_init
send "user_id": null — PikPak rejects that with a proto error and every
playback/download link 502s until the next full login."""

import base64
import json
from types import SimpleNamespace

from app.services.pikpak import _backfill_user_id


def _jwt_with(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def test_backfills_sub_from_access_token():
    client = SimpleNamespace(user_id=None, access_token=_jwt_with({"sub": "u-123"}))
    _backfill_user_id(client)
    assert client.user_id == "u-123"


def test_keeps_existing_user_id():
    client = SimpleNamespace(user_id="keep", access_token=_jwt_with({"sub": "other"}))
    _backfill_user_id(client)
    assert client.user_id == "keep"


def test_garbage_token_leaves_user_id_unset():
    for tok in ("", "not-a-jwt", "a.%%%.c", None):
        client = SimpleNamespace(user_id=None, access_token=tok)
        _backfill_user_id(client)
        assert client.user_id is None


def test_missing_sub_leaves_user_id_unset():
    client = SimpleNamespace(user_id=None, access_token=_jwt_with({"exp": 1}))
    _backfill_user_id(client)
    assert client.user_id is None
