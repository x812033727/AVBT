"""A failed file lookup must not leave its captcha token on the client.

pikpakapi's ``get_download_url`` sets ``client.captcha_token`` (scoped to
that one file), awaits the lookup, then clears it — on success only. A
dangling file id raised straight past the reset, the shared client kept
resending the stale token, and the next offline-download submit was
rejected with "Verification code is invalid". The service answered that
with a password re-login, which inside PikPak's login throttle became a
9.5-hour lockout (2026-11-21).
"""

import pytest

import app.services.pikpak as pikpak_mod
from app.services.pikpak import PikPakService

CAPTCHA_MSG = "Verification code is invalid"


class FakePikPakApi:
    """Mirrors the two library behaviours the bug depends on: the token
    is parked on the instance during a lookup, and ``encode_token``
    returns None (the result lives on ``encoded_token``)."""

    login_calls = 0
    lookup_error: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.username = kwargs.get("username", "")
        self.user_id = "u-1"
        self.access_token = "acc"
        self.refresh_token = "ref"
        self.encoded_token = None
        self.captcha_token = None
        self.submits = 0

    async def login(self):
        type(self).login_calls += 1

    def encode_token(self):
        self.encoded_token = "encoded-acc-ref"

    async def get_download_url(self, file_id):
        self.captcha_token = f"captcha-for-{file_id}"
        if type(self).lookup_error is not None:
            raise type(self).lookup_error
        self.captcha_token = None
        return {"id": file_id, "name": "a.mp4", "kind": "drive#file"}

    async def offline_download(self, url):
        self.submits += 1
        if self.captcha_token:
            raise RuntimeError(CAPTCHA_MSG)
        return {"task": {"id": "t1"}}


@pytest.fixture()
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(pikpak_mod, "PikPakApi", FakePikPakApi)
    monkeypatch.setattr(pikpak_mod, "TOKEN_FILE", tmp_path / "token.txt")
    monkeypatch.setattr(
        pikpak_mod, "LOGIN_COOLDOWN_FILE", tmp_path / "cooldown.json"
    )
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_username", "env-user")
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_password", "env-pass")
    FakePikPakApi.login_calls = 0
    FakePikPakApi.lookup_error = None
    return PikPakService()


async def test_failed_lookup_does_not_leave_captcha_token(service):
    FakePikPakApi.lookup_error = RuntimeError("file not found")
    with pytest.raises(RuntimeError, match="file not found"):
        await service.file_meta("gone-id")
    client = await service._ensure()
    assert client.captcha_token is None
    # …so the next captcha-checked call goes through, with no re-login.
    await service._call(lambda c: c.offline_download("magnet:?xt=x"))
    assert FakePikPakApi.login_calls == 1


async def test_successful_lookup_still_returns_the_file(service):
    meta = await service.file_meta("f1")
    assert meta == {"id": "f1", "name": "a.mp4", "kind": "drive#file"}


async def test_stale_captcha_rejection_retries_without_relogin(service):
    """Even if a stale token does get onto the client (a lookup running
    concurrently with the submit), the first answer is to drop it and
    re-issue the call — not to burn a password login."""
    client = await service._ensure()
    client.captcha_token = "captcha-for-some-other-file"
    resp = await service._call(lambda c: c.offline_download("magnet:?xt=x"))
    assert resp == {"task": {"id": "t1"}}
    assert client.submits == 2
    assert service._client is client
    assert FakePikPakApi.login_calls == 1


async def test_persistent_captcha_rejection_still_relogs_in(service):
    """A rejection that survives clearing the token is the old case (the
    session itself is bad) and keeps the old recovery."""
    first = await service._ensure()
    calls = {"n": 0}

    async def op(c):
        calls["n"] += 1
        if c is first:
            raise RuntimeError(CAPTCHA_MSG)
        return "ok"

    assert await service._call(op) == "ok"
    assert calls["n"] == 3  # original, captcha-cleared retry, post-login
    assert service._client is not first
    assert FakePikPakApi.login_calls == 2


async def test_login_persists_token_the_library_leaves_on_the_client(service):
    """encode_token() returns None in pikpakapi; the token must still
    reach disk so a restart resumes the session instead of logging in."""
    await service._ensure()
    assert pikpak_mod.TOKEN_FILE.read_text(encoding="utf-8") == "encoded-acc-ref"
    FakePikPakApi.login_calls = 0
    restarted = PikPakService()
    await restarted._ensure()
    assert FakePikPakApi.login_calls == 0
