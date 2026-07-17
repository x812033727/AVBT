# orphan 死列死信(dead-letter)機制 — 設計

日期:2026-07-17
狀態:已通過設計評審,待寫實作計畫

## 問題

`_finalize_retry_pass` 每輪反覆挑中一批 `OfflineTaskLog` orphan 死列(`archived=False`、7 天 reap window 內),對每列跑 `run_finalize`,永遠找不到歸檔夾 → 記一條 `finalize CODE: 找不到 CODE 的歸檔資料夾(path)` WARNING(finalize.py:398),並燒掉 resolve-path + lookup + presence-refresh 幾個 PikPak 呼叫。每列在 7 天 window 內每 ~10 分重試一次才過期。

實測規模(2026-07-17):**674 列 / 627 個不同 code** 落在窗內,其中 **577 列 `file_id` 為空**(下載從未產出檔案);抽樣的 code 全部不在 presence 歸檔索引(已排除「已歸檔漏蓋章」的 false-negative)。噪音 388 條/小時 + 每輪對數百死 code 的無效 PikPak 呼叫。

## 根因

`_reap_orphan_rows`(archiver.py:1195)刻意只關「task 已離開 active list **且** code 已 flattened 在目的地」的列(stamp `finalized=True`)。對「什麼都沒 landed」(NOT flattened)的死列,它在 line 1291 `if not await _already_flattened(row.code): continue` **跳過、留著等晚到的下載**——於是這些列在 7 天內持續被 `_finalize_retry_pass` 空轉。這是設計使然,但對「下載從未產出檔案」的列而言是純浪費。

## 目標

給明確死掉的 orphan 列加**死信標記**,讓它們停止被重試,消滅 WARNING 噪音並省下每輪的無效 PikPak 呼叫。對齊管線效能主題(省資源、少限流)。純 DB bookkeeping,零檔案操作,零資料風險。

## 範圍(使用者決策)

- **只涵蓋 `file_id` 為空的列**(577 列)——最保守、共識最強的死信。`file_id` 有值但 `archived=0` 的 97 列**不納入**(較模糊,留待日後)。
- **寬限 24 小時**:建立超過 24h 才判定死信。

## 設計

### 1. 標記:新增 `abandoned` 欄位
`offline_task_log` 加 `abandoned BOOLEAN DEFAULT 0`,照既有輕量 migration pattern:
- `app/database.py` 的 ALTER TABLE 清單(`finalized` 在 line 146-147)append 一行:
  `"ALTER TABLE offline_task_log ADD COLUMN abandoned BOOLEAN DEFAULT 0"`
- `app/models.py` 的 `OfflineTaskLog` 加:
  `abandoned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")`
- 與 `finalized` 分離,不污染歸檔/完成率統計。DB 為 SQLite,`DEFAULT 0` / `server_default="0"` 無 PG 疑慮。

### 2. 放棄判定:擴充 reaper 的 not-flattened 分支
在 `_reap_orphan_rows` 候選迴圈,line 1291 現況:
```python
if not await _already_flattened(row.code):
    continue  # nothing landed (or needs real finalize)
```
改為:not flattened 時,先判斷是否為明確死列,是則標記 abandoned,否則維持原本 continue:
```python
if not await _already_flattened(row.code):
    # Genuinely-dead orphan: task gone (checked above), the download
    # never produced a file (file_id empty), it isn't at the destination,
    # and it's old enough that a late arrival is implausible. Dead-letter
    # it so the finalize retry pass stops re-listing it every ~10 min for
    # the rest of the 7-day window. Pure DB flag; no file operation.
    if (
        not row.archived
        and not (row.file_id or "")
        and row.created_at < datetime.utcnow() - _ABANDON_GRACE
    ):
        row.abandoned = True
        row.message = "abandoned: task gone, no file landed"
        abandoned += 1
        logger.info(
            "orphan reap abandoned %s (task %s gone, no file landed, "
            ">%dh old)",
            row.code, row.task_id or "?",
            int(_ABANDON_GRACE.total_seconds() // 3600),
        )
    continue
```
- `task 已離開 active` 由迴圈上方 line 1281 的 `if row.task_id and row.task_id in active: continue` 保證。
- `_already_flattened(row.code)` 為 False 是**防誤殺閘門**:已 landed(present/flattened)的列走既有 stamp `finalized=True` 分支,絕不會被 abandon。
- 只 abandon `not row.archived`(排除 reaper 選取的第二路徑 `archived=True` 列)。
- 新常數 `_ABANDON_GRACE = timedelta(hours=24)`(archiver.py,放在其他 window 常數旁)。
- reaper 回傳值/commit:沿用既有 `if done:` commit;需確保 `abandoned` 的變更也被 commit(見計畫:把 abandon 計入觸發 commit 的條件,或併入既有 done 的 commit 條件)。

### 3. 兩個 pass 都排除 abandoned
- `_finalize_retry_pass`(archiver.py:1063-1095 的 select)WHERE 加 `OfflineTaskLog.abandoned.is_(False)`。
- `_reap_orphan_rows`(archiver.py:1237-1266 的 select)WHERE 加 `OfflineTaskLog.abandoned.is_(False)`。
- 死信一標,retry pass 不再選它 → WARNING 停、每輪的 resolve/lookup/presence 呼叫停;reaper 也不再重複檢查它。

### 4. 既有 577 列的消化
部署後由 reaper 在後續多個 tick 內自然標記(受 `_REAP_CHECK_LIMIT` 每輪上限節流,每列一次性 flattened 檢查後永久排除)。取代現在每 10 分重試、持續 7 天的無限浪費。無需手動 backfill。

## 安全性

- 純 DB flag,零檔案操作。
- 只放棄「task 已結束 + 無檔 landed(file_id 空)+ 不在歸檔目的地 + 超過 24h」的列——四重條件。
- `_already_flattened` 閘門確保絕不放棄「已成功 landed 只是漏蓋章」的列(那類照舊 stamp finalized)。
- 若之後重送磁力,`offline_download` 會建**新的** OfflineTaskLog 列照常處理,不受舊列 abandoned 影響。
- abandoned 純粹是「停止重試」,不刪不動任何雲端資料;誤判最壞後果=一個死 code 不再自動重試(可重送)。

## 明確不做

- 不碰 `file_id` 有值但 `archived=0` 的 97 列(留待日後評估)。
- 不縮短 `_REAP_WINDOW` / `_FINALIZE_RETRY_WINDOW`。
- 不改 `run_finalize` 的 WARNING 本身(死信後那些列不再進 retry pass,WARNING 自然消失;仍可能有零星新 orphan 在 24h 寬限內短暫出現,屬正常)。

## 測試(TDD)

單元測試(參照既有 archiver/finalize 測試的 monkeypatch 模式,mock `_active_task_ids` 與 `_already_flattened`,用記憶體 SQLite/測試 session):
- 明確死列(archived=False、file_id 空、task 不在 active、not flattened、created_at 26h 前)→ reaper 後 `abandoned=True`、`finalized=False`。
- 已 flattened 的列 → 走既有分支 `finalized=True`、`abandoned=False`(不被誤殺)。
- 未滿 24h 的死列 → 不 abandon(留著)。
- `file_id` 有值但 not flattened 的列 → 不 abandon(超出範圍)。
- `_finalize_retry_pass` 與 `_reap_orphan_rows` 的 select 排除 `abandoned=True` 的列(給一個 abandoned 列,確認不被選中)。

## 上線 / 驗證

- 部署照舊:worktree → CI 綠 → merge → pull → `docker-compose build backend`。
- 驗證信號:
  1. 單元測試全綠。
  2. 上線後 backend log 出現 `orphan reap abandoned ...` INFO,且 `找不到歸檔資料夾` WARNING 數量隨死列被消化而下降。
  3. DB 查 `abandoned=1` 的列數逐步逼近 577。
- 風險:低(DB flag、零檔案操作、四重放棄條件 + flattened 安全閘門)。

## 開放問題
無。
