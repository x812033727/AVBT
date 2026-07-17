"""PikPak operation-level throttle backoff.

`_call` must retry an operation that hits "operation is too frequent"
with exponential backoff (a pre-execution rejection, so retrying writes
is side-effect-safe), give up after a bounded number of retries, and
leave the invalid-token relogin path and all other errors unchanged.
"""

import pytest

import app.services.pikpak as pikpak_mod
from app.services.pikpak import PikPakError, PikPakService

TOO_FREQUENT = "Aborted - Your operation is too frequent, please try again later."
INVALID_TOKEN = "invalid_grant"


@pytest.fixture()
def service(monkeypatch):
    svc = PikPakService()

    async def fake_ensure(*a, **k):
        return object()

    monkeypatch.setattr(svc, "_ensure", fake_ensure)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_throttle_max_retries", 3)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_throttle_base_seconds", 1.0)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_throttle_max_seconds", 10.0)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_api_timeout_seconds", 0)
    return svc


@pytest.fixture()
def no_sleep(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(pikpak_mod.asyncio, "sleep", fake_sleep)
    return sleeps


async def test_retries_then_succeeds(service, no_sleep):
    calls = {"n": 0}

    async def op(client):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PikPakError(TOO_FREQUENT)
        return "ok"

    assert await service._call(op) == "ok"
    assert calls["n"] == 3          # 2 throttled + 1 success
    assert len(no_sleep) == 2       # backed off twice


async def test_gives_up_after_max(service, no_sleep):
    calls = {"n": 0}

    async def op(client):
        calls["n"] += 1
        raise PikPakError(TOO_FREQUENT)

    with pytest.raises(PikPakError):
        await service._call(op)
    assert calls["n"] == 4          # initial + 3 retries
    assert len(no_sleep) == 3


async def test_non_throttle_raises_immediately(service, no_sleep):
    async def op(client):
        raise PikPakError("some other error")

    with pytest.raises(PikPakError):
        await service._call(op)
    assert no_sleep == []           # never backed off


async def test_invalid_token_relogins_once(service, no_sleep, monkeypatch):
    async def noop_drop(c):
        return None

    monkeypatch.setattr(service, "_drop_for_relogin", noop_drop)
    calls = {"n": 0}

    async def op(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PikPakError(INVALID_TOKEN)
        return "ok"

    assert await service._call(op) == "ok"
    assert calls["n"] == 2          # one relogin retry
    assert no_sleep == []           # relogin path does not throttle-backoff
