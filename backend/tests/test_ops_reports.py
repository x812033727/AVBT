"""Ops report log parsing."""

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import OfflineTaskLog, PresenceEntry
from app.routers.ops import parse_reports, reconcile_fossils
from app.services import log_reconcile


def test_parse_blocks_newest_last_and_critical_flag():
    text = (
        "已清殼: ['MAS-096']\n"
        "=== 2026-07-15 14:10 (+0800) 輪值報告(第8輪)===\n"
        "健康:OK\n驗證:6 支\n"
        "=== 2026-07-15 15:20 (+0800) 輪值報告(第9輪)===\n"
        "[CRITICAL→已補救] 某事故\n細節\n"
        "计时器输出附在这里\n"
    )
    blocks = parse_reports(text)
    assert len(blocks) == 3
    assert blocks[0].header == "(未分段紀錄)"
    assert "已清殼" in blocks[0].body
    assert blocks[1].header.endswith("(第8輪)")
    assert blocks[1].critical is False
    assert blocks[2].critical is True
    assert "计时器输出" in blocks[2].body


def test_parse_empty():
    assert parse_reports("") == []


async def test_reconcile_fossils_endpoint_delegates_to_service(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(log_reconcile, "SessionLocal", maker)

    async with maker() as s:
        s.add(
            OfflineTaskLog(
                code="EP-001", magnet="m", created_at=datetime(2026, 1, 1),
            )
        )
        s.add(PresenceEntry(code="EP-001", path="AVBT/S/X/EP-001/EP-001.mp4"))
        await s.commit()

    result = await reconcile_fossils(dry_run=True, older_than="2026-07-01", limit=5000)

    assert result == {
        "scanned": 1, "dry_run": True,
        "presence_video": 1, "finalized_sibling": 0, "untouched": 0,
    }
    await engine.dispose()


async def test_reconcile_fossils_endpoint_rejects_relative_older_than(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(log_reconcile, "SessionLocal", maker)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await reconcile_fossils(dry_run=True, older_than="7d", limit=5000)
    assert exc_info.value.status_code == 400
    await engine.dispose()
