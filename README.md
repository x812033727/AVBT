# AVBT

JavBus 磁力擷取 + PikPak 離線下載管理站。

- **後端**: FastAPI + SQLAlchemy + httpx + BeautifulSoup
- **前端**: Next.js 14 (App Router) + TypeScript + Tailwind
- **資料庫**: SQLite（檔案存於 `backend/data/avbt.db`）

## 功能

- 依番號 / 關鍵字搜尋 JavBus，自動處理年齡驗證 gate
- 詳細頁：磁力清單（高清 / 字幕 / 大小 / 日期）、可點女優與類別標籤
- **女優頁** `/star/{id}` 與 **類別頁** `/genre/{id}` 列出所有作品
- 磁力**推薦排序**（高清 > 字幕 > 大小 > 日期）+ 批次勾選送 PikPak
- **磁力去重**：之前送過 PikPak 的磁力會標記「已送過」
- **「送這位女優全部」** / **「送這個類別全部」**：自動翻頁、挑最佳磁力、跳過已送過
- 本地收藏（待看 / 下載中 / 完成 狀態切換）
- PikPak 離線任務管理：自動刷新進度、重試失敗、刪除
- **自動歸檔**：完成的離線檔案會自動搬到 `<archive_folder>/<番號>/`
- **歸檔通知**：可選 webhook（Discord 相容）通知歸檔事件
- PikPak 雲端檔案：資料夾瀏覽、檔案搜尋、批次建立分享連結、移至垃圾桶
- pCloud 雲端管理：資料夾瀏覽、移動、改名、新增、刪除、搜尋、批次番號正規化(US / EU 自動偵測)
- **PikPak → pCloud 遠端傳輸**：選取 PikPak 檔案或整個資料夾,直接讓 pCloud
  從 PikPak 的 CDN 拉檔(伺服器 / 本機完全不耗頻寬),支援:
  - 單檔 / 多檔批次傳輸
  - 整個資料夾遞迴傳輸,可選保留子目錄結構
  - 傳輸佇列、進度追蹤、失敗重試、取消
  - 傳完自動把 PikPak 原檔移到垃圾桶(可選)
- **儀表板統計頁** `/dashboard`:收藏 / 下載 / 歸檔率統計、近 30 天趨勢、
  女優與類別 Top 10、追蹤新作排行、PikPak / pCloud 空間使用
- **通知**:webhook(Discord 相容)+ **Telegram Bot** 雙管道;
  追蹤新作 / 歸檔完成 / 歸檔失敗 / 下載失敗四種事件可在設定頁獨立開關
- **分集資訊**:磁力名稱偵測分集標記顯示「可能分集」;影片頁與歷史頁
  可查詢雲端實際影片檔數(「多集 N」/「單一影片」,PikPak 走歸檔資料夾、
  pCloud 走轉存紀錄)
- **批次操作**:收藏 / 缺漏 / 重複 / 歷史頁皆支援多選(批次送出、刪除、
  改狀態、重新歸檔、重送磁力、跨雲刪重複檔)
- **自動資料庫備份**:SQLite online-backup 定期快照到 `data/backups/`,
  設定頁可查看狀態與立即備份
- **安全**:單一帳號登入門禁(連續失敗鎖定)、圖片代理域名白名單 +
  DNS 私有位址檢查、CORS 可配置

## 快速啟動

### Docker（一鍵）

```bash
cp backend/.env.example backend/.env   # 填 PikPak 帳密
docker compose up -d --build
```

- 前端 <http://localhost:3000>
- 後端 <http://localhost:8000>
- 資料持久化在 `backend/data/`

### 本機開發

後端：

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                # 填 PikPak 帳密
uvicorn app.main:app --reload --port 8000
```

前端：

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

## 設定

`backend/.env`:

```
JAVBUS_BASE_URL=https://www.javbus.com
PIKPAK_USERNAME=your@email.com
PIKPAK_PASSWORD=your_password
PIKPAK_DOWNLOAD_FOLDER=AVBT      # 可選，會自動建立
PCLOUD_USERNAME=                  # 可選，pCloud 管理頁用
PCLOUD_PASSWORD=                  # 可選
HTTP_PROXY=                       # 可選
```

其他常用選項(完整清單見 `backend/.env.example`):

```
WEBHOOK_URL=                      # Discord 相容 webhook
TELEGRAM_BOT_TOKEN=               # Telegram 通知(@BotFather 申請)
TELEGRAM_CHAT_ID=                 # Telegram 目標 chat(@userinfobot 查詢)
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000  # 非 localhost 部署要改
IMG_PROXY_ALLOWED_HOSTS=          # 圖片代理額外允許的域名後綴
AUTO_BACKUP_ENABLED=true          # 自動資料庫備份
AUTO_BACKUP_INTERVAL_HOURS=24
AUTO_BACKUP_KEEP=7
```

pCloud(可選,若要用 PikPak → pCloud 傳輸):

```
PCLOUD_USERNAME=your@email.com
PCLOUD_PASSWORD=your_password
# 或直接給 token 跳過帳密
PCLOUD_ACCESS_TOKEN=
PCLOUD_REGION=auto           # us / eu / auto
PCLOUD_DEFAULT_FOLDER=/From PikPak
```

也可以不填 .env,進入 `/pcloud` 頁面用帳密或 access token 線上登入。

`frontend/.env.local`:

```
NEXT_PUBLIC_API_BASE=http://localhost:8000
```

## 登入

本站採**單一帳號門禁**保護整個網站（沿用個人單人使用的定位，沒有註冊、沒有多使用者）：

- **首次開站**：開啟前端會被導向 `/setup`，設定一組管理員帳號與密碼（密碼以 PBKDF2-SHA256 雜湊後存進 SQLite，不存明文）。
- **之後登入**：在 `/login` 輸入帳密，前端取得 JWT 存於 `localStorage`，後續所有 API 請求自動帶 `Authorization: Bearer` header。右上角「登出」可清除登入狀態。
- **修改密碼**：在「設定」頁的「登入密碼」區塊修改。
- **保護範圍**：除了影像代理 `/api/img/proxy`（瀏覽器 `<img>` 無法帶 header）與健康檢查 `/api/health` 外，所有 `/api/*` 都需登入。

相關環境變數（皆可選，見 `backend/.env.example`）：

```
AUTH_SECRET=             # 簽 JWT 的密鑰；留空自動產生並寫入 data/auth_secret.txt
AUTH_TOKEN_TTL_HOURS=720 # token 有效期（小時），預設 30 天
```

> 忘記密碼時,建立哨兵檔後重啟即可重設:`touch backend/data/reset_password && docker compose restart backend`,啟動時會清除管理員帳號並回到 `/setup` 重新設定(哨兵檔會自動刪除)。

## 開發

```bash
# 後端測試與 lint
cd backend
pip install -r requirements-dev.txt
ruff check app tests && pytest

# 前端 lint / 型別檢查 / 建置
cd frontend
npm run lint && npm run typecheck && npm run build
```

push / PR 會觸發 GitHub Actions CI(`.github/workflows/ci.yml`)跑上述全部檢查。
專案結構與開發慣例見 `CLAUDE.md`。

## 注意

- 本工具僅作為個人本機使用，不提供公開部署
- **JavBus 年齡驗證 / 地區阻擋**：部分 IP（特別是非亞洲機房）會被強制顯示
  年齡驗證頁，即使送出表單也無法通過。後端會在這種情況回 HTTP 451，
  並顯示「請在 .env 設定 HTTP_PROXY 或改用鏡像站」。常見解法：
  - 改用本地（亞洲家用 IP）執行
  - 設定 `HTTP_PROXY=http://...` 走代理
  - 把 `JAVBUS_BASE_URL` 改為可用鏡像（例如 `https://www.busjav.work`）
- PikPak 使用第三方逆向 API（[PikPakAPI](https://github.com/52funny/PikPakAPI)），帳號自負風險
