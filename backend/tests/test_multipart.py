"""Multi-file 裁決佇列: presence rows bucketed by code, the #145
parts-vs-duplicate-homes axis as the verdict hint, and the operator's
stored review round-tripping through the upsert."""

from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import PresenceEntry
from app.services import multipart

NOW = datetime(2026, 8, 3)


async def _fresh_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(multipart, "SessionLocal", maker)
    return maker


async def test_scan_groups_and_verdicts(tmp_path, monkeypatch):
    maker = await _fresh_db(tmp_path, monkeypatch)
    async with maker() as s:
        s.add_all([
            # Genuine part set: one parent, marked _N siblings.
            PresenceEntry(code="DSVR-1995", path="AVBT/製作商/SOD/未分類/DSVR-1995_1.mp4", updated_at=NOW),
            PresenceEntry(code="DSVR-1995", path="AVBT/製作商/SOD/未分類/DSVR-1995_2.mp4", updated_at=NOW),
            # Duplicate homes: same work, two different folders.
            PresenceEntry(code="ABF-010", path="AVBT/系列/A/ABF-010.mp4", updated_at=NOW),
            PresenceEntry(code="ABF-010", path="AVBT/製作商/Prestige/未分類/ABF-010.mp4", updated_at=NOW),
            # Mixed: a two-file home competing with a loose copy.
            PresenceEntry(code="MIX-001", path="AVBT/系列/M/MIX-001_1.mp4", updated_at=NOW),
            PresenceEntry(code="MIX-001", path="AVBT/系列/M/MIX-001_2.mp4", updated_at=NOW),
            PresenceEntry(code="MIX-001", path="AVBT/製作商/X/未分類/MIX-001.mp4", updated_at=NOW),
            # Single row — not a queue item.
            PresenceEntry(code="SOLO-1", path="AVBT/系列/S/SOLO-1.mp4", updated_at=NOW),
        ])
        await s.commit()

    result = await multipart.scan()
    assert result["total_rows"] == 8
    by_code = {i["code"]: i for i in result["items"]}
    assert set(by_code) == {"DSVR-1995", "ABF-010", "MIX-001"}

    parts = by_code["DSVR-1995"]
    assert parts["verdict_hint"] == "likely_parts"
    assert parts["file_count"] == 2
    assert [f["has_marker"] for f in parts["groups"][0]["files"]] == [True, True]

    homes = by_code["ABF-010"]
    assert homes["verdict_hint"] == "likely_dup_homes"
    assert len(homes["groups"]) == 2

    assert by_code["MIX-001"]["verdict_hint"] == "mixed"
    assert result["needs_listing"] == []


async def test_folder_row_lands_in_needs_listing_not_items(tmp_path, monkeypatch):
    maker = await _fresh_db(tmp_path, monkeypatch)
    async with maker() as s:
        s.add_all([
            # Wild-wrapper folder row: parts hidden behind one row.
            PresenceEntry(code="TRE-76", path="AVBT/製作商/GIGA/ヒロイン陵辱/[吾爱GIGA]TRE-76", updated_at=NOW),
            # Container files still count as files (the .iso swap class).
            PresenceEntry(code="SNIS-494", path="AVBT/系列/S/SNIS-494.iso", updated_at=NOW),
            PresenceEntry(code="SNIS-494", path="AVBT/系列/S/SNIS-494.mp4", updated_at=NOW),
        ])
        await s.commit()

    result = await multipart.scan()
    assert [n["code"] for n in result["needs_listing"]] == ["TRE-76"]
    assert [i["code"] for i in result["items"]] == ["SNIS-494"]


async def test_review_upsert_revert_and_attach(tmp_path, monkeypatch):
    maker = await _fresh_db(tmp_path, monkeypatch)
    async with maker() as s:
        s.add_all([
            PresenceEntry(code="DSVR-1995", path="AVBT/製作商/SOD/未分類/DSVR-1995_1.mp4", updated_at=NOW),
            PresenceEntry(code="DSVR-1995", path="AVBT/製作商/SOD/未分類/DSVR-1995_2.mp4", updated_at=NOW),
        ])
        await s.commit()

    stored = await multipart.set_review("dsvr-1995", "confirmed_parts", "8集無誤")
    assert stored["code"] == "DSVR-1995"
    assert stored["review"]["status"] == "confirmed_parts"

    result = await multipart.scan()
    assert result["items"][0]["review"]["status"] == "confirmed_parts"
    assert result["items"][0]["review"]["note"] == "8集無誤"

    # Second write overwrites, not duplicates.
    stored = await multipart.set_review("DSVR-1995", "resolved_dup")
    assert stored["review"]["status"] == "resolved_dup"

    # pending 撤銷 removes the row entirely.
    cleared = await multipart.set_review("DSVR-1995", "pending")
    assert cleared["review"] is None
    result = await multipart.scan()
    assert result["items"][0]["review"] is None


async def test_review_rejects_junk(tmp_path, monkeypatch):
    await _fresh_db(tmp_path, monkeypatch)
    import pytest
    with pytest.raises(ValueError):
        await multipart.set_review("DSVR-1995", "definitely_not_a_status")
    with pytest.raises(ValueError):
        await multipart.set_review("", "confirmed_parts")


async def test_endpoints_delegate(monkeypatch):
    from app.routers.pikpak import multi_part_review, multi_part_scan

    async def fake_scan():
        return {"total_rows": 0, "items": [], "needs_listing": []}

    async def fake_set(code, status, note=""):
        return {"code": code, "review": {"status": status, "note": note}}

    monkeypatch.setattr(multipart, "scan", fake_scan)
    monkeypatch.setattr(multipart, "set_review", fake_set)
    assert (await multi_part_scan())["items"] == []
    out = await multi_part_review("ABC-123", status="confirmed_parts", note="")
    assert out["review"]["status"] == "confirmed_parts"
