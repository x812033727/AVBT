# 自動歸檔 / 下載管線效能強化 — 第一輪(方案 A)

日期:2026-07-17
狀態:已通過設計評審,待寫實作計畫

## 目標

改進 AVBT 自動歸檔與下載管線的效能,四個面向:吐量、延遲、少被限流/更穩、省資源。
第一輪(本 spec)採**保守、最小爆炸半徑**路線:只降低 PikPak 呼叫量、加上限流退避,
**不動並行度、不動資料安全閘門**。並行化吐量推進留待第二輪。

## 背景 / 關鍵事實

管線探查(read-only)確認一個貫穿全局的事實:

- `PikPakService._call`(`backend/app/services/pikpak.py:469`)目前**只**在 refresh token
  被別的 session 失效時自動重試(`_is_invalid_token_error`)。對 PikPak 的
  **「operation too frequent」限流完全沒有退避**——一撞限流就直接 raise 穿透。
- `_is_too_frequent_error`(`pikpak.py:127`)已存在,但目前只用於**登入**冷卻
  (`_note_login_failure`,1800s→6h),沒有用在一般操作。

這解釋了為什麼管線許多地方是**故意串行**的:在沒有 operation 級退避以前,
把 PikPak 操作並行化只會把「慢」變成「被限流然後失敗」。因此第一輪先補上退避這個基石,
其餘只做**降低呼叫量**的安全改動。

## 範圍

三個元件。全部在 `backend/`,不動前端。

### 元件 1 — `_call` 的 operation 級 too-frequent 退避(基石)

**問題**:`_call`(`pikpak.py:469-505`)對限流零退避。
**改動**:在既有 `_run(client)` 外層加一圈退避迴圈。捕捉到 `_is_too_frequent_error(exc)`
時,指數退避後重試;超過最大次數才 raise,交回上層迴圈既有的錯誤退避。

- 保留既有 invalid-token 重試邏輯**完全不變**。兩者是不同的 except 分支:
  - `_is_invalid_token_error` → 既有的 drop+relogin+重跑一次(維持原狀)。
  - `_is_too_frequent_error` → 新的指數退避重試迴圈。
  - 其他例外 → 照舊 raise。
- 退避參數(新增到 `app/config.py` `Settings`,可用 .env 覆寫):
  - `pikpak_throttle_max_retries`:預設 `4`
  - `pikpak_throttle_base_seconds`:預設 `3.0`
  - `pikpak_throttle_max_seconds`:單次退避上限,預設 `30.0`
  - 第 n 次(0-based)退避 = `min(base * 2**n, max) + jitter`,jitter 為
    `[0, base)` 的小抖動。最壞加總 ≈ 3+6+12+24 ≈ 45s,遠低於
    `_FINALIZE_ROW_TIMEOUT`=300s / `_FINALIZE_PASS_BUDGET`=900s。
- 每次退避發 `WARNING` log(含 op 描述/等待秒數/第幾次)。這是上線後的驗證信號:
  在 backend log 看到退避即代表基石生效。
- **安全前提**:PikPak「too frequent」是**執行前拒絕**(伺服器還沒動到檔案就擋回),
  故對 move/rename/trash/offline 等寫入操作而言,退避後重試不會造成重複執行副作用。
  此假設明確記錄於程式碼註解。
- jitter 的隨機來源:用 `random` 模組即可(此處無 replay/決定性需求)。

**測試**(參照 `tests/test_pikpak_login_cooldown.py` 的 `FakePikPakApi` 模式):
- monkeypatch `asyncio.sleep` 記錄呼叫、不真的等待。
- op 前 N 次丟 too-frequent 後成功 → 斷言重試了 N 次、退避秒數符合公式、最終回傳成功值。
- op 一直丟 too-frequent → 斷言退避 `max_retries` 次後 raise `PikPakError`(或原例外)。
- invalid-token 路徑不受影響(既有行為回歸)。
- 其他例外立即 raise、不進退避。

### 元件 2 — `refresh_codes` 共享夾只列一次(安全減量)

**問題**:`refresh_codes`(`pikpak_presence.py:252`)對 K 個 code 併發跑
`_live_paths_for`,每個 code 內(`pikpak_presence.py:342-349`)都會去列 legacy 歸檔夾
`AVBT/已完成`(以及自己的 製作商/<studio>/<series> 夾)。同一次呼叫內 legacy 夾被列 K 次。
`_list`(`:369`)背後的 `list_all_files` 沒有列表快取。

**改動**:在**單次 `refresh_codes` 呼叫作用域**內放一個 listing memo,
`_live_paths_for` → `_list` 依 `folder_id` 查 memo,命中就不打 PikPak。

- memo 是 **request-scoped**(這次 `refresh_codes` 用完即丟),**無 TTL、不跨呼叫**。
  一次 `refresh_codes` 本就是一個一致快照,同夾共享列表無 staleness 風險。
- **並發合併**:`refresh_codes` 用 `asyncio.gather` + `Semaphore(_REFRESH_CONCURRENCY=4)`
  同時處理多個 code,可能同時想列同一個 legacy 夾。memo 必須合併「同 key 的並發請求」
  成單一次 listing——用 `dict[str, asyncio.Future]`(或等價的 async 記憶化):
  第一個請求建立 future 並實際 listing,其餘 await 同一個 future。
  失敗時要把該 key 的失敗 future 清掉,避免把一次暫時失敗永久快取。
- 實作放在 `PikPakPresence` 內部(memo 由 `refresh_codes` 建立,透過參數傳入
  `_live_paths_for` 與 `_list`;不改 `PikPakService` 的狀態)。
- 效果:legacy 夾從每次 refresh K 次 listing 降到 1 次;同 studio/series 的多個 code
  也共用那層夾的 listing。

**測試**(參照 `tests/test_presence_refresh_endpoint.py` / `test_presence_walk.py`):
- monkeypatch `pikpak_service.lookup_folder_id` 與 `pikpak_service.list_all_files`,
  各自計數。
- 用 K 個共享 legacy 夾(且部分共享 studio/series 夾)的 code 呼叫 `refresh_codes`。
- 斷言 legacy 夾的 `list_all_files` 只被呼叫 1 次(而非 K 次),且結果正確
  (每個 code 的 paths 與非快取版一致)。
- 併發合併:兩個 code 同時要同一夾 → 只發一次 listing。

### 元件 3 — 縮範圍:request-scoped memo,放棄 stateful TTL 快取(決策已定)

原構想是在 `PikPakService` 上做「短 TTL listing 快取」跨迴圈去重(finalize retry pass /
reaper / presence 在同一個 archiver tick 內重複列同夾)。設計評審時發現兩個地雷,**決定不做**:

1. `confirm_arrivals`(`pikpak.py:870-893`)是**輪詢等檔案在目的地出現**。若
   `list_all_files` 被快取,它會一直拿到搬移前的舊列表,永遠確認不到 →
   可能誤判而刪掉來源夾(踩資料安全鐵律:唯有目的地正向現身才算搬到)。
2. 寫入操作(`move_files`/`trash_files`/`rename_file`,`pikpak.py:854-895`)吃的是
   file id 不是 parent id,一個 per-parent 快取無法乾淨地只失效受影響的 parent,
   只能退化成「任何寫入清全部」——複雜又脆。

**決定**:第一輪**不做**有狀態 TTL 服務快取。元件 3 併入元件 2 的
request-scoped memo 技術即可(哪裡在單一段程式碼內明確重複列同夾,就放一個呼叫作用域
memo)。跨迴圈的 TTL 快取**延到第二輪**再評估(且屆時需明確處理 confirm_arrivals bypass)。

實務上:元件 2 已涵蓋 `refresh_codes` 這個最大、可量測的 N+1。第一輪不額外新增跨迴圈快取。
若在寫實作計畫時發現某個**單一函式內**(如 finalize 的某段)明顯重複列同夾且改動局部,
才以同樣的 request-scoped memo 就地消除;否則不擴大範圍。

## 明確不做(留待第二輪)

- 並行化 archiver 逐列 move+finalize(`archiver.py:1507-1569`)。
- 並行化 pCloud `_poll_running`(`pcloud_transfer.py:358-363`)。
- 拉高 `download_queue_concurrency`。
- 縮短 30 分 `MOVE_SETTLE_SECONDS` 閘門 / `confirm_arrivals` 自適應退避
  (碰資料安全,單獨評估)。
- 有狀態的跨迴圈 TTL listing 快取。

以上多數需以第一輪的退避基石(元件 1)在線上驗證穩定後才安全。

## 錯誤處理

- 元件 1:退避耗盡後 raise,不吞錯——讓上層既有的迴圈級退避
  (archiver `err`-backoff、tracker backoff)接手。既有把 listing 失敗吞成 `[]` 的
  行為(`pikpak_presence.py:382-387`)維持不變。
- 元件 2:memo 內某夾 listing 失敗時,清掉該 key 的 future(不快取失敗),
  行為回退到「這次沒列到」——與現況一致(`_list` 已把例外吞成 `[]`)。

## 資料流(不變)

管線階段、順序、資料安全閘門(30 分 settle、confirm_arrivals)全部**不變**。
本輪只在既有 `_call` 加退避、在 `refresh_codes` 內去重列表,不改變任何搬移/刪除語意。

## 上線 / 驗證

- 部署路徑照舊:worktree → CI 綠 → merge → pull → `docker-compose build`。
- 驗證信號:
  1. 單元測試全綠(元件 1、元件 2)。
  2. 上線後 backend log 出現「throttle backoff」WARNING 即代表基石生效且真的在擋限流。
  3. 觀察 `refresh_codes` 相關的 PikPak listing 呼叫量下降(log / 行為)。
- 風險:低。無並行度變更、無資料安全語意變更、無 stateful 快取。

## 開放問題

無(元件 3 範圍決策已於評審拍板)。
