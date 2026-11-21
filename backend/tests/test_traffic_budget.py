"""Spread PikPak's monthly offline-traffic allowance across the month.

PikPak meters cloud-download traffic separately from storage: 40 TiB
(43,980,465,111,040 B) that resets to zero on the 1st, Taipei time.
Observed live in the rota log:

    === 2026-07-31 19:15 UTC (r364) ===
    [流量閘] 已重置開啟(r143-r361 連續 219 輪關後首開):
             offline.assets 歸零重計,現僅 109.2GB / 40T

That reset was 2026-08-01 03:15 CST. The pipeline then burned the whole
40 TiB in **5.2 days** (last task created 08-06 07:40) and every submit
since fails with ``Cloud Download Traffic 40.x T has exceeded the limit
40 T``. July was the same shape: the gate sat shut for 219 consecutive
rota rounds. Two months running, the month's budget was gone in its
first third and the pipeline sat dark for the rest.

6,722 tasks consumed ~44 TB in that window — ~6.5 GB each. Sustainable
is 43.98 TB / 31 days = 1.42 TB/day ≈ 218 tasks/day; 08-03 alone sent
2,745. This gate makes the budget last the month instead of the week.
"""

from datetime import date

from app.services import traffic_budget as tb

TIB40 = 43_980_465_111_040


# ---------------------------------------------------------------------------
# days remaining (pure)
# ---------------------------------------------------------------------------

def test_days_left_counts_today():
    """On the 1st the whole month is ahead; on the last day only today."""
    assert tb._days_left_in_month(date(2026, 8, 1)) == 31
    assert tb._days_left_in_month(date(2026, 8, 31)) == 1
    assert tb._days_left_in_month(date(2026, 8, 8)) == 24


def test_days_left_handles_short_and_leap_months():
    assert tb._days_left_in_month(date(2026, 9, 1)) == 30
    assert tb._days_left_in_month(date(2026, 2, 1)) == 28
    assert tb._days_left_in_month(date(2028, 2, 1)) == 29


# ---------------------------------------------------------------------------
# the allowance decision (pure)
# ---------------------------------------------------------------------------

def test_fresh_month_allows_one_thirty_first_of_the_budget():
    p = tb.plan(used=0, limit=TIB40, day_start_used=0, today=date(2026, 8, 1))
    assert p.allowance == TIB40 // 31
    assert p.spent_today == 0
    assert p.is_open is True


def test_gate_shuts_once_today_share_is_spent():
    """The whole point: 08-03 sent 2,745 tasks (~17 TB) in one day
    against a 1.42 TB share. Everything past the share waits for
    tomorrow instead of eating the month."""
    share = TIB40 // 31
    p = tb.plan(used=share + 1, limit=TIB40, day_start_used=0,
                today=date(2026, 8, 1))
    assert p.is_open is False
    assert p.spent_today == share + 1


def test_unspent_budget_rolls_forward_into_a_bigger_daily_share():
    """Underspending must not be lost — a quiet week should leave more
    per day for the rest of the month, not the same flat 1/31."""
    # Half the month gone, nothing spent: 40 TiB across the last 16 days.
    p = tb.plan(used=0, limit=TIB40, day_start_used=0,
                today=date(2026, 8, 16))
    assert p.allowance == TIB40 // 16
    assert p.allowance > TIB40 // 31


def test_overspend_shrinks_the_remaining_days_share():
    """Symmetric: burn 90% early and the rest of the month gets what is
    actually left, not another 1/31 each."""
    used = int(TIB40 * 0.9)
    p = tb.plan(used=used, limit=TIB40, day_start_used=used,
                today=date(2026, 8, 8))
    assert p.allowance == (TIB40 - used) // 24
    assert p.is_open is True  # nothing spent yet today


def test_exhausted_month_is_shut():
    """Today's live state: 44.58 TB against a 40 TiB limit. PikPak
    rejects these anyway; the gate should not waste the round trip."""
    used = 44_580_402_763_571
    p = tb.plan(used=used, limit=TIB40, day_start_used=used,
                today=date(2026, 8, 8))
    assert p.allowance == 0
    assert p.is_open is False


# ---------------------------------------------------------------------------
# the month-boundary trap
# ---------------------------------------------------------------------------

def test_counter_reset_rebaselines_instead_of_locking_the_day_shut():
    """The reset lands at ~00:00 Taipei on the 1st — the same moment the
    day stamp rolls. If the day's baseline were captured just BEFORE the
    reset (44.58 TB) and the counter then dropped to ~0, spent_today
    would go negative and the allowance would compute off a baseline
    above the limit — the gate would stay shut for all of day 1 of a
    brand-new budget. Detect the drop and re-baseline."""
    # 109.2 GB is what r364 actually read moments after the reset.
    post_reset = 109_200_000_000
    p = tb.plan(used=post_reset, limit=TIB40,
                day_start_used=44_580_402_763_571, today=date(2026, 8, 1))
    assert p.rebaselined is True
    assert p.spent_today == 0
    # The share is off what is LEFT, so the 109 GB already burned this
    # month is deducted — not a full untouched 1/31.
    assert p.allowance == (TIB40 - post_reset) // 31
    assert p.is_open is True


def test_no_rebaseline_when_the_counter_merely_holds_still():
    p = tb.plan(used=5_000, limit=TIB40, day_start_used=5_000,
                today=date(2026, 8, 8))
    assert p.rebaselined is False


# ---------------------------------------------------------------------------
# fail open, never fail closed
# ---------------------------------------------------------------------------

def test_unreadable_limit_fails_open():
    """A quota read that returns junk must not stop the pipeline. PikPak
    itself hard-rejects at 100%, so it remains the backstop; failing
    closed here would turn a transient API blip into a full outage."""
    for limit in (0, -1):
        p = tb.plan(used=1, limit=limit, day_start_used=0,
                    today=date(2026, 8, 8))
        assert p.is_open is True
        assert p.degraded is True


def test_healthy_read_is_not_degraded():
    p = tb.plan(used=1, limit=TIB40, day_start_used=0, today=date(2026, 8, 8))
    assert p.degraded is False


# ---------------------------------------------------------------------------
# the gate, through the real queue path
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.schemas import SendAllOptions  # noqa: E402
from app.services import download_queue as dq  # noqa: E402

_MAGNET = "magnet:?xt=urn:btih:D967088166B41DEBE83A89606F3D2AAFF2B5D6AB&dn=t"


def _queue_env(monkeypatch, submitted, fetched):
    monkeypatch.setattr(dq, "_sent_hashes_cache", set())
    monkeypatch.setattr(dq, "_inflight_hashes", set())

    async def fetch(code):
        fetched.append(code)
        await asyncio.sleep(0)
        return SimpleNamespace(magnets=[SimpleNamespace(
            link=_MAGNET, name="t", size="5.0GB", is_hd=True,
            has_subtitle=False)])

    async def submit(payload):
        submitted.append(payload.magnet)
        return SimpleNamespace(id="t1", file_id="f1", name="t",
                               phase="PENDING", message="")

    async def log(*, code, magnet, **kw):
        dq._note_sent_hash(magnet)

    monkeypatch.setattr(dq, "scraper",
                        SimpleNamespace(fetch_detail_resolved=fetch))
    monkeypatch.setattr(dq, "pick_best_magnet",
                        lambda magnets, **kw: magnets[0])
    monkeypatch.setattr(dq, "pikpak_service",
                        SimpleNamespace(offline_download=submit))
    monkeypatch.setattr(dq, "_log_offline_task", log)


async def test_shut_gate_blocks_before_spending_a_javbus_fetch(monkeypatch):
    """r494 burned a full listing scan per round only to have every
    submit rejected, logging 19 rows that could never be re-sent. The
    gate must sit ahead of the detail fetch, not after it."""
    submitted, fetched = [], []
    _queue_env(monkeypatch, submitted, fetched)

    async def shut():
        return False, "daily traffic share spent"

    monkeypatch.setattr(dq.traffic_budget, "gate_open", shut)
    r = await dq.DownloadQueue(concurrency=1)._process(
        dq.Job(code="ABC-123", options=SendAllOptions(), source="test"))

    assert r.status == "skipped_traffic_gate"
    assert "daily traffic share spent" in r.message
    assert submitted == [], "must not reach PikPak"
    assert fetched == [], "must not spend a JavBus fetch either"


async def test_open_gate_submits_normally(monkeypatch):
    submitted, fetched = [], []
    _queue_env(monkeypatch, submitted, fetched)

    async def open_gate():
        return True, "today 0.0 GB of 1418.7 GB"

    monkeypatch.setattr(dq.traffic_budget, "gate_open", open_gate)
    r = await dq.DownloadQueue(concurrency=1)._process(
        dq.Job(code="ABC-123", options=SendAllOptions(), source="test"))

    assert r.status == "sent"
    assert submitted == [_MAGNET]


async def test_force_overrides_a_shut_gate(monkeypatch):
    """A manual send is ~6.5 GB against a 1.42 TB/day share — noise. It
    must not require flipping the app_meta kill switch."""
    submitted, fetched = [], []
    _queue_env(monkeypatch, submitted, fetched)

    async def shut():
        return False, "daily traffic share spent"

    monkeypatch.setattr(dq.traffic_budget, "gate_open", shut)
    r = await dq.DownloadQueue(concurrency=1)._process(
        dq.Job(code="ABC-123", options=SendAllOptions(), source="test",
               direct_magnet=_MAGNET, force=True))

    assert r.status == "sent"
    assert submitted == [_MAGNET]


async def test_gate_failure_never_stops_the_pipeline(monkeypatch):
    """gate_open swallows everything, but pin it: a bug in the budget
    code must not be able to take downloads offline."""
    async def boom():
        raise RuntimeError("app_meta exploded")

    monkeypatch.setattr(tb, "enabled", boom)
    allowed, why = await tb.gate_open()
    assert allowed is True
    assert "allowing" in why
