# orphan 死列死信機制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 給 `OfflineTaskLog` 加 `abandoned` 旗標,讓 reaper 把「下載從未產出檔案、task 已消失、不在歸檔、超過 24h」的死列標記放棄,使 `_finalize_retry_pass` 停止每 ~10 分重試它們——消滅 `找不到歸檔資料夾` WARNING 噪音與其無效 PikPak 呼叫。

**Architecture:** 純 DB bookkeeping。新增 `abandoned` 欄位(照 `finalized` 的既有 ALTER TABLE + `server_default` pattern)。在 `_reap_orphan_rows` 既有的 not-flattened 分支加死信判定(`_already_flattened` 為 False 是防誤殺已 landed 列的安全閘門)。`_finalize_retry_pass` 與 `_reap_orphan_rows` 的 select 都排除 `abandoned=True`。零檔案操作。

**Tech Stack:** Python / FastAPI、SQLite(`sqlite+aiosqlite`)、SQLAlchemy async、pytest(`asyncio_mode="auto"`)。

## Global Constraints

- 工作目錄:worktree `/opt/avbt-worktrees/abandon-dead-orphans`(分支 `feat/abandon-dead-orphans`)。指令在 `backend/` 下執行。
- **只放棄 `file_id` 為空的列**(使用者決策);`file_id` 有值的列不納入。
- **寬限 24 小時**:`created_at < now − _ABANDON_GRACE`,`_ABANDON_GRACE = timedelta(hours=24)`。
- 死信判定**必須**在 `_already_flattened(code)` 回 False 之後才做(防誤殺已 landed 只是漏蓋章的列)。
- 純 DB flag,**零檔案操作**;不刪不動任何 PikPak/雲端資料。
- 既有 `finalized` 語意不變;abandoned 與 finalized 分離。
- 測試不得打真實 PikPak/網路:monkeypatch `_active_task_ids`、`_already_flattened`、`archiver.SessionLocal`。
- pytest 在 worktree 用 `/opt/avbt-venv/bin/python -m pytest`(裸 python 沒裝 pikpakapi)。
- CI 跑 `ruff check app tests`,ruff `select` 含 `UP`(`from __future__ import annotations` 下勿給註解加引號)。
- 每個 commit 訊息結尾加:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y
  ```

---

### Task 1: `abandoned` 死信機制(欄位 + reaper 判定 + 兩 pass 排除)

**Files:**
- Modify: `app/models.py`(`OfflineTaskLog` 加 `abandoned` 欄位,約 line 178 `finalized` 之後)
- Modify: `app/database.py`(migration 清單尾端,約 line 147 之後加一行 ALTER TABLE)
- Modify: `app/services/archiver.py`(新增 `_ABANDON_GRACE` 常數;`_finalize_retry_pass` select 加排除;`_reap_orphan_rows` select 加排除 + 迴圈加死信分支 + commit 條件)
- Test: `tests/test_abandon_dead_orphans.py`(新建)

**Interfaces:**
- Consumes: 既有 `OfflineTaskLog`、`_reap_orphan_rows`、`_finalize_retry_pass`、`_active_task_ids`、`_already_flattened`、`_REAP_CHECK_LIMIT`、`SessionLocal`。
- Produces:
  - `OfflineTaskLog.abandoned: bool`(預設 False)。
  - `_ABANDON_GRACE = timedelta(hours=24)`。
  - `_reap_orphan_rows` 對符合死信條件的列設 `abandoned=True`(不設 finalized),回傳值仍是「已 stamp finalized 的數量」(behavior:abandoned 不計入回傳的 `done`,但會觸發 commit)。
  - 兩個 select 都不再選出 `abandoned=True` 的列。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_abandon_dead_orphans.py`:

```python
"""Dead-letter genuinely-dead orphan rows so the finalize retry pass
stops re-listing them every ~10 min for the 7-day reap window.

A row is abandoned only when the download never produced a file
(file_id empty), the task is gone from PikPak, the code is NOT at the
destination (not flattened — the safety gate against abandoning a
landed-but-unstamped row), and it is older than the 24h grace.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.database as db
import app.services.archiver as archiver
from app.models import OfflineTaskLog


@pytest.fixture()
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    monkeypatch.setattr(db, "engine", engine)
    await db.init_db()
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(archiver, "SessionLocal", m)
    # Reset module-global attempt maps so a row.id (restarts at 1 per fresh
    # tmp DB) from one test can't skip the reaper/retry loop in the next.
    monkeypatch.setattr(archiver, "_reap_attempts", {})
    monkeypatch.setattr(archiver, "_finalize_attempts", {})
    # No live PikPak: no active tasks; nothing is flattened by default.
    async def no_active():
        return set()

    async def not_flat(code):
        return False

    monkeypatch.setattr(archiver, "_active_task_ids", no_active)
    monkeypatch.setattr(archiver, "_already_flattened", not_flat)
    yield m
    await engine.dispose()


def _row(**kw):
    base = dict(
        code="X-001", magnet="magnet:?xt=1", btih="", task_id="gone",
        file_id="", name="", phase="", message="", archived=False,
        finalized=False,
        created_at=datetime.utcnow() - timedelta(hours=26),
    )
    base.update(kw)
    return OfflineTaskLog(**base)


async def test_dead_orphan_is_abandoned(maker):
    async with maker() as s:
        s.add(_row(code="DEAD-001"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "DEAD-001")
        )).scalar_one()
        assert row.abandoned is True
        assert row.finalized is False          # abandoned, not "done"


async def test_flattened_row_is_finalized_not_abandoned(maker, monkeypatch):
    async def yes_flat(code):
        return True

    monkeypatch.setattr(archiver, "_already_flattened", yes_flat)
    async with maker() as s:
        s.add(_row(code="LAND-001"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "LAND-001")
        )).scalar_one()
        assert row.abandoned is False          # never abandon a landed row
        assert row.finalized is True           # existing stamp behaviour


async def test_fresh_row_within_grace_is_left(maker):
    async with maker() as s:
        s.add(_row(code="FRESH-001",
                   created_at=datetime.utcnow() - timedelta(hours=2)))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "FRESH-001")
        )).scalar_one()
        assert row.abandoned is False
        assert row.finalized is False


async def test_row_with_file_id_not_abandoned(maker):
    async with maker() as s:
        s.add(_row(code="HASFILE-001", file_id="f-123"))
        await s.commit()
    await archiver._reap_orphan_rows()
    async with maker() as s:
        row = (await s.execute(
            select(OfflineTaskLog).where(OfflineTaskLog.code == "HASFILE-001")
        )).scalar_one()
        assert row.abandoned is False          # out of scope (has a file)


async def test_abandoned_row_excluded_from_retry_and_reap(maker):
    async with maker() as s:
        s.add(_row(code="GONE-001", abandoned=True))
        await s.commit()
    # Neither pass should select an already-abandoned row.
    n_reap = await archiver._reap_orphan_rows()
    n_retry = await archiver._finalize_retry_pass()
    assert n_reap == 0
    assert n_retry == 0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_abandon_dead_orphans.py -v`
Expected: FAIL —— `AttributeError`/`TypeError` on `abandoned`(欄位與判定尚未存在)。

- [ ] **Step 3: model 加 `abandoned` 欄位**

在 `app/models.py` 的 `OfflineTaskLog` 中,`finalized_at` 欄位之後加:

```python
    # Dead-letter: a genuinely-dead orphan (download never produced a
    # file, task gone, not at the destination, past the 24h grace). Set
    # by _reap_orphan_rows so the finalize retry pass stops re-listing it
    # for the rest of the 7-day reap window. Distinct from finalized so
    # archive/completion stats stay clean. server_default keeps fresh
    # create_all in lockstep with the ALTER TABLE DEFAULT 0.
    abandoned: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
```

- [ ] **Step 4: database.py migration 加欄位**

在 `app/database.py` 的 migration 清單(以 `"ALTER TABLE offline_task_log ADD COLUMN finalized_at DATETIME",` 結尾,約 line 147),在該行之後、閉合的 `)` 之前加:

```python
    "ALTER TABLE offline_task_log ADD COLUMN abandoned BOOLEAN DEFAULT 0",
```

- [ ] **Step 5: archiver.py 新增 `_ABANDON_GRACE` 常數**

在 `app/services/archiver.py` 的 `_REAP_CHECK_LIMIT = 5`(約 line 1024)之後加:

```python
# A genuinely-dead orphan (no file ever landed) is dead-lettered instead
# of re-tried for the whole reap window, but only after this grace so a
# late-arriving download still gets its chance.
_ABANDON_GRACE = timedelta(hours=24)
```

- [ ] **Step 6: `_finalize_retry_pass` select 排除 abandoned**

在 `_finalize_retry_pass`(約 line 1066)的 `.where(` 內,把:

```python
                .where(
                    OfflineTaskLog.finalized.is_(False),
                    or_(
```
改為:
```python
                .where(
                    OfflineTaskLog.finalized.is_(False),
                    OfflineTaskLog.abandoned.is_(False),
                    or_(
```

- [ ] **Step 7: `_reap_orphan_rows` select 排除 abandoned**

在 `_reap_orphan_rows`(約 line 1240)的 `.where(` 內,把:

```python
                .where(
                    OfflineTaskLog.finalized.is_(False),
                    or_(
```
改為:
```python
                .where(
                    OfflineTaskLog.finalized.is_(False),
                    OfflineTaskLog.abandoned.is_(False),
                    or_(
```

(注意:此檔案有兩處 `.where(\n ... finalized.is_(False),\n or_(`——Step 6 是 `_finalize_retry_pass`、Step 7 是 `_reap_orphan_rows`。逐一按函式確認,勿用 replace-all。)

- [ ] **Step 8: `_reap_orphan_rows` 迴圈加死信分支 + commit 條件**

在 `_reap_orphan_rows` 中,把 `checked = 0`(約 line 1279)改為同時初始化計數:

```python
        checked = 0
        abandoned = 0
```

把 not-flattened 分支(約 line 1289-1292):

```python
            try:
                async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT):
                    if not await _already_flattened(row.code):
                        continue  # nothing landed (or needs real finalize)
```
改為:
```python
            try:
                async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT):
                    if not await _already_flattened(row.code):
                        # Genuinely-dead orphan: task gone (checked above),
                        # the download never produced a file (file_id
                        # empty), it isn't at the destination, and it's
                        # older than the grace — a late arrival is
                        # implausible. Dead-letter it so the finalize retry
                        # pass stops re-listing it every ~10 min for the
                        # rest of the reap window. Pure DB flag.
                        if (
                            not row.archived
                            and not (row.file_id or "")
                            and row.created_at
                            < datetime.utcnow() - _ABANDON_GRACE
                        ):
                            row.abandoned = True
                            row.message = (
                                "abandoned: task gone, no file landed"
                            )
                            abandoned += 1
                            logger.info(
                                "orphan reap abandoned %s (task %s gone, "
                                "no file landed, >%dh old)",
                                row.code, row.task_id or "?",
                                int(_ABANDON_GRACE.total_seconds() // 3600),
                            )
                        continue  # nothing landed (or needs real finalize)
```

把結尾的 commit 條件(約 line 1320):

```python
        if done:
            await session.commit()
    return done
```
改為:
```python
        if done or abandoned:
            await session.commit()
    return done
```

- [ ] **Step 9: 跑測試確認通過**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_abandon_dead_orphans.py -v`
Expected: PASS(5 passed)。

- [ ] **Step 10: 回歸 + lint**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest tests/test_finalize.py tests/test_stats.py -q && /opt/avbt-venv/bin/python -m ruff check app tests`
Expected: PASS + `All checks passed!`。

- [ ] **Step 11: Commit**

```bash
cd /opt/avbt-worktrees/abandon-dead-orphans
git add backend/app/models.py backend/app/database.py backend/app/services/archiver.py backend/tests/test_abandon_dead_orphans.py
git commit -m "feat: dead-letter genuinely-dead orphan rows

Add an 'abandoned' flag. _reap_orphan_rows now dead-letters an orphan
row whose download never produced a file (file_id empty), whose task is
gone, that isn't at the destination (not flattened), and that is older
than a 24h grace — instead of leaving it to be re-listed by the finalize
retry pass every ~10 min for the 7-day reap window. Both passes exclude
abandoned rows, so the '找不到歸檔資料夾' warning churn and its wasted
PikPak calls stop. Pure DB bookkeeping; the flattened check is the gate
that prevents abandoning a landed-but-unstamped row.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y"
```

---

### Task 2: 全套件回歸 + 部署驗證 + PR

**Files:** 無(僅驗證與上線)

- [ ] **Step 1: 全後端套件**

Run: `cd backend && /opt/avbt-venv/bin/python -m pytest -q`
Expected: 全綠(既有無關 flaky 才允許,需逐一確認非本次造成)。

- [ ] **Step 2: diff 範圍守門**

Run: `git -C /opt/avbt-worktrees/abandon-dead-orphans diff main --stat`
Expected:僅 `backend/app/models.py`、`backend/app/database.py`、`backend/app/services/archiver.py` 三個原始檔 + 1 新測試檔 + 1 spec + 1 plan。無其他檔案、無檔案操作邏輯變更。

- [ ] **Step 3: 推分支、開 PR(target main)、等 CI 綠**

依專案慣例推送、開 PR。部署(pull main → `docker-compose build backend` → `up -d backend`)與線上驗證在合併後進行:
- backend log 出現 `orphan reap abandoned ...` INFO。
- `找不到歸檔資料夾` WARNING 數量隨死列被消化而下降。
- DB `SELECT COUNT(*) WHERE abandoned=1` 逐步逼近 577。

---

## Self-Review

**1. Spec coverage:**
- 新增 `abandoned` 欄位(migration + model + server_default)→ Task 1 Step 3-4。
- reaper not-flattened 分支死信判定(四重條件 + flattened 安全閘門)→ Task 1 Step 8。
- 兩 pass 排除 abandoned → Task 1 Step 6-7。
- `_ABANDON_GRACE=24h` → Task 1 Step 5。
- 只涵蓋空 file_id、不碰 file_id-present 的 97 列 → 由 Step 8 的 `not (row.file_id or "")` 條件 + `test_row_with_file_id_not_abandoned` 守住。
- 既有 577 列自然消化 → 由 reaper 每輪處理(受 `_REAP_CHECK_LIMIT` 節流),無需計畫額外步驟。
- 觀測 log → Step 8 的 `logger.info("orphan reap abandoned ...")`。
- spec 測試章節五案 → Step 1 五個測試對應。

**2. Placeholder scan:** 無 TBD/TODO;每個改動步驟附完整可照抄程式碼與預期輸出。

**3. Type consistency:** `abandoned`(model 欄位)/`OfflineTaskLog.abandoned.is_(False)`(兩 select)/`row.abandoned = True`(reaper)/`_ABANDON_GRACE`(常數與使用)命名在各步驟間一致;reaper 迴圈 `abandoned` 計數與 `done` 分離、commit 條件 `if done or abandoned`。
