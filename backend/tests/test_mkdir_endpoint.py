"""POST /files/mkdir — resolve-or-create a nested path via folder_id
(the twin-aware, create-locked resolver), for misfiled 番號 whose
correct 製作商 folder doesn't exist yet (KK-078 live case)."""

import pytest
from fastapi import HTTPException

import app.routers.pikpak as rp


async def test_mkdir_resolves_and_strips_slashes(monkeypatch):
    calls = []

    async def fake_folder_id(path):
        calls.append(path)
        return "fid-123"

    monkeypatch.setattr(rp.pikpak_service, "folder_id", fake_folder_id)
    resp = await rp.make_folder_path(path="/AVBT/製作商/グローリークエスト/")
    assert resp == {"id": "fid-123", "path": "AVBT/製作商/グローリークエスト"}
    assert calls == ["AVBT/製作商/グローリークエスト"]


async def test_mkdir_empty_path_rejected():
    with pytest.raises(HTTPException) as exc:
        await rp.make_folder_path(path="  / ")
    assert exc.value.status_code == 400


async def test_mkdir_blank_result_is_502(monkeypatch):
    async def fake_folder_id(path):
        return ""

    monkeypatch.setattr(rp.pikpak_service, "folder_id", fake_folder_id)
    with pytest.raises(HTTPException) as exc:
        await rp.make_folder_path(path="AVBT/x")
    assert exc.value.status_code == 502
