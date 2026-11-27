"""PikPak → Google Drive mover: path mapping, traffic policy, token
crypto, and the streaming upload worker end-to-end against fakes."""

from datetime import datetime, time, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as db
import app.services.gdrive_accounts as ga
import app.services.gdrive_config as gc
import app.services.gdrive_policy as gp
import app.services.gdrive_transfer as gt
import app.services.studio_index as si
from app.config import settings
from app.models import (
    GDriveAccount,
    GDriveJob,
    GDriveLocation,
    GDriveOAuthClient,
    GDriveTransfer,
)
from app.services import gdrive, gdrive_crypto


@pytest.fixture(autouse=True)
def _isolated_key_file(tmp_path, monkeypatch):
    """Never read/write ./data/gdrive_token_key.txt from tests."""
    monkeypatch.setattr(gdrive_crypto, "KEY_FILE", tmp_path / "gdrive_token_key.txt")
    # OAuth state is signed with the login secret; a fixed one keeps
    # _get_secret from writing ./data/auth_secret.txt.
    monkeypatch.setattr(settings, "auth_secret", "test-secret")
    gdrive_crypto.reset_cache()
    yield
    gdrive_crypto.reset_cache()


# ---------------------------------------------------------------------------
# pure helpers


def test_plan_leaf_folder_mirrors_paths(monkeypatch):
    monkeypatch.setattr(settings, "gdrive_root_folder", "")
    specs = gt.plan_leaf_files(
        "AVBT/製作商/S1/未分類/SSIS-001",
        True,
        [
            {"file_id": "a", "name": "SSIS-001.mp4", "size": 100, "rel_dir": ""},
            {"file_id": "b", "name": "x.mp4", "size": 5, "rel_dir": "sub"},
            {"file_id": "c", "name": "empty.txt", "size": 0, "rel_dir": ""},
        ],
    )
    assert [s["file_id"] for s in specs] == ["a", "b"]  # zero-byte dropped
    assert specs[0]["pikpak_path"] == "AVBT/製作商/S1/未分類/SSIS-001/SSIS-001.mp4"
    assert specs[0]["drive_dir"] == "AVBT/製作商/S1/未分類/SSIS-001"
    assert specs[1]["drive_dir"] == "AVBT/製作商/S1/未分類/SSIS-001/sub"
    assert specs[1]["drive_code_dir"] == "AVBT/製作商/S1/未分類/SSIS-001"


def test_plan_bare_file_leaf_and_root_prefix(monkeypatch):
    monkeypatch.setattr(settings, "gdrive_root_folder", "/Backup/")
    specs = gt.plan_leaf_files(
        "AVBT/製作商/S1/系列A/SSIS-002.mp4",
        False,
        [{"file_id": "a", "name": "SSIS-002.mp4", "size": 9}],
    )
    assert specs[0]["drive_dir"] == "Backup/AVBT/製作商/S1/系列A"
    assert specs[0]["drive_code_dir"] == "Backup/AVBT/製作商/S1/系列A"
    assert specs[0]["pikpak_path"] == "AVBT/製作商/S1/系列A/SSIS-002.mp4"


def test_job_outcome():
    assert gt.job_outcome({"pending": 1, "done": 3}) is None
    assert gt.job_outcome({"running": 1}) is None
    assert gt.job_outcome({"done": 4}) == "done"
    assert gt.job_outcome({"done": 3, "failed": 1}) == "partial"
    assert gt.job_outcome({"done": 3, "cancelled": 1}) == "partial"


def test_escape_q_and_chunk_alignment(monkeypatch):
    assert gdrive.escape_q("it's a\\b") == "it\\'s a\\\\b"
    monkeypatch.setattr(settings, "gdrive_chunk_mb", 5)
    assert gdrive.chunk_size() % gdrive.CHUNK_ALIGN == 0


def test_window_parsing_and_wraparound():
    assert gp.parse_window("") is None
    assert gp.parse_window("garbage") is None
    assert gp.parse_window("03:00-03:00") is None
    w = gp.parse_window("23:00-06:30")
    assert gp.in_window(w, time(23, 30))
    assert gp.in_window(w, time(2, 0))
    assert not gp.in_window(w, time(6, 30))
    assert not gp.in_window(w, time(12, 0))
    day = gp.parse_window("01:00-07:00")
    assert gp.in_window(day, time(1, 0)) and not gp.in_window(day, time(7, 0))
    now = datetime(2026, 1, 1, 12, 0)
    assert gp.seconds_until_window(day, now) == 13 * 3600
    assert gp.seconds_until_window(day, datetime(2026, 1, 1, 2, 0)) == 0


def test_policy_block_reasons():
    noon = datetime(2026, 1, 1, 12, 0)
    p = gp.Policy(budget_bytes=0, window_raw="", paused=False, used_today=10**12)
    assert p.block_reason(noon) == "" and p.remaining() is None
    p = gp.Policy(budget_bytes=100, window_raw="", paused=False, used_today=100)
    assert "預算" in p.block_reason(noon)
    p = gp.Policy(budget_bytes=0, window_raw="01:00-07:00", paused=False, used_today=0)
    assert "時段" in p.block_reason(noon)
    p = gp.Policy(budget_bytes=0, window_raw="", paused=True, used_today=0)
    assert p.block_reason(noon) == "已暫停"


def test_token_crypto_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "gdrive_token_key", "")
    assert not gdrive_crypto.key_configured()
    monkeypatch.setattr(settings, "gdrive_token_key", Fernet.generate_key().decode())
    sealed = gdrive_crypto.encrypt("1//refresh")
    assert "refresh" not in sealed
    assert gdrive_crypto.decrypt(sealed) == "1//refresh"
    monkeypatch.setattr(settings, "gdrive_token_key", Fernet.generate_key().decode())
    with pytest.raises(gdrive_crypto.TokenKeyError):
        gdrive_crypto.decrypt(sealed)


def test_oauth_state_roundtrip():
    st = ga.make_state(client_pk=2, label="主帳號", account_id=3)
    claims = ga.read_state(st)
    assert claims["label"] == "主帳號" and claims["aid"] == 3 and claims["cid"] == 2
    assert ga.read_state(st + "x") is None


# ---------------------------------------------------------------------------
# worker end-to-end against fakes

MiB = 1024 * 1024


class FakeDrive:
    """Stands in for ``gdrive_client``: records chunks, enforces that each
    PUT starts exactly at the committed offset (i.e. resume is correct)."""

    def __init__(self, source: bytes) -> None:
        self.source = source
        self.received = bytearray()
        self.sessions = 0
        self.public: list[str] = []
        self.range_requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            rng = request.headers.get("Range")
            self.range_requests.append(rng or "")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                return httpx.Response(206, content=self.source[start:])
            return httpx.Response(200, content=self.source)

        self._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def http(self):
        return self._http

    async def ensure_path(self, auth, path):
        return "folder:" + path

    async def start_upload(self, auth, *, name, folder_id, size, mime=""):
        self.sessions += 1
        self.received = bytearray()
        return f"https://upload/session/{self.sessions}"

    async def query_upload(self, uri, size):
        if len(self.received) == size:
            return size, {"id": "F1", "size": str(size)}
        return len(self.received), None

    async def put_chunk(self, uri, data, offset, size):
        assert offset == len(self.received), "chunk not contiguous with committed bytes"
        self.received += data
        if len(self.received) == size:
            return size, {"id": "F1", "size": str(size)}
        return len(self.received), None

    async def make_public(self, auth, file_id):
        self.public.append(file_id)

    async def trash(self, auth, file_id):
        pass


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/g.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    for mod in (gt, ga, gc, gp, si):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    monkeypatch.setattr(settings, "gdrive_token_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "gdrive_chunk_mb", 1)
    monkeypatch.setattr(settings, "gdrive_root_folder", "")

    source = bytes(range(256)) * (10 * MiB // 256) + b"tail"
    fake = FakeDrive(source)
    monkeypatch.setattr(gt, "gdrive_client", fake)

    trashed: list[list[str]] = []

    async def file_links(fid):
        return {"download_url": "https://pikpak.example/dl"}

    async def trash_files(ids):
        trashed.append(list(ids))
        return {}

    async def refresh_codes(codes, **kw):
        return 0

    monkeypatch.setattr(gt.pikpak_service, "file_links", file_links)
    monkeypatch.setattr(gt.pikpak_service, "trash_files", trash_files)
    monkeypatch.setattr(gt.presence_index, "refresh_codes", refresh_codes)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gt.webhook_queue, "enqueue_nowait", lambda msg, event="": sent.append((event, msg))
    )

    async with maker() as s:
        client = GDriveOAuthClient(
            label="C", client_id="cid-1", client_secret_enc=gdrive_crypto.encrypt("sec")
        )
        s.add(client)
        await s.flush()
        acc = GDriveAccount(
            label="A", email="a@x", refresh_token_enc=gdrive_crypto.encrypt("rt"),
            oauth_client_id=client.id,
        )
        s.add(acc)
        await s.flush()
        job = GDriveJob(
            studio_id="s1", studio_name="S1", account_id=acc.id,
            status="transferring", delete_source=True, source_state="pending",
        )
        s.add(job)
        await s.flush()
        tr = GDriveTransfer(
            job_id=job.id, account_id=acc.id, code="SSIS-001",
            pikpak_file_id="pf1", pikpak_name="SSIS-001.mp4", pikpak_size=len(source),
            pikpak_path="AVBT/製作商/S1/未分類/SSIS-001/SSIS-001.mp4",
            source_leaf_id="leaf1",
            drive_dir="AVBT/製作商/S1/未分類/SSIS-001",
            drive_code_dir="AVBT/製作商/S1/未分類/SSIS-001",
        )
        s.add(tr)
        await s.commit()
        ids = SimpleNamespace(account=acc.id, job=job.id, transfer=tr.id)

    yield SimpleNamespace(
        maker=maker, fake=fake, source=source, trashed=trashed, sent=sent, ids=ids
    )
    await fake.http().aclose()
    await engine.dispose()


async def _run(queue, rid):
    claimed = await queue._claim(1)
    assert claimed == [rid]
    await queue._run_one(rid)


async def test_full_upload_moves_and_trashes_source(env):
    q = gt.GDriveTransferQueue()
    await _run(q, env.ids.transfer)

    assert bytes(env.fake.received) == env.source
    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        job = await s.get(GDriveJob, env.ids.job)
        locs = (await s.execute(select(GDriveLocation))).scalars().all()
        acc = await s.get(GDriveAccount, env.ids.account)
    assert tr.status == "done" and tr.drive_file_id == "F1"
    assert job.status == "done" and job.source_state == "trashed"
    assert env.trashed == [["leaf1"]]
    assert len(locs) == 1 and locs[0].code == "SSIS-001" and locs[0].is_video
    assert locs[0].path == "AVBT/製作商/S1/未分類/SSIS-001/SSIS-001.mp4"
    assert env.fake.public == ["F1"]
    assert acc.daily_uploaded_bytes == len(env.source)
    assert (await gp.load()).used_today == len(env.source)
    assert env.sent and env.sent[-1][0] == "gdrive_done"


async def test_budget_pauses_midway_then_resumes_from_offset(env):
    # Budget of ~3 MiB: the worker must stop at a chunk boundary, park the
    # row as pending (session kept), and resume from the same byte later.
    await gp.update(daily_budget_gb=3 * MiB / gp.GB)
    q = gt.GDriveTransferQueue()
    await _run(q, env.ids.transfer)

    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        job = await s.get(GDriveJob, env.ids.job)
    assert tr.status == "pending" and "預算" in tr.message
    assert tr.session_uri and tr.bytes_uploaded == 3 * MiB
    assert job.status == "transferring" and env.trashed == []

    # Lift the budget, clear the park delay, run again.
    await gp.update(daily_budget_gb=0)
    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        tr.next_retry_at = None
        await s.commit()
    await _run(q, env.ids.transfer)

    assert bytes(env.fake.received) == env.source
    assert env.fake.sessions == 1  # resumed, not restarted
    assert env.fake.range_requests[-1] == f"bytes={3 * MiB}-"
    async with env.maker() as s:
        job = await s.get(GDriveJob, env.ids.job)
    assert job.status == "done" and env.trashed == [["leaf1"]]


async def test_failure_keeps_source_and_marks_partial(env, monkeypatch):
    monkeypatch.setattr(settings, "gdrive_transfer_max_attempts", 1)

    async def broken_links(fid):
        return {"download_url": ""}

    monkeypatch.setattr(gt.pikpak_service, "file_links", broken_links)
    q = gt.GDriveTransferQueue()
    await _run(q, env.ids.transfer)

    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        job = await s.get(GDriveJob, env.ids.job)
    assert tr.status == "failed"
    assert job.status == "partial"
    assert env.trashed == []
    assert env.sent[-1][0] == "gdrive_failed"

    # Retry flips the job back to transferring.
    assert await q.retry_job(env.ids.job) == 1
    async with env.maker() as s:
        assert (await s.get(GDriveJob, env.ids.job)).status == "transferring"


async def test_account_daily_cap_defers_claim(env, monkeypatch):
    monkeypatch.setattr(settings, "gdrive_account_daily_limit_gb", 1.0)
    await ga.add_uploaded(env.ids.account, gp.GB)
    q = gt.GDriveTransferQueue()
    assert await q._claim(1) == []
    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
    assert tr.status == "pending" and tr.next_retry_at is not None
    assert "額度" in tr.message


# ---------------------------------------------------------------------------
# PikPak login cooldown: wait, don't fail


def test_pikpak_unavailable_reads_cooldown(monkeypatch):
    monkeypatch.setattr(
        gt.pikpak_service, "status",
        lambda: {"login_cooldown_seconds": 0, "login_block_reason": ""},
    )
    assert gt.pikpak_unavailable() is None
    monkeypatch.setattr(
        gt.pikpak_service, "status",
        lambda: {"login_cooldown_seconds": 3540, "login_block_reason": "operation is too frequent"},
    )
    reason, secs = gt.pikpak_unavailable()
    assert secs == 3540 and "59 分鐘" in reason and "too frequent" in reason


async def test_cooldown_parks_without_burning_attempts(env, monkeypatch):
    async def blocked(fid):
        raise gt.PikPakLoginBlocked(
            "登入冷卻中(約剩 60 分鐘)。上次失敗原因: operation is too frequent", 3600.0
        )

    monkeypatch.setattr(gt.pikpak_service, "file_links", blocked)
    monkeypatch.setattr(
        gt.pikpak_service, "status",
        lambda: {"login_cooldown_seconds": 3600, "login_block_reason": "too frequent"},
    )
    monkeypatch.setattr(settings, "gdrive_transfer_max_attempts", 1)  # would fail at once otherwise
    q = gt.GDriveTransferQueue()
    await _run(q, env.ids.transfer)

    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        job = await s.get(GDriveJob, env.ids.job)
    assert tr.status == "pending" and tr.attempts == 0
    assert "PikPak" in tr.message and tr.session_uri  # resumable session kept
    assert (tr.next_retry_at - datetime.utcnow()).total_seconds() > 3000
    assert job.status == "transferring" and env.sent == []


async def _add_transfer(env, code, leaf, status):
    async with env.maker() as s:
        s.add(
            GDriveTransfer(
                job_id=env.ids.job, account_id=env.ids.account, code=code,
                pikpak_file_id=f"pf-{code}-{status}", pikpak_name=f"{code}.mp4", pikpak_size=1,
                pikpak_path=f"AVBT/製作商/S1/未分類/{code}.mp4", source_leaf_id=leaf,
                drive_dir="AVBT/製作商/S1/未分類", drive_code_dir="AVBT/製作商/S1/未分類",
                status=status,
            )
        )
        await s.commit()


async def _set_status(env, job_status, transfer_status):
    async with env.maker() as s:
        (await s.get(GDriveJob, env.ids.job)).status = job_status
        (await s.get(GDriveTransfer, env.ids.transfer)).status = transfer_status
        await s.commit()


async def test_partial_job_trashes_only_done_leaves_on_request(env, monkeypatch):
    """One permanently failed file must not pin every moved original on
    PikPak: an explicit trash-source on a partial job trashes the done
    rows' leaves and nothing else."""
    await _set_status(env, "partial", "done")  # leaf1 done
    await _add_transfer(env, "SSIS-002", "leaf2", "failed")
    await _add_transfer(env, "SSIS-003", "leaf3", "cancelled")
    # A folder leaf shared by a done part and a failed part stays.
    await _add_transfer(env, "SSIS-004", "leaf4", "done")
    await _add_transfer(env, "SSIS-004", "leaf4", "failed")
    refreshed = []

    async def refresh_codes(codes, **kw):
        refreshed.append((list(codes), kw))
        return 0

    monkeypatch.setattr(gt.presence_index, "refresh_codes", refresh_codes)
    q = gt.GDriveTransferQueue()
    assert await q.trash_source(env.ids.job) is True
    assert env.trashed == [["leaf1"]]
    assert refreshed[0][0] == ["SSIS-001"]
    async with env.maker() as s:
        job = await s.get(GDriveJob, env.ids.job)
    assert job.status == "partial" and job.source_state == "trashed"
    assert "1 個" in job.message and "3 個" in job.message


async def test_partial_job_trash_refused_when_unsettled_or_nothing_done(env):
    q = gt.GDriveTransferQueue()
    # Nothing done → nothing to trash.
    await _set_status(env, "partial", "failed")
    assert await q.trash_source(env.ids.job) is False
    # A row still queued (job flipped back by a retry race) → refuse.
    await _set_status(env, "partial", "done")
    await _add_transfer(env, "SSIS-002", "leaf2", "pending")
    assert await q.trash_source(env.ids.job) is False
    # Still transferring → refuse, as before.
    await _set_status(env, "transferring", "done")
    assert await q.trash_source(env.ids.job) is False
    assert env.trashed == []


async def test_partial_outcome_never_trashes_automatically(env):
    """The automatic settle path keeps every original of a partial job."""
    await _add_transfer(env, "SSIS-002", "leaf2", "failed")
    q = gt.GDriveTransferQueue()
    await _run(q, env.ids.transfer)
    async with env.maker() as s:
        job = await s.get(GDriveJob, env.ids.job)
    assert job.status == "partial" and job.source_state == "pending"
    assert env.trashed == []


async def test_retry_all_requeues_every_partial_job(env):
    async with env.maker() as s:
        job = await s.get(GDriveJob, env.ids.job)
        job.status = "partial"
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        tr.status, tr.attempts = "failed", 5
        await s.commit()
    q = gt.GDriveTransferQueue()
    assert await q.retry_all() == 1
    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        job = await s.get(GDriveJob, env.ids.job)
    assert tr.status == "pending" and tr.attempts == 0 and job.status == "transferring"


async def test_pikpak_login_cooldown_parks_without_burning_attempts(env, monkeypatch):
    """A PikPak login cooldown is an outage, not a per-file failure.

    Live 2026-11-21: a 30-min cooldown failed every claim instantly, and
    the 120/240/480/960s retry ladder sums to exactly 30 min — 821 queued
    transfers hit max_attempts and went ``failed`` without one byte sent.
    """
    from app.services.pikpak import PikPakLoginBlocked

    async def blocked(fid):
        raise PikPakLoginBlocked("登入冷卻中(約剩 30 分鐘)。", 1800.0)

    monkeypatch.setattr(gt.pikpak_service, "file_links", blocked)
    q = gt.GDriveTransferQueue()
    before = datetime.utcnow()
    await _run(q, env.ids.transfer)

    async with env.maker() as s:
        tr = await s.get(GDriveTransfer, env.ids.transfer)
        job = await s.get(GDriveJob, env.ids.job)
    assert tr.status == "pending"
    assert (tr.attempts or 0) == 0
    assert "PikPak" in tr.message
    assert tr.next_retry_at >= before + timedelta(seconds=1800)
    assert job.status == "transferring"
    assert env.trashed == [] and env.sent == []


def test_login_cooldown_raises_typed_error_with_remaining():
    from app.services.pikpak import PikPakError, PikPakLoginBlocked, PikPakService

    svc = PikPakService()
    svc._note_login_failure(RuntimeError("Your operation is too frequent"))
    with pytest.raises(PikPakLoginBlocked) as ei:
        svc._raise_if_login_blocked(explicit=False)
    assert isinstance(ei.value, PikPakError)
    assert 0 < ei.value.remaining <= 1800
