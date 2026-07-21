"""Presence-index hygiene scan: wild wrapper folders + filenames the
sweep will never normalise (2026-07-20 user-report classes)."""

from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import PresenceEntry
from app.services import hygiene
from app.services.hygiene import classify_basename

# --- classify_basename ------------------------------------------------

def test_conforming_shapes_pass():
    for code, base in [
        ("NTK-836", "NTK-836.mp4"),
        ("TRE-76", "TRE-76_1.mkv"),
        ("TRE-76", "TRE-76_04.mkv"),
        ("SKMJ-058", "SKMJ-058B.mp4"),
        ("OFJE-276", "OFJE-276CD1-A.mp4"),          # composite disc marker
        ("NTK-826", "NTK-826-UNCENSORED.mp4"),      # cut tag survives dedupe
        ("SNIS-494", "SNIS-494.iso"),               # container awaiting swap
        ("ntk-836", "ntk-836.MP4"),                 # case-insensitive
    ]:
        assert classify_basename(code, base) is None, base


def test_title_decorated_and_dup_suffix_are_nonconforming():
    for code, base in [
        ("NTK-836", "300NTK-836 【極上P活娘…150字標題…】.mp4"),
        ("NTK-841", "300NTK-841  えろえろオトナGAL.mp4"),
        ("RCT-161", "RCT-161 [日系乱伦综艺]近親相姦.avi"),
        ("REBD-1013", "REBD-1013 (2).mp4"),   # pending-judgment dup copy
        ("HNVR-175", "HNVR00175_2_.mp4"),     # zero-padded old shape
    ]:
        assert classify_basename(code, base) == "nonconforming", base


def test_folder_row_is_wild_wrapper():
    assert classify_basename("TRE-76", "[吾爱GIGA]TRE-76") == "wild_wrapper"
    assert classify_basename("HAWA-110", "51.hawa110") == "wild_wrapper"


# --- scan -------------------------------------------------------------

async def test_scan_counts_and_samples(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(hygiene, "SessionLocal", maker)

    now = datetime(2026, 7, 20)
    async with maker() as s:
        s.add_all([
            PresenceEntry(code="NTK-836", path="AVBT/製作商/Magic/Magicシロウト娘/NTK-836.mp4", updated_at=now),
            PresenceEntry(code="TRE-76", path="AVBT/製作商/GIGA/ヒロイン陵辱/[吾爱GIGA]TRE-76", updated_at=now),
            PresenceEntry(code="RCT-161", path="AVBT/製作商/X/未分類/RCT-161 [日系]近親相姦.avi", updated_at=now),
        ])
        await s.commit()

    result = await hygiene.scan()
    assert result["total_rows"] == 3
    assert result["wild_wrappers"]["count"] == 1
    assert result["wild_wrappers"]["samples"][0]["code"] == "TRE-76"
    assert result["nonconforming"]["count"] == 1
    assert result["nonconforming"]["samples"][0]["code"] == "RCT-161"


async def test_endpoint_delegates(monkeypatch):
    from app.routers.ops import hygiene_scan

    async def fake_scan():
        return {"total_rows": 0}

    monkeypatch.setattr(hygiene, "scan", fake_scan)
    assert await hygiene_scan() == {"total_rows": 0}
