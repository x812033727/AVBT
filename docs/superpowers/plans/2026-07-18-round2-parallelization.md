# 管線並行化 第二輪 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 archiver 逐列 finalize 與 pCloud poll 兩個串行點改成**有界並行**,全部由 .env 旋鈕控制、**預設 = 現狀併行度(1)→ 部署零行為改變**;上線後手動慢車拉高。

**Architecture:** Task 1 把 `archive_once` 的逐列 inline finalize 改成三段式(串行 move/session → 並行 per-distinct-code finalize → 串行標 finalized+commit),並行部分抽成 `_run_finalize_batch` helper;worker 只拿原語、不碰 session。Task 2 把 `_poll_running` 的逐列 body 包進有界 semaphore + gather。兩者旋鈕預設 1 = byte-for-byte 現況。不動任何資料安全閘門。

**Tech Stack:** Python / asyncio、SQLAlchemy async、SQLite、pytest(`asyncio_mode="auto"`)。

## Global Constraints

- 工作目錄:worktree `/opt/avbt-worktrees/round2-parallelization`(分支 `perf/round2-parallelization`)。指令在 `backend/` 下。
- **旋鈕預設 = 現狀併行度 1**;`concurrency=1` 時行為必須與改動前 byte-for-byte 相同。
- **不得**改動任何資料安全閘門(MOVE_SETTLE、settle grace、建夾 `_create_lock`);不得改 `run_finalize`/`_poll_one` body 的語意(只改「誰呼叫、序列 vs 並行」)。
- **archiver worker 只接收原語**(code, target_id),**絕不**接觸 ORM row 或 `session`(單一 AsyncSession 併發不安全)。所有 row/session 變更 + commit 留在主協程。
- **對抗式修正(必納入)**:(1)每個 finalize worker 包 try/except 回 `(code, False)`,gather `return_exceptions=True` —— 一個 code 失敗/逾時**不 abort 整批**;(2)Phase C **只標實際 move 成功的列**(重複 code 重下載會誤標)。(3)pCloud semaphore 包**整個 `_poll_one` body**,非只 pCloud 呼叫。
- 測試不打真實 PikPak/pCloud/網路。pytest 用 `/opt/avbt-venv/bin/python -m pytest`(worktree 裸 python 沒裝 pikpakapi)。
- CI 跑 `ruff check app tests`,ruff `select` 含 `UP`(`from __future__ import annotations` 下勿給型別註解加引號)。
- **不做**:拉高 `download_queue_concurrency`(維持 5)、動 settle 閘門、TTL 快取。
- 每個 commit 結尾:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y
  ```

---

### Task 1: archiver 逐列 finalize → 有界並行(三段式)

**Files:**
- Modify: `app/config.py`(約 line 53 `archive_interval_seconds` 附近新增旋鈕)
- Modify: `app/services/archiver.py`(新增 `_run_finalize_batch` helper;改 `archive_once` 的 completed-row 迴圈 1597-1662 為三段式)
- Test: `tests/test_archive_finalize_concurrency.py`(新建)

**Interfaces:**
- Consumes: 既有 `run_finalize(svc, code, *, folder_id)`、`pikpak_service`、`_FINALIZE_ROW_TIMEOUT`、`settings`。
- Produces:
  - `settings.archive_finalize_concurrency: int`(預設 1)。
  - `_run_finalize_batch(targets: dict[str, str], concurrency: int) -> set[str]`:對每個 distinct code 併發跑 `run_finalize`(bounded by `concurrency`),回傳 finalize 成功的 code 集合;per-code 失敗/逾時隔離,永不拋。
  - `archive_once` 行為在 `concurrency=1` 時不變。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_archive_finalize_concurrency.py`:

```python
"""archive_once finalizes per-distinct-code concurrently (bounded), keeping
all moves/session mutations serial. Concurrency=1 reproduces the old
serial behaviour. Per-code finalize failure is isolated; only rows that
actually moved get finalized; duplicate-code rows finalize once."""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.database as db
import app.services.archiver as arch
from app.models import OfflineTaskLog


# ---------- _run_finalize_batch (the concurrency mechanism) ----------

async def test_finalize_batch_returns_ok_codes(monkeypatch):
    async def fake_finalize(svc, code, *, folder_id=None):
        return code != "FAIL-001"  # everything ok except one

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)
    targets = {"A-1": "t1", "B-2": "t2", "FAIL-001": "t3"}
    ok = await arch._run_finalize_batch(targets, 2)
    assert ok == {"A-1", "B-2"}


async def test_finalize_batch_isolates_exceptions(monkeypatch):
    async def fake_finalize(svc, code, *, folder_id=None):
        if code == "BOOM-1":
            raise RuntimeError("pikpak blew up")
        return True

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)
    ok = await arch._run_finalize_batch({"BOOM-1": "t1", "OK-2": "t2"}, 2)
    assert ok == {"OK-2"}          # one raise never aborts the batch


async def test_finalize_batch_bounds_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def fake_finalize(svc, code, *, folder_id=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)     # yield so overlap is possible
        active -= 1
        return True

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)
    await arch._run_finalize_batch({f"C-{i}": f"t{i}" for i in range(10)}, 3)
    assert peak <= 3               # semaphore caps in-flight


async def test_finalize_batch_empty():
    assert await arch._run_finalize_batch({}, 4) == set()


# ---------- archive_once integration (dedup + over-mark + serial moves) --

async def _archive_db(tmp_path, monkeypatch, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(arch, "SessionLocal", m)
    async with m() as s:
        for r in rows:
            s.add(r)
        await s.commit()
    return engine, m


class _FakeTask:
    def __init__(self, file_id):
        self.file_id = file_id
        self.phase = "PHASE_TYPE_COMPLETE"


async def _harness(monkeypatch, m, *, move_fail=(), finalize_fail=()):
    # Neutralise the pre-loop machinery so the test targets the row loop.
    arch.state.enabled = True
    monkeypatch.setattr(arch.settings, "pikpak_username", "u")
    monkeypatch.setattr(arch, "_sweep_due", lambda: False)
    monkeypatch.setattr(arch, "_legacy_sweep_due", lambda: False)

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(arch, "_finalize_retry_pass", _noop)
    monkeypatch.setattr(arch, "_reap_orphan_rows", _noop)

    # list_tasks → every seeded file_id is COMPLETE.
    async with m() as s:
        fids = [r.file_id for r in (await s.execute(select(OfflineTaskLog))).scalars()]

    async def fake_list_tasks(size=200):
        return [_FakeTask(f) for f in fids if f]

    monkeypatch.setattr(arch.pikpak_service, "list_tasks", fake_list_tasks)

    async def fake_ad_shell(svc, fid):
        return False

    monkeypatch.setattr("app.services.finalize.wrapper_is_ad_shell", fake_ad_shell)
    monkeypatch.setattr(arch, "_resolve_archive_path",
                        lambda row: _aret(f"AVBT/S/Ser/{row.code}"))

    async def fake_folder_id(path):
        return "fid-" + path.rsplit("/", 1)[-1]

    monkeypatch.setattr(arch.pikpak_service, "folder_id", fake_folder_id)

    async def fake_move(ids, to):
        if ids and ids[0] in move_fail:
            raise RuntimeError("move failed")
        return {}

    monkeypatch.setattr(arch.pikpak_service, "move_files", fake_move)

    calls: list[str] = []

    async def fake_finalize(svc, code, *, folder_id=None):
        calls.append(code)
        return code not in finalize_fail

    monkeypatch.setattr("app.services.finalize.run_finalize", fake_finalize)

    async def _noop_refresh(codes, **k):
        return 0

    # presence_index / invalidate_result_caches are imported inside
    # archive_once from their source modules — patch them there.
    monkeypatch.setattr(
        "app.services.pikpak_presence.presence_index.refresh_codes",
        _noop_refresh,
    )
    monkeypatch.setattr("app.services.missing.invalidate_result_caches",
                        lambda: None)
    monkeypatch.setattr(arch.webhook_queue, "enqueue_nowait",
                        lambda *a, **k: None)
    return calls


def _aret(v):
    async def _c(*a, **k):
        return v
    return _c()


def _mkrow(code, fid):
    return OfflineTaskLog(
        code=code, magnet="m", btih="", task_id="t-" + fid, file_id=fid,
        name="", phase="", message="", archived=False, finalized=False,
        created_at=datetime.utcnow() - timedelta(hours=1),
    )


async def test_archive_dedups_finalize_per_code(tmp_path, monkeypatch):
    # Two rows, same code, different file_ids (a re-download) → finalize once.
    engine, m = await _archive_db(tmp_path, monkeypatch,
                                  [_mkrow("DUP-1", "f1"), _mkrow("DUP-1", "f2")])
    calls = await _harness(monkeypatch, m)
    moved = await arch.archive_once()
    assert moved == 2                       # both files moved
    assert calls.count("DUP-1") == 1        # finalize ran once for the code
    async with m() as s:
        rows = (await s.execute(select(OfflineTaskLog))).scalars().all()
        assert all(r.archived and r.finalized for r in rows)
    await engine.dispose()


async def test_archive_does_not_finalize_unmoved_row(tmp_path, monkeypatch):
    # Same code, two rows; one move fails → only the moved row is finalized.
    engine, m = await _archive_db(tmp_path, monkeypatch,
                                  [_mkrow("DUP-2", "ok"), _mkrow("DUP-2", "bad")])
    await _harness(monkeypatch, m, move_fail={"bad"})
    await arch.archive_once()
    async with m() as s:
        by_fid = {r.file_id: r for r in
                  (await s.execute(select(OfflineTaskLog))).scalars()}
        assert by_fid["ok"].archived and by_fid["ok"].finalized
        assert not by_fid["bad"].archived and not by_fid["bad"].finalized
    await engine.dispose()


async def test_archive_isolates_finalize_failure(tmp_path, monkeypatch):
    # One code's finalize fails → the other still finalizes, batch commits.
    engine, m = await _archive_db(tmp_path, monkeypatch,
                                  [_mkrow("A-9", "fa"), _mkrow("B-9", "fb")])
    await _harness(monkeypatch, m, finalize_fail={"A-9"})
    await arch.archive_once()
    async with m() as s:
        by_code = {r.code: r for r in
                   (await s.execute(select(OfflineTaskLog))).scalars()}
        assert by_code["A-9"].archived and not by_code["A-9"].finalized
        assert by_code["B-9"].archived and by_code["B-9"].finalized
    await engine.dispose()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_archive_finalize_concurrency.py -q`
Expected: FAIL —— `AttributeError: module 'app.services.archiver' has no attribute '_run_finalize_batch'`(helper 尚未存在)等。

- [ ] **Step 3: 新增旋鈕**

在 `app/config.py` 的 `archive_interval_seconds: int = 60`(約 line 53)之後新增:

```python
    # Round-2 parallelization. Distinct-code finalize concurrency inside
    # archive_once. 1 = serial (current behaviour); raise gradually only
    # after the round-1 PikPak throttle backoff is validated in prod.
    archive_finalize_concurrency: int = 1
```

- [ ] **Step 4: 新增 `_run_finalize_batch` helper**

在 `app/services/archiver.py` 的 `archive_once` 定義**之前**(例如 `_reap_orphan_rows` 之後、`_already_flattened` 附近的 helper 區)新增:

```python
async def _run_finalize_batch(
    targets: dict[str, str], concurrency: int
) -> set[str]:
    """Finalize each DISTINCT code concurrently, bounded by ``concurrency``.

    ``targets`` maps code → its archive folder_id. Returns the set of codes
    whose finalize returned truthy. A per-code failure/timeout is isolated
    (returns that code as not-finalized) and never aborts the batch —
    mirroring the old inline per-row try/except. Workers take only
    primitives (code, folder_id); ``run_finalize`` touches no DB session,
    so this composes safely with a single-threaded caller session."""
    if not targets:
        return set()
    from .finalize import run_finalize  # avoid cycle

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(code: str, target_id: str) -> tuple[str, bool]:
        async with sem:
            try:
                async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT):
                    ok = await run_finalize(
                        pikpak_service, code, folder_id=target_id
                    )
                return code, bool(ok)
            except Exception as exc:  # noqa: BLE001
                logger.warning("finalize %s failed: %s", code, exc)
                return code, False

    results = await asyncio.gather(
        *(_one(c, t) for c, t in targets.items()),
        return_exceptions=True,
    )
    return {r[0] for r in results if isinstance(r, tuple) and r[1]}
```

- [ ] **Step 5: 改 `archive_once` 迴圈為三段式**

把 `archive_once` 內 completed-row 迴圈(現況 1597-1662,`for row in rows:` 到 `if moved or shell_trashed:` 之前)改寫。**Phase A**:把 inline finalize(現況 1634-1644 的 `try: ... run_finalize ... except`)整段**移除**,改成收集 worklist;其餘(ad-shell、move、標 archived、moved_codes、notification、outer except)不變。**Phase B/C**:在迴圈**之後**、`if moved or shell_trashed:` commit 區塊**之前**插入。

Phase A —— 迴圈開頭新增兩個累加器,並把 1634-1644 的 inline finalize 區塊替換:

在 `moved_codes: list[str] = []`(1596)之後加:
```python
        finalize_targets: dict[str, str] = {}
        moved_rows_by_code: dict[str, list] = {}
```
把現況(1628-1644):
```python
                moved_codes.append(row.code)
                # Best-effort finalize: keep only canonical videos in the
                # 番號 folder, purge junk. The PikPak move above is async
                # server-side, so the wrapper may not have landed yet —
                # failure just leaves finalized=False and the bounded
                # retry pass (or the manual button) picks it up.
                try:
                    from .finalize import run_finalize  # avoid cycle

                    async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT):
                        if await run_finalize(
                            pikpak_service, row.code, folder_id=target_id
                        ):
                            row.finalized = True
                            row.finalized_at = datetime.utcnow()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("finalize %s failed: %s", row.code, exc)
```
替換為:
```python
                moved_codes.append(row.code)
                # Defer finalize to a bounded-concurrent batch after all
                # moves (Phase B). Record the target per DISTINCT code and
                # which successfully-moved rows share it, so Phase C flags
                # ONLY moved rows (a re-download's failed-move sibling must
                # not be marked finalized). run_finalize is best-effort;
                # the bounded retry pass / manual button cover misses.
                finalize_targets[row.code] = target_id
                moved_rows_by_code.setdefault(row.code, []).append(row)
```

Phase B/C —— 在迴圈結束後(現況 1660 空行處)、`if moved or shell_trashed:`(1661)**之前**插入:
```python
        # Phase B: finalize each distinct moved code concurrently (bounded).
        # Serial moves above guarantee every file has been asked to land
        # before any finalize starts; distinct codes target distinct 系列
        # folders, so their finalizes are independent and order-free.
        finalized_codes = await _run_finalize_batch(
            finalize_targets, settings.archive_finalize_concurrency
        )
        # Phase C: back on the single caller session, flag finalized ONLY on
        # rows that actually moved (archived=True) for a finalized code.
        _now = datetime.utcnow()
        for code in finalized_codes:
            for row in moved_rows_by_code.get(code, []):
                row.finalized = True
                row.finalized_at = _now
```

(注意:`import asyncio` 已在檔案頂;`settings` 已 import。Phase A 不再需要迴圈內的 `from .finalize import run_finalize`——已移到 helper。)

- [ ] **Step 6: 跑測試確認通過**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_archive_finalize_concurrency.py -q`
Expected: PASS(7 passed)。若 harness 有未 mock 到的 pre-loop 呼叫報錯,依「把每個 pre-loop 副作用呼叫 monkeypatch 成 noop/受控值」原則補上,勿改 archive_once 本體。

- [ ] **Step 7: 回歸 + lint**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_finalize.py -q && /opt/avbt-venv/bin/python -m ruff check app tests`
Expected: PASS + `All checks passed!`。

- [ ] **Step 8: Commit**

```bash
cd /opt/avbt-worktrees/round2-parallelization
git add backend/app/config.py backend/app/services/archiver.py backend/tests/test_archive_finalize_concurrency.py
git commit -m "perf: bounded-concurrent per-code finalize in archive_once

Defer inline finalize to a 3-phase pass: serial moves + session mutations,
then bounded-concurrent run_finalize per DISTINCT code (workers take only
primitives, no ORM/session), then serial finalized-flagging of moved rows
only. Gated by archive_finalize_concurrency (default 1 = current serial).
Per-code failure isolated; duplicate-code rows finalize once.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y"
```

---

### Task 2: pCloud poll → 有界並行

**Files:**
- Modify: `app/config.py`(約 line 285 `pcloud_poll_interval_seconds` 附近新增旋鈕)
- Modify: `app/services/pcloud_transfer.py`(`_poll_running` 339-416)
- Test: `tests/test_pcloud_poll_concurrency.py`(新建)

**Interfaces:**
- Consumes: 既有 `pcloud_service.upload_progress`、`SessionLocal`、`_fail_or_retry`、`pikpak_service.trash_files`、`webhook_queue`、`settings`。
- Produces:`settings.pcloud_poll_concurrency: int`(預設 1);`_poll_running` 併發 poll(bounded),行為在 `concurrency=1` 時不變。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_pcloud_poll_concurrency.py`:

```python
"""_poll_running polls running rows concurrently, bounded by
pcloud_poll_concurrency (default 1 = serial). One row's error never aborts
the pass; the whole per-row body runs inside the semaphore."""

import asyncio

import pytest

import app.services.pcloud_transfer as pt


async def _run_with(monkeypatch, rows, concurrency, progress):
    monkeypatch.setattr(pt.settings, "pcloud_poll_concurrency", concurrency)

    # Feed rows straight into _poll_running by stubbing the DB read.
    class _Result:
        def all(self_):
            return rows

    class _Sess:
        async def __aenter__(self_):
            return self_
        async def __aexit__(self_, *a):
            return False
        async def execute(self_, *a, **k):
            return _Result()
        async def commit(self_):
            return None

    monkeypatch.setattr(pt, "SessionLocal", lambda: _Sess())
    monkeypatch.setattr(pt.pcloud_service, "upload_progress", progress)
    return pt.PCloudTransferQueue()


async def test_poll_bounds_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def progress(uid):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {"status": "downloading", "downloaded": 1, "size": 2}

    rows = [(_i, _i, False, "", "") for _i in range(8)]
    svc = await _run_with(monkeypatch, rows, 3, progress)
    await svc._poll_running()
    assert peak <= 3


async def test_poll_isolates_row_error(monkeypatch):
    seen = []

    async def progress(uid):
        seen.append(uid)
        if uid == 2:
            raise RuntimeError("pcloud hiccup")
        return {"status": "downloading", "downloaded": 1, "size": 2}

    rows = [(i, i, False, "", "") for i in range(4)]
    svc = await _run_with(monkeypatch, rows, 2, progress)
    await svc._poll_running()          # must not raise
    assert set(seen) == {0, 1, 2, 3}   # every row polled despite one error


async def test_poll_serial_default(monkeypatch):
    active = 0
    peak = 0

    async def progress(uid):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {"status": "downloading", "downloaded": 1, "size": 2}

    rows = [(i, i, False, "", "") for i in range(5)]
    svc = await _run_with(monkeypatch, rows, 1, progress)  # default
    await svc._poll_running()
    assert peak == 1                   # serial
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_pcloud_poll_concurrency.py -q`
Expected: FAIL —— `AttributeError` on `settings.pcloud_poll_concurrency`,或併發峰值 > 1(尚未有 semaphore)。

- [ ] **Step 3: 新增旋鈕**

在 `app/config.py` 的 `pcloud_poll_interval_seconds: int = 15`(約 line 285)之後新增:

```python
    # Round-2 parallelization. Concurrent pCloud status polling per pass.
    # 1 = serial (current). Raise gradually; pCloud has no per-call backoff.
    pcloud_poll_concurrency: int = 1
```

- [ ] **Step 4: 改 `_poll_running` 為有界並行**

把 `_poll_running`(pcloud_transfer.py:339)的 `for rid, upload_id, ... in rows:`(358)到方法結尾(416)改寫:抓 `rows` 後(357 `if not rows: return` 之後),把逐列 body **逐字**搬進 `async def _poll_one(...)`,整個 body 包在 `async with sem:` 內,再 `gather`:

```python
        if not rows:
            return
        conc = max(1, int(settings.pcloud_poll_concurrency or 1))
        sem = asyncio.Semaphore(conc)

        async def _poll_one(rid, upload_id, delete_source, pikpak_fid, pikpak_name):
            async with sem:
                try:
                    p = await pcloud_service.upload_progress(int(upload_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("pCloud progress poll failed for %s: %s", rid, exc)
                    return
                status = p.get("status")
                if status == "downloading":
                    async with SessionLocal() as session:
                        await session.execute(
                            update(PCloudTransfer)
                            .where(PCloudTransfer.id == rid)
                            .values(
                                bytes_downloaded=int(p.get("downloaded") or 0),
                                message=f"pCloud 下載中 ({p.get('downloaded',0)}/{p.get('size',0)})",
                            )
                        )
                        await session.commit()
                elif status == "done":
                    async with SessionLocal() as session:
                        await session.execute(
                            update(PCloudTransfer)
                            .where(PCloudTransfer.id == rid)
                            .values(
                                pcloud_file_id=int(p.get("file_id") or 0),
                                status="done",
                                message="完成",
                                finished_at=datetime.utcnow(),
                                bytes_downloaded=int(
                                    (p.get("metadata") or {}).get("size") or 0
                                ),
                            )
                        )
                        await session.commit()
                    if delete_source and pikpak_fid:
                        try:
                            await pikpak_service.trash_files([pikpak_fid])
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "post-transfer PikPak trash failed for %s: %s",
                                pikpak_fid, exc,
                            )
                    webhook_queue.enqueue_nowait(
                        f"✅ pCloud 傳輸完成:{pikpak_name or rid}",
                        event="transfer_done",
                    )
                elif status == "failed":
                    await self._fail_or_retry(
                        rid, f"pCloud 下載失敗: {p.get('error') or 'unknown'}"
                    )
                elif status == "unknown":
                    await self._fail_or_retry(
                        rid, "pCloud 找不到此上傳任務(可能已逾時或被取消)"
                    )

        await asyncio.gather(
            *(_poll_one(rid, upload_id, delete_source, pikpak_fid, pikpak_name)
              for rid, upload_id, delete_source, pikpak_fid, pikpak_name in rows),
            return_exceptions=True,
        )
```

- [ ] **Step 5: 跑測試確認通過**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_pcloud_poll_concurrency.py -q`
Expected: PASS(3 passed)。

- [ ] **Step 6: 回歸 + lint**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_transfer_retry.py -q && /opt/avbt-venv/bin/python -m ruff check app tests`
Expected: PASS + `All checks passed!`。

- [ ] **Step 7: Commit**

```bash
cd /opt/avbt-worktrees/round2-parallelization
git add backend/app/config.py backend/app/services/pcloud_transfer.py backend/tests/test_pcloud_poll_concurrency.py
git commit -m "perf: bounded-concurrent pCloud status polling

_poll_running now runs each running row's full poll body inside a bounded
semaphore + gather (return_exceptions), instead of serially. Gated by
pcloud_poll_concurrency (default 1 = current serial). The whole per-row
body (progress + DB write + trash + webhook) is inside the semaphore.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y"
```

---

### Task 3: 全套件回歸 + PR

**Files:** 無

- [ ] **Step 1: 全套件**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest -q`
Expected: 全綠。

- [ ] **Step 2: diff 範圍守門**

Run: `git -C /opt/avbt-worktrees/round2-parallelization diff main --stat`
Expected:僅 `backend/app/config.py`、`backend/app/services/archiver.py`、`backend/app/services/pcloud_transfer.py` 三原始檔 + 2 新測試檔 + 2 docs。無其他檔案、無 download_queue/settle 變更。

- [ ] **Step 3: 推分支、開 PR(target main)、等 CI 綠**

依專案慣例。**PR 描述須註明:旋鈕預設 = 現狀(部署 no-op),上線後需先跑階段 0 退避驗證再手動慢車拉高**(見 spec)。部署與階段 0 驗證在合併後手動進行,不在本計畫。

---

## Self-Review

**1. Spec coverage:** 階段 1 archiver 三段式(串行 move/session + 並行 per-code finalize + 串行標 moved rows)→ Task 1;階段 2 pCloud 有界 poll(全 body 進 sem)→ Task 2;兩旋鈕預設 1 → Task 1/2 Step 3;對抗式三修正(失敗隔離 `return_exceptions`+worker try/except、只標 moved rows、sem 包全 body)→ Task 1 Step 4/5 + Task 2 Step 4,對應測試 `test_finalize_batch_isolates_exceptions`/`test_archive_does_not_finalize_unmoved_row`/`test_poll_bounds_concurrency`;不做 download/settle → Task 3 Step 2 守門;階段 0 驗證為手動 → 計畫外(spec 上線步驟)。

**2. Placeholder scan:** 無 TBD;每步附完整程式碼與預期輸出。harness 有一處明確 fallback 指示(未 mock 到的 pre-loop 呼叫補 noop)。

**3. Type consistency:** `_run_finalize_batch(targets: dict[str,str], concurrency: int) -> set[str]`、`finalize_targets`/`moved_rows_by_code`、`archive_finalize_concurrency`、`pcloud_poll_concurrency`、`_poll_one` 命名在各步驟與測試間一致。
