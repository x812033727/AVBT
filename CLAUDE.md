# CLAUDE.md

AVBT:個人用 JAV 收藏管理站。JavBus 抓磁力 → PikPak 離線下載 → 自動歸檔到
`AVBT/<類別>/<名稱>/<番號>/` → 可選轉存 pCloud,或依製作商整批搬到多個 Google Drive 帳號。單人使用、單一帳號門禁。

## 架構

- `backend/`(FastAPI + async SQLAlchemy + SQLite):
  - `app/main.py` — app 組裝;lifespan 啟動背景工作(archiver、tracker、
    log_cleanup、auto_backup、download_queue、webhook_queue、pcloud_transfer_queue、
    gdrive_transfer_queue)
  - `app/routers/` — API 端點(javbus / pikpak / pcloud / collection /
    tracked / stats / notify / backup / compare / img / auth / gdrive)
  - `app/services/` — 業務邏輯。重點:
    - `jav_code.py` — 番號解析核心(`extract_jav_code`、`detect_part_hint` 等,純函式)
    - `rename_plan.py` — 影片改名計畫(PikPak sweep / pCloud organize / episode_finder 共用)
    - `pikpak.py` / `pcloud.py` — 雲端客戶端;pCloud 的 organize/cleanup 在
      `pcloud_organize.py`(mixin)、錯誤型別在 `pcloud_errors.py`,皆從
      `pcloud.py` re-export
    - `download_queue.py` — 所有 PikPak 離線提交的單一入口(去重、限流)
    - `pikpak_presence.py` — 番號 → PikPak 路徑索引(missing / video_count 用)
    - `video_count.py` — 「這部幾集?」實際檔案數查詢
    - `gdrive*.py` — PikPak → Google Drive 依製作商搬移:`gdrive.py` REST 客戶端
      (OAuth、resumable upload)、`gdrive_config.py` 金鑰/OAuth 用戶端/重新導向網址、
      `gdrive_accounts.py` 帳號/token、`gdrive_policy.py`
      流量預算/時段(`app_meta` 的 `gdrive:*`)、`gdrive_transfer.py` 工作與串流 worker、
      `gdrive_auto.py` 追蹤頁「存到 Drive」的定期自動搬移
    - `listing_codes.py` — 追蹤(任一類別)→ PikPak 上番號,用 detail 快取比對
      (歸檔資料夾一律是 `製作商/…`,女優/系列等沒有自己的資料夾)
  - `app/scrapers/javbus.py` — JavBus 抓取(限流、429 退避、detail 快取)
- `frontend/`(Next.js 14 App Router + TS + Tailwind):
  - `lib/api.ts` — 唯一 API client 與全部型別
  - `app/<page>/page.tsx` — 各頁;`components/` 共用元件
    (settings 頁的區塊在 `components/settings/`)

## 開發指令

```bash
# 後端(Python 3.12)
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check app tests && .venv/bin/pytest        # lint + 測試
.venv/bin/uvicorn app.main:app --reload --port 8000       # 開發伺服器

# 前端(Node 20)
cd frontend && npm install
npm run lint && npm run typecheck && npm run build
```

## 關鍵慣例與陷阱

- **DB migration**:不用 Alembic。`database.py init_db()` 的 DDL 清單 +
  `app_meta` 表的 `migrated:*` 一次性旗標。加欄位/索引 → DDL 清單;
  一次性資料回填 → `_run_once` 包裝。
- **通知**:一律走 `webhook_queue.enqueue_nowait(msg, event=...)`,事件別
  (`tracked_new`/`archive_done`/`archive_failed`/`download_failed`)決定
  開關;`notify.send_notification` 同時發 webhook + Telegram。
- **番號慣例**:`-C`/`ch` 後綴是**中文字幕**標記不是第 C 集;數字前綴
  (`259LUXU-1543`)一律剝除(JavBus 目錄不帶前綴);變體字母
  (`ABP-123A`)在 `extract_jav_code` 剝除、`extract_jav_code_full` 保留。
- **歸檔後 file_id 失效**:archiver 的扁平化清理會把 BT wrapper 資料夾
  丟垃圾桶。查已歸檔作品的檔案要用番號走 `presence_index.paths_for`,
  不能用 offline_task_log 的 file_id。
- **PikPak 提交**:一律經 `download_queue`(btih 去重、併發上限),
  不要直接呼叫 `pikpak_service.offline_download`。
- **設定**:`.env` + 重啟生效;執行期可調的開關存 `app_meta`
  (`notify:*` 等)。secrets 只放 env,不進 DB / API 回應。
- **Google Drive**:Drive 沒有遠端抓 URL 的 API,檔案一律 PikPak → 後端 → Drive
  串流(吃主機流量)。OAuth 用戶端(可多組,`gdrive_oauth_client`)與帳號 refresh
  token 是例外進 DB 的 secret,一律用 Fernet 加密;金鑰來自 env `GDRIVE_TOKEN_KEY`
  或 `data/gdrive_token_key.txt`(後台產生),**不進 DB**,API 也不回傳密鑰。
  設定管理在 `gdrive_config.py`;每個帳號綁定發 token 的那組用戶端。搬走的作品看 `gdrive_location`(番號 → Drive 檔案),
  presence index 已看不到它們。
- **例外處理**:背景迴圈要 log 再吞(`except Exception` 需 `# noqa: BLE001`
  註記);ruff 開 BLE 規則。

## 測試

- `backend/tests/`,pytest + pytest-asyncio(`asyncio_mode=auto`)。
- 優先純邏輯測試(番號解析、magnet 挑選、計數邏輯);需要 DB 的用
  tmp sqlite + monkeypatch `app.database.engine`;雲端服務用
  SimpleNamespace 假物件 monkeypatch(見 `test_video_count.py`)。
- conftest 已把 `DATABASE_URL` 設為 in-memory,測試不會碰真實 DB。
