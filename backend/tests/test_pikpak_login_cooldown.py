"""Failed PikPak logins must enter a cooldown instead of retrying forever.

Background loops (archiver sweep, tracker) call ``_ensure`` every few
minutes. When the stored token dies and the credential login hits
PikPak's "operation too frequent" throttle, each retry refreshes the
throttle window — so the account stays locked out indefinitely and the
user's manual login can never succeed. The service must back off
exponentially after a too-frequent failure and fail fast (no network
call) while the cooldown is active.
"""

import time

import pytest

import app.services.pikpak as pikpak_mod
from app.services.pikpak import PikPakError, PikPakService

TOO_FREQUENT_MSG = (
    "Aborted - Your operation is too frequent, please try again later."
)


class FakePikPakApi:
    """Stands in for pikpakapi.PikPakApi. ``login_error`` controls what
    ``login()`` does; ``login_calls`` counts real login attempts."""

    login_error: Exception | None = None
    login_calls: int = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.username = kwargs.get("username", "")
        self.user_id = "u-1"
        self.access_token = ""

    async def login(self):
        type(self).login_calls += 1
        if type(self).login_error is not None:
            raise type(self).login_error

    def encode_token(self):
        return "tok-abc"

    async def get_user_info(self):
        return {"name": self.username or "tokuser"}


@pytest.fixture()
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(pikpak_mod, "PikPakApi", FakePikPakApi)
    monkeypatch.setattr(pikpak_mod, "TOKEN_FILE", tmp_path / "token.txt")
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_username", "env-user")
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_password", "env-pass")
    FakePikPakApi.login_error = None
    FakePikPakApi.login_calls = 0
    return PikPakService()


async def test_too_frequent_failure_enters_cooldown(service):
    FakePikPakApi.login_error = Exception(TOO_FREQUENT_MSG)
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    assert FakePikPakApi.login_calls == 1

    # Second call inside the cooldown: fail fast, no network login.
    with pytest.raises(PikPakError, match="冷卻"):
        await service._ensure()
    assert FakePikPakApi.login_calls == 1


async def test_cooldown_message_mentions_remaining_time(service):
    FakePikPakApi.login_error = Exception(TOO_FREQUENT_MSG)
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    with pytest.raises(PikPakError, match=r"\d+ 分"):
        await service._ensure()


async def test_cooldown_expiry_allows_retry(service):
    FakePikPakApi.login_error = Exception(TOO_FREQUENT_MSG)
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    service._login_blocked_until = time.monotonic() - 1
    FakePikPakApi.login_error = None
    client = await service._ensure()
    assert client is not None
    assert FakePikPakApi.login_calls == 2


async def test_too_frequent_cooldown_grows_exponentially(service):
    FakePikPakApi.login_error = Exception(TOO_FREQUENT_MSG)
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    first = service._login_blocked_until - time.monotonic()

    service._login_blocked_until = time.monotonic() - 1
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    second = service._login_blocked_until - time.monotonic()

    assert second > first * 1.5


async def test_explicit_creds_blocked_during_too_frequent_cooldown(service):
    """User-initiated logins must not keep refreshing PikPak's throttle
    window either — surface the remaining wait instead."""
    FakePikPakApi.login_error = Exception(TOO_FREQUENT_MSG)
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    with pytest.raises(PikPakError, match="冷卻"):
        await service._ensure("user", "pw")
    assert FakePikPakApi.login_calls == 1


async def test_explicit_creds_bypass_generic_failure_cooldown(service):
    """A wrong-password failure shouldn't lock the user out of retrying
    with corrected credentials."""
    FakePikPakApi.login_error = Exception("invalid password")
    with pytest.raises(Exception, match="invalid password"):
        await service._ensure()
    FakePikPakApi.login_error = None
    client = await service._ensure("user", "pw")
    assert client is not None
    assert FakePikPakApi.login_calls == 2


async def test_generic_failure_still_cools_background_logins(service):
    FakePikPakApi.login_error = Exception("invalid password")
    with pytest.raises(Exception, match="invalid password"):
        await service._ensure()
    with pytest.raises(PikPakError, match="冷卻"):
        await service._ensure()
    assert FakePikPakApi.login_calls == 1


async def test_success_clears_cooldown_and_streak(service):
    FakePikPakApi.login_error = Exception(TOO_FREQUENT_MSG)
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    service._login_blocked_until = time.monotonic() - 1
    FakePikPakApi.login_error = None
    await service._ensure()
    assert service._login_blocked_until == 0
    assert service._too_frequent_streak == 0


async def test_status_exposes_cooldown(service):
    FakePikPakApi.login_error = Exception(TOO_FREQUENT_MSG)
    with pytest.raises(Exception, match="too frequent"):
        await service._ensure()
    info = service.status()
    assert info["login_cooldown_seconds"] > 0
    assert "too frequent" in info["login_block_reason"]
