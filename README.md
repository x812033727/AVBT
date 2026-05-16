# AVBT

JavBus 磁力擷取 + PikPak 離線下載管理站。

- **後端**: FastAPI + SQLAlchemy + httpx + BeautifulSoup
- **前端**: Next.js 14 (App Router) + TypeScript + Tailwind
- **資料庫**: SQLite（檔案存於 `backend/data/avbt.db`）

## 功能

- 依番號 / 關鍵字搜尋 JavBus
- 解析詳細頁的磁力連結（含高清 / 字幕標記、檔案大小、日期）
- 一鍵把磁力丟到 PikPak 雲端離線下載
- 本地收藏（標記已下載 / 待看）
- PikPak 雲端檔案瀏覽、離線任務狀態、刪除、取得直鏈

## 快速啟動

### 1. 後端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                # 填入 PikPak 帳密
uvicorn app.main:app --reload --port 8000
```

後端跑在 <http://localhost:8000>，OpenAPI 文件在 `/docs`。

### 2. 前端

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

前端跑在 <http://localhost:3000>。

## 設定

`backend/.env`:

```
JAVBUS_BASE_URL=https://www.javbus.com
PIKPAK_USERNAME=your@email.com
PIKPAK_PASSWORD=your_password
PIKPAK_DOWNLOAD_FOLDER=AVBT      # 可選，會自動建立
HTTP_PROXY=                       # 可選
```

`frontend/.env.local`:

```
NEXT_PUBLIC_API_BASE=http://localhost:8000
```

## 注意

- 本工具僅作為個人本機使用，不提供公開部署
- JavBus 偶有 Cloudflare 防護，必要時可在 `.env` 設定 `HTTP_PROXY`
- PikPak 使用第三方逆向 API（[PikPakAPI](https://github.com/52funny/PikPakAPI)），帳號自負風險
