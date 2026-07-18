"""The raw /files/move endpoint must stamp moved ids (settle gate) —
it was the last unstamped mover (#213 adversarial review minor)."""

import app.routers.pikpak as rp


async def test_move_endpoint_stamps_each_id(monkeypatch):
    moved = []
    stamped = []

    async def fake_move(ids, target):
        moved.append((list(ids), target))
        return {"ok": True}

    monkeypatch.setattr(rp.pikpak_service, "move_files", fake_move)
    monkeypatch.setattr(rp.pikpak_service, "record_move_source",
                        lambda fid: stamped.append(fid))
    resp = await rp.move_files(file_ids=["a", "b"], target_folder_id="t")
    assert resp == {"ok": True}
    assert moved == [(["a", "b"], "t")]
    assert stamped == ["a", "b"]


async def test_move_endpoint_failure_stamps_nothing(monkeypatch):
    stamped = []

    async def boom(ids, target):
        raise RuntimeError("nope")

    monkeypatch.setattr(rp.pikpak_service, "move_files", boom)
    monkeypatch.setattr(rp.pikpak_service, "record_move_source",
                        lambda fid: stamped.append(fid))
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await rp.move_files(file_ids=["a"], target_folder_id="t")
    assert stamped == []       # failed move must not open a settle stamp
