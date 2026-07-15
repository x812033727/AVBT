"""The tracker's runtime toggles must survive a restart.

``TrackerState`` seeds ``enabled`` / ``backfill_enabled`` from the env
defaults (both ``True``), so an in-memory-only toggle meant every
container restart silently reverted the operator's choice. Live
2026-07-15: a deploy re-enabled the auto-send the operator had turned
off, and ``run_loop`` calls ``check_all()`` on its very first iteration
— JavBus went straight into 429 backoff. CLAUDE.md's convention is that
run-time-adjustable switches live in ``app_meta`` (like ``notify:*``).
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
import app.services.tracker as tracker
from app.models import AppMeta


async def _bind_tmp_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    # tracker binds SessionLocal at import time — patch its own reference.
    monkeypatch.setattr(tracker, "SessionLocal", maker)
    return engine, maker


async def test_set_toggle_persists_to_app_meta(tmp_path, monkeypatch):
    engine, maker = await _bind_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker.state, "enabled", True)

    await tracker.set_toggle("enabled", False)

    assert tracker.state.enabled is False
    async with maker() as session:
        row = await session.get(AppMeta, "tracker:enabled")
    assert row is not None and row.value == "0"
    await engine.dispose()


async def test_load_persisted_toggles_overrides_env_default(tmp_path, monkeypatch):
    """The restart case: state comes up ``True`` from the env default and
    must be pulled back down to the operator's stored ``False``."""
    engine, maker = await _bind_tmp_db(tmp_path, monkeypatch)
    async with maker() as session:
        session.add(AppMeta(key="tracker:enabled", value="0"))
        session.add(AppMeta(key="tracker:backfill_enabled", value="0"))
        await session.commit()
    monkeypatch.setattr(tracker.state, "enabled", True)
    monkeypatch.setattr(tracker.state, "backfill_enabled", True)

    await tracker.load_persisted_toggles()

    assert tracker.state.enabled is False
    assert tracker.state.backfill_enabled is False
    await engine.dispose()


async def test_load_persisted_toggles_keeps_env_default_when_unset(
    tmp_path, monkeypatch
):
    """Never-toggled installs must keep the env default, not be forced off."""
    engine, _maker = await _bind_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker.state, "enabled", True)

    await tracker.load_persisted_toggles()

    assert tracker.state.enabled is True
    await engine.dispose()


async def test_toggle_survives_simulated_restart(tmp_path, monkeypatch):
    """End-to-end: turn it off, rebuild state from the env defaults the
    way a fresh process would, and confirm the load pulls it back off."""
    engine, _maker = await _bind_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker.state, "enabled", True)
    await tracker.set_toggle("enabled", False)

    # A restart re-seeds from settings (tracker_enabled defaults True).
    monkeypatch.setattr(tracker.state, "enabled", True)
    await tracker.load_persisted_toggles()

    assert tracker.state.enabled is False
    await engine.dispose()
