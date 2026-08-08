"""Escalating backoff for rows the finalize pass keeps failing on.

Live 2026-08-08, first pass after the #273 instrumentation landed:

    finalize pass: selected=190 candidates=33 done=1 elapsed=30.6s
      tasklist=0.6s presence=3.7s rows=26.2s pikpak_calls=461

33 candidates, 461 PikPak round trips, ONE row finalized. The other 32
were rows that can never finalize — 21 of them "找不到 X 的歸檔資料夾"
for codes whose download died at 0% and whose files never landed. Under
the flat 10-minute cooldown each one came back every 10 minutes forever,
burning ~14 round trips apiece and pushing genuinely finalizable rows to
the back of a budget-bounded pass. archived→finalized median went 6-8
min → 28 min.

Backoff only — a row is never permanently dropped from the pool, because
skipping finalize means its junk is never purged. The ceiling keeps a
row that failed transiently coming back within 6 h.
"""

from datetime import datetime, timedelta

from app.services import archiver as arch

# ---------------------------------------------------------------------------
# the delay curve (pure)
# ---------------------------------------------------------------------------

def test_first_failure_keeps_the_current_ten_minute_cooldown():
    """Unchanged for the common case: one transient miss (a move that
    hadn't landed yet) still retries promptly."""
    assert arch._finalize_backoff(1) == timedelta(minutes=10)


def test_backoff_doubles_per_consecutive_failure():
    assert arch._finalize_backoff(2) == timedelta(minutes=20)
    assert arch._finalize_backoff(3) == timedelta(minutes=40)
    assert arch._finalize_backoff(4) == timedelta(minutes=80)


def test_backoff_is_capped():
    """A permanently-stuck row must still come back eventually — the
    operator fixes these by hand and the retry is how the fix is picked
    up. Uncapped doubling would push it past any useful horizon."""
    assert arch._finalize_backoff(20) == arch._FINALIZE_RETRY_COOLDOWN_MAX
    assert arch._FINALIZE_RETRY_COOLDOWN_MAX == timedelta(hours=6)


def test_zero_failures_means_no_wait():
    assert arch._finalize_backoff(0) == timedelta(0)


# ---------------------------------------------------------------------------
# gate behaviour
# ---------------------------------------------------------------------------

def test_gate_blocks_within_the_escalated_window():
    """The regression this fixes: at 15 minutes a twice-failed row was
    eligible again under the flat cooldown. It must not be."""
    now = datetime(2026, 8, 8, 12, 0, 0)
    arch._finalize_attempts.clear()
    arch._finalize_attempts[1] = (now - timedelta(minutes=15), 2)
    assert arch._finalize_in_cooldown(1, now) is True


def test_gate_releases_once_the_escalated_window_expires():
    now = datetime(2026, 8, 8, 12, 0, 0)
    arch._finalize_attempts.clear()
    arch._finalize_attempts[1] = (now - timedelta(minutes=25), 2)
    assert arch._finalize_in_cooldown(1, now) is False


def test_unknown_row_is_never_in_cooldown():
    arch._finalize_attempts.clear()
    assert arch._finalize_in_cooldown(999, datetime(2026, 8, 8)) is False


def test_recording_a_failure_increments_the_streak():
    now = datetime(2026, 8, 8, 12, 0, 0)
    arch._finalize_attempts.clear()
    arch._note_finalize_failure(7, now)
    arch._note_finalize_failure(7, now)
    arch._note_finalize_failure(7, now)
    assert arch._finalize_attempts[7] == (now, 3)


def test_success_clears_the_streak_so_the_next_failure_starts_at_ten_min():
    """A row that finally finalizes, then later lands in the pool again
    (a re-sent code) must not inherit the old row's 6 h penalty."""
    now = datetime(2026, 8, 8, 12, 0, 0)
    arch._finalize_attempts.clear()
    for _ in range(5):
        arch._note_finalize_failure(7, now)
    arch._clear_finalize_attempts(7)
    arch._note_finalize_failure(7, now)
    assert arch._finalize_attempts[7] == (now, 1)
    assert arch._finalize_in_cooldown(7, now + timedelta(minutes=11)) is False


# ---------------------------------------------------------------------------
# the map must stay bounded
# ---------------------------------------------------------------------------

def test_stale_entries_are_pruned():
    """Entries used to accumulate for every row id ever attempted and
    nothing dropped them. Holding them longer (that is the point of
    backoff) makes the leak worse, so prune what the selection window
    can no longer return."""
    now = datetime(2026, 8, 8, 12, 0, 0)
    arch._finalize_attempts.clear()
    arch._finalize_attempts[1] = (now - arch._FINALIZE_RETRY_WINDOW
                                  - timedelta(hours=1), 3)
    arch._finalize_attempts[2] = (now - timedelta(minutes=5), 1)
    arch._prune_finalize_attempts(now)
    assert 1 not in arch._finalize_attempts
    assert 2 in arch._finalize_attempts


def test_prune_keeps_a_row_still_inside_its_own_long_backoff():
    """A row on the 6 h ceiling is younger than the window — dropping it
    would reset its streak and hand it a free retry, undoing the backoff."""
    now = datetime(2026, 8, 8, 12, 0, 0)
    arch._finalize_attempts.clear()
    arch._finalize_attempts[1] = (now - timedelta(hours=5), 12)
    arch._prune_finalize_attempts(now)
    assert arch._finalize_attempts[1] == (now - timedelta(hours=5), 12)


# ---------------------------------------------------------------------------
# end to end through the real pass
# ---------------------------------------------------------------------------

async def test_pass_escalates_a_repeatedly_failing_row(tmp_path, monkeypatch, caplog):
    """The whole point, exercised through _finalize_retry_pass rather
    than the helpers: a row that keeps failing must stop coming back
    every 10 minutes, and the summary must say how many are parked."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import OfflineTaskLog
    from app.services import finalize as fin
    from app.services.pikpak_presence import presence_index

    now = datetime.utcnow()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(arch, "SessionLocal", maker)

    async def fake_refresh(codes):
        return 0

    async def no_active():
        return set()

    async def never_finalizes(svc, code, *, folder_id=None, **_kw):
        return None  # "找不到歸檔資料夾" — ran, nothing to finalize

    async def not_flattened(code, *, strict=False):
        return False

    monkeypatch.setattr(presence_index, "refresh_codes", fake_refresh)
    monkeypatch.setattr(arch, "_active_task_ids", no_active)
    monkeypatch.setattr(fin, "run_finalize", never_finalizes)
    monkeypatch.setattr(arch, "_already_flattened", not_flattened)
    arch._finalize_attempts.clear()

    async with maker() as s:
        s.add(OfflineTaskLog(code="DEAD-001", magnet="m", archived=True,
                             archived_at=now, finalized=False,
                             created_at=now - timedelta(hours=1)))
        await s.commit()

    # Pass 1 attempts it and records failure #1.
    assert await arch._finalize_retry_pass() == 0
    assert arch._finalize_attempts[1][1] == 1

    # 11 minutes later the flat cooldown would let it through; it does,
    # and fails again → streak 2, so the next window is 20 minutes.
    arch._finalize_attempts[1] = (datetime.utcnow() - timedelta(minutes=11), 1)
    assert await arch._finalize_retry_pass() == 0
    assert arch._finalize_attempts[1][1] == 2

    # 15 minutes on: under the old flat rule this row would be retried.
    arch._finalize_attempts[1] = (datetime.utcnow() - timedelta(minutes=15), 2)
    with caplog.at_level(logging.INFO, logger="app.services.archiver"):
        assert await arch._finalize_retry_pass() == 0
    assert arch._finalize_attempts[1][1] == 2, "must not have been attempted"
    msg = [r.getMessage() for r in caplog.records if "finalize pass:" in r.getMessage()][-1]
    assert "candidates=0" in msg
    assert "cooling=1" in msg

    # 25 minutes on: past the escalated window, it gets its next attempt.
    arch._finalize_attempts[1] = (datetime.utcnow() - timedelta(minutes=25), 2)
    assert await arch._finalize_retry_pass() == 0
    assert arch._finalize_attempts[1][1] == 3

    await engine.dispose()
