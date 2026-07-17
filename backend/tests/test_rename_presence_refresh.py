"""The rename endpoint self-heals the presence index for the new name.

A manual rename used to leave the code's presence rows stale forever
(``refresh_codes`` only ran when the code landed again through the
pipeline). The endpoint now schedules a delayed per-code refresh after
a successful rename.
"""

import asyncio

from app.routers import pikpak as pikpak_router
from app.services import missing as missing_svc


def _stub_rename(monkeypatch):
    async def fake_rename(file_id, new_name):
        return {"id": file_id, "name": new_name}

    monkeypatch.setattr(
        pikpak_router.pikpak_service, "rename_file", fake_rename
    )


def _capture_refresh(monkeypatch):
    calls: list[list[str]] = []

    async def fake_refresh(codes, **kw):
        calls.append(list(codes))
        return 1

    monkeypatch.setattr(
        pikpak_router.presence_index, "refresh_codes", fake_refresh
    )
    return calls


async def test_rename_schedules_presence_refresh(monkeypatch):
    _stub_rename(monkeypatch)
    calls = _capture_refresh(monkeypatch)
    invalidated: list[bool] = []

    async def fake_invalidate(**kw):
        invalidated.append(True)

    monkeypatch.setattr(
        missing_svc, "invalidate_all_caches_async", fake_invalidate
    )
    monkeypatch.setattr(pikpak_router, "_RENAME_PRESENCE_REFRESH_DELAY", 0)

    result = await pikpak_router.rename_file(
        file_id="f1", new_name="ABC-123.mkv"
    )
    assert result["name"] == "ABC-123.mkv"
    await asyncio.sleep(0.05)

    assert calls == [["ABC-123"]]
    assert invalidated, "a changed refresh must drop the aggregate caches"


async def test_rename_without_code_skips_refresh(monkeypatch):
    _stub_rename(monkeypatch)
    calls = _capture_refresh(monkeypatch)
    monkeypatch.setattr(pikpak_router, "_RENAME_PRESENCE_REFRESH_DELAY", 0)

    await pikpak_router.rename_file(file_id="f1", new_name="整理用資料夾")
    await asyncio.sleep(0.05)

    assert calls == [], "no code in the new name → nothing to refresh"


async def test_failed_rename_schedules_nothing(monkeypatch):
    async def boom(file_id, new_name):
        raise RuntimeError("pikpak down")

    monkeypatch.setattr(pikpak_router.pikpak_service, "rename_file", boom)
    calls = _capture_refresh(monkeypatch)
    monkeypatch.setattr(pikpak_router, "_RENAME_PRESENCE_REFRESH_DELAY", 0)

    try:
        await pikpak_router.rename_file(file_id="f1", new_name="ABC-123.mkv")
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(0.05)

    assert calls == [], "refresh only fires after a successful rename"
