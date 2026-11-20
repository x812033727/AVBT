"""Tracked-page "存到 Google Drive": listing membership, auto reconcile,
scan de-duplication and the per-listing settings endpoint."""

import json
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as db
import app.routers.tracked as tracked_router
import app.services.gdrive_accounts as ga
import app.services.gdrive_auto as gauto
import app.services.gdrive_config as gc
import app.services.gdrive_transfer as gt
import app.services.listing_codes as lc
from app.config import settings
from app.models import (
    GDriveAccount,
    GDriveJob,
    GDriveLocation,
    GDriveOAuthClient,
    GDriveTransfer,
    MovieDetailCache,
    OfflineTaskLog,
    PresenceEntry,
    TrackedListing,
)
from app.schemas import MovieDetail
from app.services import gdrive_crypto

OLD = datetime.utcnow() - timedelta(days=3)


def _detail(code, studio="s1", series=None, stars=(), label=None, director=None):
    d = {"code": code, "title": code, "studio": {"id": studio, "name": studio.upper()}}
    if series:
        d["series"] = {"id": series, "name": series}
    if label:
        d["label"] = {"id": label, "name": label}
    if director:
        d["director"] = {"id": director, "name": director}
    d["actresses"] = [{"id": s, "name": s} for s in stars]
    return json.dumps(d)


def test_keys_for_detail_covers_every_kind():
    d = MovieDetail.model_validate_json(
        _detail("ABC-1", studio="st", series="se", stars=("a1", "a2"), label="lb", director="dr")
    )
    assert set(lc.keys_for_detail(d)) == {
        ("studio", "st"), ("series", "se"), ("label", "lb"), ("director", "dr"),
        ("star", "a1"), ("star", "a2"),
    }


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(gdrive_crypto, "KEY_FILE", tmp_path / "k.txt")
    gdrive_crypto.reset_cache()
    monkeypatch.setattr(settings, "gdrive_token_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "auth_secret", "test-secret")
    monkeypatch.setattr(settings, "gdrive_auto_settle_minutes", 60)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    for mod in (lc, gauto, gt, ga, gc):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    lc.invalidate()

    present = {"OLD-001", "OLD-002", "NEW-003", "BOTH-004", "RAW-005"}
    monkeypatch.setattr(lc.presence_index, "peek", lambda: set(present))

    # Scans are exercised separately; here jobs just get created.
    async def no_scan(self, job_id):
        return None

    monkeypatch.setattr(gt.GDriveTransferQueue, "_scan_job", no_scan)

    async with maker() as s:
        client = GDriveOAuthClient(label="c", client_id="cid",
                                   client_secret_enc=gdrive_crypto.encrypt("sec"))
        s.add(client)
        await s.flush()
        acc = GDriveAccount(label="A", email="a@x", oauth_client_id=client.id,
                            refresh_token_enc=gdrive_crypto.encrypt("rt"))
        acc2 = GDriveAccount(label="B", email="b@x", oauth_client_id=client.id,
                             refresh_token_enc=gdrive_crypto.encrypt("rt2"))
        s.add_all([acc, acc2])
        for code, kw in {
            "OLD-001": {"studio": "s1", "stars": ("star1",)},
            "OLD-002": {"studio": "s1"},
            "NEW-003": {"studio": "s1"},
            "BOTH-004": {"studio": "s1", "stars": ("star1",)},
            "RAW-005": {"studio": "s1"},
            "GONE-006": {"studio": "s1"},  # not on PikPak
        }.items():
            s.add(MovieDetailCache(code=code, detail=_detail(code, **kw), release_date="", fetched_at=OLD))
            if code in present:
                s.add(PresenceEntry(code=code, path=f"AVBT/製作商/S1/未分類/{code}",
                                    updated_at=datetime.utcnow() if code == "NEW-003" else OLD))
        # RAW-005 is archived but not finalized yet.
        s.add(OfflineTaskLog(code="RAW-005", magnet="m", archived=True, finalized=False))
        s.add(TrackedListing(kind="studio", id="s1", name="S1", gdrive_enabled=True))
        s.add(TrackedListing(kind="star", id="star1", name="Star", gdrive_enabled=True))
        await s.commit()
        for row in (await s.execute(select(TrackedListing))).scalars():
            row.gdrive_account_id = acc.id if row.kind == "studio" else acc2.id
        await s.commit()
        ids = {"acc": acc.id, "acc2": acc2.id}
    yield maker, ids
    gdrive_crypto.reset_cache()
    await engine.dispose()


async def test_codes_for_listing_by_kind(env):
    assert await lc.codes_for_listing("studio", "s1") == {
        "OLD-001", "OLD-002", "NEW-003", "BOTH-004", "RAW-005"
    }
    assert await lc.codes_for_listing("star", "star1") == {"OLD-001", "BOTH-004"}
    assert await lc.codes_for_listing("series", "nope") == set()


async def test_reconcile_skips_unsettled_and_handled(env):
    maker, ids = env
    # OLD-002 already on Drive → skipped too.
    async with maker() as s:
        s.add(GDriveLocation(code="OLD-002", account_id=ids["acc"], drive_file_id="F", path="p"))
        await s.commit()
    created = await gauto.reconcile(("studio", "s1"))
    assert created == 1
    async with maker() as s:
        job = (await s.execute(select(GDriveJob))).scalars().one()
    assert job.origin == "auto" and job.listing_kind == "studio" and job.account_id == ids["acc"]
    # NEW-003 landed just now, RAW-005 not finalized, OLD-002 on Drive.
    assert json.loads(job.codes) == ["BOTH-004", "OLD-001"]
    # Active job → a second pass creates nothing.
    assert await gauto.reconcile(("studio", "s1")) == 0


async def test_overlapping_listing_skips_codes_already_queued(env):
    maker, ids = env
    await gauto.reconcile(("studio", "s1"))
    async with maker() as s:
        job = (await s.execute(select(GDriveJob))).scalars().one()
        for code in json.loads(job.codes):
            s.add(GDriveTransfer(job_id=job.id, account_id=ids["acc"], code=code,
                                 pikpak_file_id=f"f-{code}", status="pending"))
        await s.commit()
    # star1's works (OLD-001, BOTH-004) are already queued under the studio job.
    assert await gauto.reconcile(("star", "star1")) == 0


async def test_overlap_while_other_job_still_scanning(env):
    # The studio job exists but hasn't created transfer rows yet.
    await gauto.reconcile(("studio", "s1"))
    assert await gauto.reconcile(("star", "star1")) == 0


async def test_backs_off_after_failed_scan(env):
    maker, _ids = env
    await gauto.reconcile(("studio", "s1"))
    async with maker() as s:
        job = (await s.execute(select(GDriveJob))).scalars().one()
        job.status, job.message, job.finished_at = "error", "PikPak 尚未登入", datetime.utcnow()
        await s.commit()
    assert await gauto.reconcile(("studio", "s1")) == 0
    async with maker() as s:
        row = await s.get(TrackedListing, ("studio", "s1"))
        assert "PikPak 尚未登入" in row.gdrive_last_error
        job = (await s.execute(select(GDriveJob))).scalars().one()
        job.finished_at = datetime.utcnow() - timedelta(hours=2)
        await s.commit()
    assert await gauto.reconcile(("studio", "s1")) == 1  # retried after back-off


async def test_reconcile_records_unusable_account(env):
    maker, ids = env
    async with maker() as s:
        acc = await s.get(GDriveAccount, ids["acc"])
        acc.enabled = False
        await s.commit()
    assert await gauto.reconcile(("studio", "s1")) == 0
    async with maker() as s:
        row = await s.get(TrackedListing, ("studio", "s1"))
    assert "停用" in row.gdrive_last_error


async def test_scan_skips_files_on_drive_or_busy(env, monkeypatch):
    maker, ids = env
    q = gt.GDriveTransferQueue()

    async def files_for_leaf(self, leaf):
        code = leaf.rsplit("/", 1)[1]
        specs = gt.plan_leaf_files(
            leaf, True, [{"file_id": f"f-{code}", "name": f"{code}.mp4", "size": 10, "rel_dir": ""}]
        )
        return [dict(sp, leaf_id=f"l-{code}") for sp in specs]

    monkeypatch.setattr(gt.GDriveTransferQueue, "_files_for_leaf", files_for_leaf)
    monkeypatch.setattr(gt.presence_index, "paths_for",
                        lambda c: [f"AVBT/製作商/S1/未分類/{c}"])
    async with maker() as s:
        job = GDriveJob(listing_kind="star", studio_id="star1", account_id=ids["acc2"],
                        status="scanning", codes=json.dumps(["OLD-001", "BOTH-004", "OLD-002"]))
        other = GDriveJob(listing_kind="studio", studio_id="s1", account_id=ids["acc"],
                          status="transferring")
        s.add_all([job, other])
        await s.flush()
        # BOTH-004 queued elsewhere; OLD-002 already on Drive (other account).
        s.add(GDriveTransfer(job_id=other.id, account_id=ids["acc"], code="BOTH-004",
                             pikpak_file_id="f-BOTH-004", status="running"))
        s.add(GDriveLocation(code="OLD-002", account_id=ids["acc"], drive_file_id="F2",
                             path=gt.drive_path_for("AVBT/製作商/S1/未分類/OLD-002/OLD-002.mp4")))
        await s.commit()
        job_id = job.id
    await q._scan_job_inner(job_id)
    async with maker() as s:
        rows = (await s.execute(select(GDriveTransfer).where(GDriveTransfer.job_id == job_id))).scalars().all()
        job = await s.get(GDriveJob, job_id)
    assert [r.code for r in rows] == ["OLD-001"]
    assert job.status == "transferring" and "略過" in job.message


async def test_settings_endpoint(env, monkeypatch):
    maker, ids = env
    fired = []
    monkeypatch.setattr(gauto, "trigger", lambda k, i: fired.append((k, i)))
    async with maker() as s:
        with pytest.raises(HTTPException):
            await tracked_router.set_gdrive(
                "studio", "s1", tracked_router.GDriveSettingIn(enabled=True, account_id=999), s
            )
        out = await tracked_router.set_gdrive(
            "studio", "s1",
            tracked_router.GDriveSettingIn(enabled=True, account_id=ids["acc2"], delete_source=False),
            s,
        )
    assert out.gdrive_enabled and out.gdrive_account_id == ids["acc2"]
    assert out.gdrive_delete_source is False
    assert fired == [("studio", "s1")]
    async with maker() as s:
        out = await tracked_router.set_gdrive(
            "studio", "s1", tracked_router.GDriveSettingIn(enabled=False, account_id=0), s
        )
    assert not out.gdrive_enabled and out.gdrive_account_id == ids["acc2"]
    assert fired == [("studio", "s1")]  # switching off doesn't trigger
