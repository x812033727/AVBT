# 自動歸檔/下載管線效能強化 第一輪 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 給 PikPak 呼叫加上 operation 級的「too frequent」退避重試(基石),並把 `refresh_codes` 對共享歸檔夾的 N+1 列表降到每次呼叫一次,降低限流風險、延遲與資源浪費。

**Architecture:** 兩個獨立、保守的後端改動。Task 1 在 `PikPakService._call` 的重試迴圈加一條 too-frequent 退避分支(既有 invalid-token 重試不變)。Task 2 在 `PikPakPresenceIndex.refresh_codes` 引入一個「呼叫作用域、可合併並發」的 listing memo,傳入 `_live_paths_for` / `_list`,同一次呼叫內每個 folder_id 最多列一次。不動並行度、不動任何搬移/刪除語意、不新增有狀態快取。

**Tech Stack:** Python / FastAPI backend、pytest(`asyncio_mode = "auto"`)、既有 `PikPakService` / `PikPakPresenceIndex`。

## Global Constraints

- 工作目錄:worktree `/opt/avbt-worktrees/archive-download-round1`(分支 `perf/archive-download-round1`)。所有指令在 `backend/` 下執行。
- 不得引入任何**新的並行度**或改動 PikPak 寫入(move/rename/trash/create)語意。
- 不得改動 30 分 `MOVE_SETTLE_SECONDS` 閘門或 `confirm_arrivals`。
- PikPak「too frequent」視為**執行前拒絕**(伺服器未動檔案即擋回),故退避後重試對寫入操作無重複副作用——此假設須寫進程式碼註解。
- 既有 invalid-token 重試邏輯(`_is_invalid_token_error` → `_drop_for_relogin` → 單次重跑)行為完全不變。
- 測試不得真的 sleep 或打真實 PikPak/網路;用 monkeypatch。
- 每個 commit 訊息結尾加:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y
  ```

---

### Task 1: `_call` operation 級 too-frequent 退避

**Files:**
- Modify: `app/config.py`(在 `pikpak_api_timeout_seconds`,約 line 23 之後新增 3 個設定)
- Modify: `app/services/pikpak.py`(新增 `import random`;改寫 `_call`,約 line 469-505)
- Test: `tests/test_pikpak_throttle_backoff.py`(新建)

**Interfaces:**
- Consumes: 既有 `PikPakService._ensure`、`_run`、`_drop_for_relogin`、`_is_invalid_token_error`、`_is_too_frequent_error`、`PikPakError`;`settings`。
- Produces: `PikPakService._call(op)` 行為 —— op 拋 too-frequent 時指數退避重試至多 `settings.pikpak_throttle_max_retries` 次,退避秒數 `min(base * 2**attempt, cap) + uniform(0, base)`,耗盡後 raise;invalid-token 與其他例外行為不變。新增設定:`pikpak_throttle_max_retries: int`、`pikpak_throttle_base_seconds: float`、`pikpak_throttle_max_seconds: float`。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_pikpak_throttle_backoff.py`:

```python
"""PikPak operation-level throttle backoff.

`_call` must retry an operation that hits "operation is too frequent"
with exponential backoff (a pre-execution rejection, so retrying writes
is side-effect-safe), give up after a bounded number of retries, and
leave the invalid-token relogin path and all other errors unchanged.
"""

import pytest

import app.services.pikpak as pikpak_mod
from app.services.pikpak import PikPakError, PikPakService

TOO_FREQUENT = "Aborted - Your operation is too frequent, please try again later."
INVALID_TOKEN = "invalid_grant"


@pytest.fixture()
def service(monkeypatch):
    svc = PikPakService()

    async def fake_ensure(*a, **k):
        return object()

    monkeypatch.setattr(svc, "_ensure", fake_ensure)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_throttle_max_retries", 3)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_throttle_base_seconds", 1.0)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_throttle_max_seconds", 10.0)
    monkeypatch.setattr(pikpak_mod.settings, "pikpak_api_timeout_seconds", 0)
    return svc


@pytest.fixture()
def no_sleep(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(pikpak_mod.asyncio, "sleep", fake_sleep)
    return sleeps


async def test_retries_then_succeeds(service, no_sleep):
    calls = {"n": 0}

    async def op(client):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PikPakError(TOO_FREQUENT)
        return "ok"

    assert await service._call(op) == "ok"
    assert calls["n"] == 3          # 2 throttled + 1 success
    assert len(no_sleep) == 2       # backed off twice


async def test_gives_up_after_max(service, no_sleep):
    calls = {"n": 0}

    async def op(client):
        calls["n"] += 1
        raise PikPakError(TOO_FREQUENT)

    with pytest.raises(PikPakError):
        await service._call(op)
    assert calls["n"] == 4          # initial + 3 retries
    assert len(no_sleep) == 3


async def test_non_throttle_raises_immediately(service, no_sleep):
    async def op(client):
        raise PikPakError("some other error")

    with pytest.raises(PikPakError):
        await service._call(op)
    assert no_sleep == []           # never backed off


async def test_invalid_token_relogins_once(service, no_sleep, monkeypatch):
    async def noop_drop(c):
        return None

    monkeypatch.setattr(service, "_drop_for_relogin", noop_drop)
    calls = {"n": 0}

    async def op(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PikPakError(INVALID_TOKEN)
        return "ok"

    assert await service._call(op) == "ok"
    assert calls["n"] == 2          # one relogin retry
    assert no_sleep == []           # relogin path does not throttle-backoff
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend && python -m pytest tests/test_pikpak_throttle_backoff.py -v`
Expected: FAIL —— `AttributeError` on `settings.pikpak_throttle_max_retries`(設定尚未存在),或退避行為不符。

- [ ] **Step 3: 在 `app/config.py` 新增設定**

在 `pikpak_api_timeout_seconds: float = 60.0`(約 line 23)之後,同一縮排層新增:

```python
    # Operation-level throttle backoff. When PikPak rejects a call with
    # "operation is too frequent", `_call` retries with exponential
    # backoff instead of failing straight through. 0 retries disables it.
    pikpak_throttle_max_retries: int = 4
    pikpak_throttle_base_seconds: float = 3.0
    pikpak_throttle_max_seconds: float = 30.0
```

- [ ] **Step 4: 在 `app/services/pikpak.py` 新增 `import random`**

在既有 import 區塊(line 10-18 附近)加入,維持字母排序,放在 `import os` 之後:

```python
import os
import random
import time
```

- [ ] **Step 5: 改寫 `_call`**

把現有 `_call`(約 line 469-505)整段替換為:

```python
    async def _call(self, op):
        """Run ``await op(client)`` with two independent recovery paths:

        1. Refresh token invalidated by another session
           (``_is_invalid_token_error``) → drop the cached client, re-login
           once through ``_ensure``'s lock, and re-run. Unchanged from
           before.
        2. Operation throttled ("operation is too frequent",
           ``_is_too_frequent_error``) → exponential backoff and retry, up
           to ``settings.pikpak_throttle_max_retries`` times. A "too
           frequent" is a PRE-EXECUTION rejection (the server hasn't
           touched any file), so retrying even a move/rename/trash is
           side-effect-safe. When retries are exhausted the error is
           raised so the caller's loop-level backoff (archiver/tracker)
           takes over.

        Login itself is NOT wrapped by the throttle backoff: ``_ensure``
        is called at the top of each loop iteration, outside the try, so a
        throttled login surfaces through ``_ensure``'s own exponential
        login cooldown instead of this loop.

        Each round-trip is wrapped in ``asyncio.wait_for`` with
        ``settings.pikpak_api_timeout_seconds`` so a hung connection
        surfaces as a ``PikPakError``. A timeout of 0 disables the cap."""
        timeout = float(settings.pikpak_api_timeout_seconds or 0)

        async def _run(c):
            if timeout > 0:
                try:
                    return await asyncio.wait_for(op(c), timeout=timeout)
                except TimeoutError as exc:
                    raise PikPakError(
                        f"PikPak API 逾時 ({timeout:.0f}s)"
                    ) from exc
            return await op(c)

        max_retries = max(0, int(settings.pikpak_throttle_max_retries))
        base = max(0.0, float(settings.pikpak_throttle_base_seconds))
        cap = max(0.0, float(settings.pikpak_throttle_max_seconds))

        attempt = 0
        while True:
            client = await self._ensure()
            try:
                return await _run(client)
            except Exception as exc:  # noqa: BLE001
                if _is_invalid_token_error(exc):
                    logger.warning(
                        "PikPak refresh token invalidated by another "
                        "session (%s); re-logging in", exc,
                    )
                    await self._drop_for_relogin(client)
                    client = await self._ensure()
                    return await _run(client)
                if _is_too_frequent_error(exc) and attempt < max_retries:
                    delay = min(base * (2 ** attempt), cap) + random.uniform(
                        0, base
                    )
                    logger.warning(
                        "PikPak throttled (%s); backoff %.1fs "
                        "(retry %d/%d)",
                        exc, delay, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise
```

- [ ] **Step 6: 跑測試確認通過**

Run: `cd backend && python -m pytest tests/test_pikpak_throttle_backoff.py -v`
Expected: PASS(4 passed)。

- [ ] **Step 7: 跑相關回歸(登入冷卻不受影響)**

Run: `cd backend && python -m pytest tests/test_pikpak_login_cooldown.py -v`
Expected: PASS(既有登入冷卻測試不受影響)。

- [ ] **Step 8: Commit**

```bash
cd /opt/avbt-worktrees/archive-download-round1
git add backend/app/config.py backend/app/services/pikpak.py backend/tests/test_pikpak_throttle_backoff.py
git commit -m "feat: operation-level too-frequent backoff in PikPakService._call

Retry PikPak calls that hit 'operation is too frequent' with bounded
exponential backoff instead of failing straight through. Keystone for
pipeline stability; invalid-token relogin and login cooldown unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y"
```

---

### Task 2: `refresh_codes` 呼叫作用域 listing memo

**Files:**
- Modify: `app/services/pikpak_presence.py`(新增 `_ListingMemo` class;改 `refresh_codes` line 252、`_live_paths_for` line 309、`_list` line 369)
- Test: `tests/test_presence_refresh_memo.py`(新建)

**Interfaces:**
- Consumes: 既有 `PikPakPresenceIndex._live_paths_for`、`_list`、`self._sem`、`pikpak_service.list_all_files`、`pikpak_service.lookup_folder_id`、`normalize_code`、`_LIST_MAX_ITEMS`、`_REFRESH_CONCURRENCY`;`archiver.studio_series_dir_for_code`。
- Produces:
  - `_ListingMemo(loader)`,`await memo.get(parent_id) -> list`,同 key 的並發呼叫合併成單一次 `loader(parent_id)`,失敗不快取。
  - `_live_paths_for(code, *, exclude_ids=frozenset(), memo=None)`(新增 `memo` keyword)。
  - `_list(parent_id, *, memo=None)`:`memo` 為 None 時行為與現況完全相同(其餘呼叫點 line 477/510 不受影響)。
  - `refresh_codes` 對外簽章與回傳值不變。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_presence_refresh_memo.py`:

```python
"""refresh_codes must list a shared archive folder once per call.

Each code's _live_paths_for lists the shared legacy folder AVBT/已完成
(plus its studio/series dir). Without a per-call memo, K codes trigger K
listings of the SAME legacy folder. A request-scoped, concurrency-
coalescing memo collapses that to one listing while keeping results
identical — and never caches across calls (no staleness).
"""

import asyncio
from types import SimpleNamespace

import pytest

import app.services.archiver as archiver_mod
import app.services.pikpak_presence as pp
from app.services.pikpak_presence import _ListingMemo


async def test_listing_memo_coalesces_concurrent_calls():
    calls = {"n": 0}
    started = asyncio.Event()

    async def loader(parent_id):
        calls["n"] += 1
        started.set()
        await asyncio.sleep(0)  # yield so the second caller races in
        return [f"item-of-{parent_id}"]

    memo = _ListingMemo(loader)
    a, b = await asyncio.gather(memo.get("F"), memo.get("F"))
    assert a == b == ["item-of-F"]
    assert calls["n"] == 1        # one load despite two concurrent gets


async def test_listing_memo_does_not_cache_failure():
    calls = {"n": 0}

    async def loader(parent_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return ["ok"]

    memo = _ListingMemo(loader)
    with pytest.raises(RuntimeError):
        await memo.get("F")
    assert await memo.get("F") == ["ok"]   # retried, not a cached failure
    assert calls["n"] == 2


async def test_refresh_codes_lists_shared_folder_once(monkeypatch):
    index = pp.PikPakPresenceIndex()
    index._codes = set()
    index._paths = {}

    async def noop_persist(code, paths):
        return None

    monkeypatch.setattr(index, "_persist_code", noop_persist)

    # Only the legacy folder is searched (nested studio/series dir is None).
    async def no_nested(code, *, allow_fetch=False):
        return None

    monkeypatch.setattr(archiver_mod, "studio_series_dir_for_code", no_nested)

    async def fake_lookup(path):
        return "leg" if path.strip("/") == "AVBT/已完成" else None

    list_calls = {"leg": 0}

    async def fake_list_all(parent_id, *, cap):
        list_calls[parent_id] = list_calls.get(parent_id, 0) + 1
        return [
            SimpleNamespace(id="a", name="ABC-001"),
            SimpleNamespace(id="b", name="ABC-002"),
            SimpleNamespace(id="c", name="ABC-003"),
        ], False

    monkeypatch.setattr(pp.pikpak_service, "lookup_folder_id", fake_lookup)
    monkeypatch.setattr(pp.pikpak_service, "list_all_files", fake_list_all)
    monkeypatch.setattr(pp.settings, "pikpak_archive_folder", "AVBT/已完成")

    changed = await index.refresh_codes(["ABC-001", "ABC-002", "ABC-003"])

    assert changed == 3
    assert index._paths["ABC-001"] == ["AVBT/已完成/ABC-001"]
    assert list_calls["leg"] == 1   # listed ONCE for all three codes
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd backend && python -m pytest tests/test_presence_refresh_memo.py -v`
Expected: FAIL —— `ImportError: cannot import name '_ListingMemo'`(尚未定義)。

- [ ] **Step 3: 新增 `_ListingMemo` class**

在 `pikpak_presence.py` 的 `logger = logging.getLogger(__name__)`(約 line 42)之後、常數區之前,新增:

```python
class _ListingMemo:
    """Per-call, concurrency-coalescing memo for PikPak folder listings.

    Lives only for the duration of one refresh_codes() call. Within that
    call each ``parent_id`` is listed at most once — even when several
    codes run concurrently and want the same folder, they serialise on a
    per-key lock so only the first triggers the load and the rest read
    the stored result. This is NOT a TTL cache: it is discarded when the
    call returns, so it can never serve a stale listing to a later call
    or to a change-polling caller such as confirm_arrivals. Failed loads
    are not stored, so a later caller retries."""

    def __init__(self, loader):
        self._loader = loader
        self._results: dict[str, list] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, parent_id: str) -> list:
        if parent_id in self._results:
            return self._results[parent_id]
        # setdefault is atomic (no await between get and set), so racing
        # callers for the same key share one lock.
        lock = self._locks.setdefault(parent_id, asyncio.Lock())
        async with lock:
            if parent_id in self._results:
                return self._results[parent_id]
            result = await self._loader(parent_id)
            self._results[parent_id] = result
            return result
```

- [ ] **Step 4: 讓 `refresh_codes` 建立並傳入 memo**

在 `refresh_codes`(line 252)內,找到:

```python
        sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)
        gone = frozenset(exclude_ids or ())

        async def one(code: str) -> tuple[str, list[str]] | None:
            async with sem:
                try:
                    return code, await self._live_paths_for(
                        code, exclude_ids=gone
                    )
```

改為:

```python
        sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)
        gone = frozenset(exclude_ids or ())
        memo = _ListingMemo(self._list_uncached)

        async def one(code: str) -> tuple[str, list[str]] | None:
            async with sem:
                try:
                    return code, await self._live_paths_for(
                        code, exclude_ids=gone, memo=memo
                    )
```

- [ ] **Step 5: 讓 `_live_paths_for` 接受並傳遞 memo**

把 `_live_paths_for` 簽章(line 309)由:

```python
    async def _live_paths_for(
        self, code: str, *, exclude_ids: frozenset[str] = frozenset()
    ) -> list[str]:
```

改為:

```python
    async def _live_paths_for(
        self,
        code: str,
        *,
        exclude_ids: frozenset[str] = frozenset(),
        memo: "_ListingMemo | None" = None,
    ) -> list[str]:
```

並把其中(line 349)的:

```python
            for child in await self._list(folder_id):
```

改為:

```python
            for child in await self._list(folder_id, memo=memo):
```

- [ ] **Step 6: 拆分 `_list` 成 memo 分派 + `_list_uncached`**

把現有 `_list`(line 369-387)由:

```python
    async def _list(self, parent_id: str) -> list:
        async with self._sem:
            try:
                files, partial = await pikpak_service.list_all_files(
                    parent_id=parent_id, cap=_LIST_MAX_ITEMS
                )
                if partial:
                    logger.warning(
                        "presence walk truncated at %d items under folder %s "
                        "— codes beyond the cap will look missing",
                        len(files), parent_id,
                    )
                return files
            except PikPakError as exc:
                logger.debug("list_all_files(%s) failed: %s", parent_id, exc)
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_all_files(%s) failed: %s", parent_id, exc)
                return []
```

改為(保留原邏輯到 `_list_uncached`,`_list` 變成 memo 分派、向後相容):

```python
    async def _list(
        self, parent_id: str, *, memo: "_ListingMemo | None" = None
    ) -> list:
        if memo is not None:
            return await memo.get(parent_id)
        return await self._list_uncached(parent_id)

    async def _list_uncached(self, parent_id: str) -> list:
        async with self._sem:
            try:
                files, partial = await pikpak_service.list_all_files(
                    parent_id=parent_id, cap=_LIST_MAX_ITEMS
                )
                if partial:
                    logger.warning(
                        "presence walk truncated at %d items under folder %s "
                        "— codes beyond the cap will look missing",
                        len(files), parent_id,
                    )
                return files
            except PikPakError as exc:
                logger.debug("list_all_files(%s) failed: %s", parent_id, exc)
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_all_files(%s) failed: %s", parent_id, exc)
                return []
```

- [ ] **Step 7: 跑測試確認通過**

Run: `cd backend && python -m pytest tests/test_presence_refresh_memo.py -v`
Expected: PASS(3 passed)。

- [ ] **Step 8: 跑 presence 回歸**

Run: `cd backend && python -m pytest tests/test_presence_refresh_endpoint.py tests/test_presence_walk.py tests/test_presence_persist.py tests/test_rename_presence_refresh.py -v`
Expected: PASS(既有 presence 行為不受影響;line 477/510 的無 memo 呼叫走 `_list_uncached`,行為等同現況)。

- [ ] **Step 9: Commit**

```bash
cd /opt/avbt-worktrees/archive-download-round1
git add backend/app/services/pikpak_presence.py backend/tests/test_presence_refresh_memo.py
git commit -m "perf: request-scoped listing memo in refresh_codes

Collapse the K-times-per-call re-listing of the shared legacy archive
folder to one listing per refresh_codes() call, with concurrency
coalescing. Request-scoped only — no TTL, no cross-call staleness.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012RxSJP9fL9PBjG2MuQLM3Y"
```

---

### Task 3: 全套件回歸 + 收尾

**Files:** 無(僅驗證)

- [ ] **Step 1: 跑後端全套件**

Run: `cd backend && python -m pytest -q`
Expected: 全綠(或既有的、與本次無關的 flaky 才允許,需逐一確認非本次改動造成)。

- [ ] **Step 2: 確認 diff 範圍符合 spec**

Run: `git -C /opt/avbt-worktrees/archive-download-round1 diff main --stat`
Expected: 僅 `backend/app/config.py`、`backend/app/services/pikpak.py`、`backend/app/services/pikpak_presence.py` 三個原始檔 + 兩個新測試檔 + 兩份 docs。無其他檔案、無並行度/閘門變更。

- [ ] **Step 3: 推分支、開 PR、等 CI**

依專案慣例(worktree → CI 綠 → merge → pull → docker-compose build)推送並開 PR。部署與線上驗證(觀察 backend log 是否出現 throttle backoff WARNING、refresh_codes listing 呼叫量下降)在合併部署後進行,不在本計畫內。

---

## Self-Review

**1. Spec coverage:**
- spec 元件 1(`_call` 退避 + 3 設定 + WARNING + 安全註解)→ Task 1 全覆蓋。
- spec 元件 2(refresh_codes request-scoped memo、並發合併、失敗不快取)→ Task 2 全覆蓋。
- spec 元件 3(縮範圍、放棄 TTL 快取)→ 無新增任務,符合「明確不做」;`_list` 保持向後相容確保沒有偷偷加狀態快取。
- spec「明確不做」清單(並行化/併發/settle 閘門)→ Task 3 Step 2 的 diff 範圍檢查守住。
- spec 測試章節 → Task 1/Task 2 的 TDD 步驟對應。

**2. Placeholder scan:** 無 TBD/TODO;每個改動步驟都附完整可照抄程式碼與預期輸出。

**3. Type consistency:** `_ListingMemo(loader)` / `memo.get(parent_id)` / `_live_paths_for(..., memo=...)` / `_list(parent_id, *, memo=None)` / `_list_uncached(parent_id)` 命名在 Task 2 各步驟間一致;`refresh_codes` 傳 `self._list_uncached` 當 loader,與 `_list` 分派邏輯一致。設定名 `pikpak_throttle_max_retries/base_seconds/max_seconds` 在 config、`_call`、測試間一致。
