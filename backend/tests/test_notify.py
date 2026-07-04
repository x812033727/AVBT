from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as db
from app.models import AppMeta
from app.services.notify import event_enabled, send_notification


async def _bind_tmp_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", maker)
    return engine, maker


async def test_event_enabled_config_defaults(tmp_path, monkeypatch):
    engine, _ = await _bind_tmp_db(tmp_path, monkeypatch)
    assert await event_enabled("tracked_new") is True
    assert await event_enabled("download_failed") is False  # noisy → off by default
    assert await event_enabled("unknown_event") is True  # unknown always delivers
    await engine.dispose()


async def test_event_enabled_appmeta_override(tmp_path, monkeypatch):
    engine, maker = await _bind_tmp_db(tmp_path, monkeypatch)
    async with maker() as session:
        session.add(AppMeta(key="notify:tracked_new", value="0"))
        session.add(AppMeta(key="notify:download_failed", value="1"))
        await session.commit()
    assert await event_enabled("tracked_new") is False
    assert await event_enabled("download_failed") is True
    await engine.dispose()


async def test_send_notification_respects_toggle(tmp_path, monkeypatch):
    engine, maker = await _bind_tmp_db(tmp_path, monkeypatch)
    async with maker() as session:
        session.add(AppMeta(key="notify:archive_done", value="0"))
        await session.commit()

    sent: list[str] = []

    async def fake_webhook(msg):
        sent.append(msg)
        return True

    monkeypatch.setattr("app.services.notify.send_webhook", fake_webhook)
    monkeypatch.setattr("app.services.notify.settings.webhook_url", "http://example/hook")

    assert await send_notification("hi", event="archive_done") == {}
    assert sent == []

    out = await send_notification("hi", event="tracked_new")
    assert out == {"webhook": True}
    assert sent == ["hi"]
    await engine.dispose()
