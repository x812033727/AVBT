# CLAUDE.md

AVBT:個人用 JAV 收藏管理站。JavBus 抓磁力 → PikPak 離線下載 → 自動歸檔到
`AVBT/<類別>/<名稱>/<番號>/` → 可選轉存 pCloud。單人使用、單一帳號門禁。

## 架構

- `backend/`(FastAPI + async SQLAlchemy + SQLite):
  - `app/main.py` — app 組裝;lifespan 啟動背景工作(archiver、tracker、
    log_cleanup、auto_backup、download_queue、webhook_queue、pcloud_transfer_queue)
  - `app/routers/` — API 端點(javbus / pikpak / pcloud / collection /
    tracked / stats / notify / backup / compare / img / auth)
  - `app/services/` — 業務邏輯。重點:
    - `jav_code.py` — 番號解析核心(`extract_jav_code`、`detect_part_hint` 等,純函式)
    - `rename_plan.py` — 影片改名計畫(PikPak sweep / pCloud organize / episode_finder 共用)
    - `pikpak.py` / `pcloud.py` — 雲端客戶端;pCloud 的 organize/cleanup 在
      `pcloud_organize.py`(mixin)、錯誤型別在 `pcloud_errors.py`,皆從
      `pcloud.py` re-export
    - `download_queue.py` — 所有 PikPak 離線提交的單一入口(去重、限流)
    - `pikpak_presence.py` — 番號 → PikPak 路徑索引(missing / video_count 用)
    - `video_count.py` — 「這部幾集?」實際檔案數查詢
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
- **例外處理**:背景迴圈要 log 再吞(`except Exception` 需 `# noqa: BLE001`
  註記);ruff 開 BLE 規則。

## 測試

- `backend/tests/`,pytest + pytest-asyncio(`asyncio_mode=auto`)。
- 優先純邏輯測試(番號解析、magnet 挑選、計數邏輯);需要 DB 的用
  tmp sqlite + monkeypatch `app.database.engine`;雲端服務用
  SimpleNamespace 假物件 monkeypatch(見 `test_video_count.py`)。
- conftest 已把 `DATABASE_URL` 設為 in-memory,測試不會碰真實 DB。
