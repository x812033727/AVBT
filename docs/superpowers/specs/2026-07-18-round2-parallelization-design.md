# 管線並行化 第二輪 — 設計

日期:2026-07-18
狀態:已通過設計評審,待寫實作計畫

## 目標

在第一輪(PikPak too-frequent 退避基石 + refresh_codes listing memo,已上線 main=c89e333)之後,把管線刻意串行的並行點改成**有界並行**以提升吞吐/延遲。核心安全原則:**validate-first + 全部做成 .env 旋鈕(預設 = 現狀,部署 no-op)+ 上線後手動微幅漸進拉高**。

## 背景 / 關鍵約束

- 管線多處**故意串行**,正因為第一輪以前 `PikPakService._call` 對限流零退避——並行化會把「慢」變「被限流失敗」。第一輪補上退避後,並行化才安全。
- ⚠️但退避基石**至今在生產尚未實際觸發過**(`pikpak throttled` log 0 次,因流量未撞限流)。因此第二輪的並行化**安全性未經線上驗證**。本設計以「先驗證退避、再拉併發」處理此風險。
- 本設計經 workflow 平行深度危害分析 + 每點對抗式 skeptic 覆核,已納入其修正。

## 整體形狀

1. **階段 0**:一次性、唯讀、不改碼的退避驗證(確認退避在 prod 真的 fire 且 recover)。
2. **階段 1 / 2**:把並行點改成有界 semaphore,旋鈕預設 = 現狀併行度(1),部署零行為改變。
3. **上線後**:先跑階段 0 驗證退避;通過後由使用者/助手手動把旋鈕從保守值微幅拉高並觀察(不在本 spec 的程式碼內)。

---

## 階段 0 — 退避驗證(手動一次性程序,非程式碼)

用**常駐** avbt-backend client 的現有唯讀端點 `GET /api/pikpak/files?parent_id=<id>&size=100`(router pikpak.py),**單線連發**直到出現第一條 `PikPak throttled … backoff` WARNING,確認**同一個呼叫退避後回 HTTP 200**(而非 500)= 驗證成功。

- **不新登入**(用常駐 client),避免踩 login 限流鐵律。
- **前置 token 有效性閘門**(對抗式修正):不信 `/status` 的 `logged_in=true`——要確認它有實際 exercise token(quota()),且 `login_cooldown_seconds==0`。
- **判準**:SUCCESS = 至少一條 backoff WARNING 且無「500 帶 too frequent」;`retry 4/4` 單獨出現**不算**失敗(第 5 次發還可能成功);EXHAUSTION = 4/4 後仍 500 帶 too frequent;NULL = 從未觸發(讀路徑此量級不受限)→ 結論即停,**不打軍備競賽硬逼**。
- **abort(任一即停,不得拉併發)**:(a) 4/4 後 500;(b) /status 出現 login_cooldown>0 或 login_block 提及 too frequent;(c) 真實使用者 browse/playback 開始報錯;(d) mid-burst 出現「refresh token invalidated by another session」。
- 挑空檔跑(避開 `43 */2` 輪值 cron、無下載/sweep 高峰);跑完留幾分鐘安靜讓 operation-throttle 窗清空。
- 這是**驗證**,不改退避本身。

---

## 階段 1 — archiver 逐列 move+finalize 並行(最高價值)

### 現況
`archive_once()`(archiver.py,約 1583-1679)開**單一** `SessionLocal` session,選出所有 completed 未歸檔列,**嚴格串行** `for row in rows:`:每列 ad-shell 檢查 → `_resolve_archive_path` → `folder_id`(建夾)→ `move_files` → 標 row.archived → `run_finalize` 包在 `asyncio.timeout(_FINALIZE_ROW_TIMEOUT=300)` 內(每列最貴、多次 listing + trash/delete)。迴圈後單次 `commit` + `refresh_codes(moved_codes)` + cache invalidate + webhook drain。`run_finalize`/`finalize_code_folder_stream` **不碰 DB session**(僅驅動 PikPak + presence_index)。

### 安全設計:三段式(旋鈕 gated)
- **Phase A(串行,主協程,共享 session)**:照現況逐列 in-order 做 ad-shell 檢查、`_resolve_archive_path`、`folder_id`(已 `_create_lock` 序列化)、`move_files`、標 `row.archived/archived_at`、`moved+=1`。**不 inline finalize**,改收集 worklist:`{distinct code: target_id}` + `code → [實際搬成功的 rows]`。
- **Phase B(並行)**:`sem = asyncio.Semaphore(N)`;`gather` 對**每個 distinct code** 一個 task,`async with sem: async with asyncio.timeout(_FINALIZE_ROW_TIMEOUT): ok = await run_finalize(pikpak_service, code, folder_id=target_id)`,回 `(code, ok)`。**worker 只拿原語 (code, target_id),絕不碰 ORM row 或 session** → 單一 AsyncSession 併發不安全的危害消失(主協程在 gather 上 idle-await)。
- **Phase C(串行,主協程)**:對 finalize 回 truthy 的 code,把該 code 底下**實際搬成功**的列標 `finalized=True/finalized_at`;然後既有單次 `commit` + `refresh_codes(moved_codes)` + invalidate + webhook drain,**不變**。

### 對抗式修正(2 個真 bug,必納入)
1. **失敗隔離**:每個 worker 把 `run_finalize` 包 `try/except` 回 `(code, False)`(mirror 現況 archiver.py:1643 的 per-row try/except),並 `gather(..., return_exceptions=True)`。一個 code 的 finalize 失敗/timeout **絕不 abort 整批** commit。
2. **不過標 finalized**:重複 code 重下載(兩列同 code 不同 file_id)——Phase C 的 `code → rows` map **只納入 Phase A 實際 move 成功(archived=True)的列**,不是所有同 code 的列。

### 不變的資料安全
- 所有 **move、row/session 變更、commit 串行**;每列 move 完成才有任何 finalize 開始(finalize 需檔案已 landed);跨 distinct code 的 finalize 目標是不同 per-code 夾、settle gate 是 per-source-id + 時間制 → order-independent。
- 建夾 `_create_lock`(pikpak.py)、MOVE_SETTLE gate(同步、不 await → 原子)、`refresh_codes` 自帶 lock、`_canonical_cache`/`_folder_cache`(await 間賦值原子且冪等)——全部維持,不需改。
- 與第一輪退避互補:semaphore 上限 = 在途 op 數上限(降低撞限流頻率),`_call` 的 per-op 退避 + jitter 吸收仍發生的限流(只序列化單一呼叫的重試,非全域鎖)→ 與 gather 乾淨組合。

### 旋鈕
`archive_finalize_concurrency: int = 1`(app/config.py)。預設 1 → Phase B 逐一 await = 現況串行 byte-for-byte。

---

## 階段 2 — pCloud poll 並行(中價值)

### 現況
`pcloud_transfer.py` 的 `_poll_running`(約 339-416)對每個 running 列**串行** poll `upload_progress`(每 ~15s),每列自己的 SessionLocal 寫、done→trash→webhook、`_fail_or_retry` 路由。

### 安全設計
抓 `rows` 後,`sem = asyncio.Semaphore(max(1, settings.pcloud_poll_concurrency or 1))`,定義 `async def _poll_one(...)` **逐字包住現況 for-loop body**,`await asyncio.gather(*(_poll_one(*r) for r in rows), return_exceptions=True)`,gather join 後才 return。

### 對抗式修正(必納入)
semaphore 要包**整個 `_poll_one` body**(upload_progress + 分支 DB 寫 + trash_files + webhook),**不只 pCloud 呼叫**——否則界外部分不受限,失去限流保護意義。

### 不變
`_poll_loop` 仍 await `_poll_running` 完成才 sleep → poll pass 不重疊、同列不併發 poll;每列自己 session + id-keyed UPDATE(不相交);per-row 例外照吞(`return_exceptions=True` mirror 現況 `continue`);`_fail_or_retry` 的 attempts/next_retry_at 與 submit loop 的 `pcloud_transfer_concurrency` 不動。pCloud 無 per-call backoff → 殘餘風險是對 pCloud 的 request-rate throttle,由旋鈕上限 + 慢車漸進吸收。

### 旋鈕
`pcloud_poll_concurrency: int = 1`。預設 1 → semaphore 恰好 1 in-flight = 現況串行 byte-for-byte。

---

## 明確不做

- **拉高下載併發**(`download_queue_concurrency`,現 5):對抗式判定現狀已合理、幾乎零改善空間、徒增 PikPak submit 限流曝險 → 移出第二輪,維持 5。
- 縮短 30 分 `MOVE_SETTLE_SECONDS` / settle 閘門(資料安全,不碰)。
- 有狀態跨迴圈 TTL listing 快取(第一輪已刻意放棄)。

## 測試(TDD)

單元測試(monkeypatch `run_finalize` / `pikpak_service` / `SessionLocal`,memory SQLite):
- **archiver 三段式**:
  - 併發 finalize 保留所有 row/session 變更在主協程、結果與串行一致(相同 archived/finalized 集合)。
  - **per-code 去重**:兩列同 code 不同 file_id → `run_finalize` 只被呼叫一次(該 code 的 target 夾)。
  - **失敗隔離**:一個 code 的 `run_finalize` 拋例外 → 其餘照常 finalized、batch commit 不 abort、該 code 不標 finalized。
  - **不過標**(對抗式#2):同 code 兩列一移成功一移失敗 → 只有 archived=True 的列被標 finalized。
  - `archive_finalize_concurrency=1` → 行為等同現況(可用呼叫序列或計數驗證)。
- **pCloud poll**:
  - `pcloud_poll_concurrency` 預設 1 → 逐一;>1 → semaphore 限制同時 in-flight 數;一列例外不 abort 其餘(`return_exceptions`)。
  - semaphore 包全 body(可用一個在 sem 內 sleep 的 fake upload_progress + 計數併發峰值驗證 ≤ N)。
- 階段 0 退避驗證是**手動程序**,無自動測試。

## 上線 / 驗證

- 部署照舊:worktree → CI 綠 → merge → pull → `docker-compose build backend`。**旋鈕預設全 = 現狀 → 部署零行為改變**。
- 上線後步驟(手動,不在程式碼):
  1. 跑階段 0 退避驗證(確認 backoff fire+recover 或 NULL)。
  2. 通過後,把 `archive_finalize_concurrency` 從 1 微幅拉到 2,observe(backoff WARNING 頻率、finalize 吞吐、無資料異常)一段時間;再視情況到 3。`pcloud_poll_concurrency` 同理。
  3. 任一異常(限流升級、資料不一致、使用者報錯)→ 旋鈕改回 1(免 redeploy,若旋鈕走 restart 則 restart)。
- 風險:低(預設 no-op、串行部分不動、資料閘門不變、對抗式修正已納入)。

## 開放問題

- 旋鈕生效方式:`.env` 改動需 restart 後端才套用(AVBT 無熱重載)。慢車漸進 = 改 .env + restart backend。此為可接受(非熱路徑頻繁調整)。
