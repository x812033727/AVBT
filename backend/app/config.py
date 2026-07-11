from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    javbus_base_url: str = "https://www.javbus.com"
    javbus_lang: str = "zh"

    pikpak_username: str = ""
    pikpak_password: str = ""
    pikpak_download_folder: str = "AVBT"

    # Per-call HTTP timeout for pCloud API requests; same role as
    # ``pikpak_api_timeout_seconds``. 0 disables.
    pcloud_api_timeout_seconds: float = 60.0

    # Cap per-call PikPak API latency so a stuck connection can't freeze
    # the legacy-sweep / archive loop indefinitely. Each PikPak round-trip
    # (folder lookup, list, move, rename, trash) is wrapped in
    # ``asyncio.wait_for``; on timeout the caller sees a ``PikPakError`` it
    # can log and move past. 60s is generous — normal calls finish in <2s.
    pikpak_api_timeout_seconds: float = 60.0

    # Where the backend tells PikPak to drop newly-submitted offline
    # tasks. Defaults to ``<pikpak_download_folder>/TASK`` so finished
    # BT noise (kfa55.com@..., 第一會所新片@... wrappers) is corralled
    # in a parking area instead of polluting the AVBT root. Sweep walks
    # just this folder, so it doesn't need a kind-dir skip list. Set
    # blank to revert to legacy behaviour (downloads land in AVBT root).
    pikpak_task_folder: str = "AVBT/TASK"
    # When True, sweep also walks the AVBT root once per cycle so
    # magnets submitted via the PikPak App/web (which bypass the
    # backend and ignore pikpak_task_folder) still get tidied. Off by
    # default — leave off if you only ever submit downloads from this
    # site.
    pikpak_sweep_fallback_root: bool = False

    # Auto-archiver: every N seconds, scan PikPak completed offline tasks
    # and move their files to <archive_folder>/<code>/.
    archive_enabled: bool = True
    # Legacy / fallback flat archive. New downloads matching a tracked
    # listing go to AVBT/<kind>/<name>/<code>/ instead; codes with no
    # tracked match still land here.
    pikpak_archive_folder: str = "AVBT/已完成"
    archive_interval_seconds: int = 60

    # Auto-sweep: in addition to the OfflineTaskLog-driven archive pass,
    # periodically scan the AVBT root for orphans dropped there outside
    # the backend (PikPak App / web manual adds, magnets handed straight
    # to PikPak, leftovers from past tools) and route them into the
    # kind/name hierarchy too. Reuses the reorganize phase-1 logic.
    archive_sweep_root_enabled: bool = True
    archive_sweep_interval_seconds: int = 300

    # Auto-promote parked codes: walk ``pikpak_archive_folder`` (the
    # fallback bucket; default ``AVBT/已完成``) and migrate codes that
    # *now* match a tracked listing into the kind/name structure. Lets
    # the user add a star/series to tracking after the file already
    # landed in the fallback bucket, and have it reclassified
    # automatically instead of needing to click "整理 PikPak 資料夾".
    archive_sweep_legacy_enabled: bool = True

    # PikPak presence index + missing-codes feature
    presence_ttl_seconds: int = 300            # cache lifetime
    missing_listing_cache_seconds: int = 3600  # JavBus listing cache
    missing_max_pages: int = 50                # safety cap when crawling a listing
    # 缺漏摘要重建時同時處理幾個 tracked listing。JavBus 端已有全域
    # 限流,平行主要是重疊 IO 等待;調太高只會增加無效等待。
    missing_rebuild_concurrency: int = 4

    # Per-kind hierarchy paths. When empty, derived as
    # ``<pikpak_download_folder>/<kind>``. Set explicitly when your
    # PikPak layout differs (e.g. nested under a Chinese label or
    # custom parent path):
    #   PIKPAK_SERIES_FOLDER=AVBT/AVBT/系列/系列
    pikpak_series_folder: str = ""
    pikpak_star_folder: str = ""
    pikpak_studio_folder: str = ""
    pikpak_label_folder: str = ""
    pikpak_director_folder: str = ""
    pikpak_genre_folder: str = ""

    # Listing tracker: every N seconds, check JavBus for new works of
    # every TrackedListing row.
    tracker_enabled: bool = True
    tracker_interval_seconds: int = 3600  # 1 hour
    tracker_auto_send_hd_only: bool = True
    tracker_auto_send_skip_sent: bool = True
    # Soft cap: prefer magnets ≤ this many MB, but if every candidate
    # exceeds it, fall back to picking from the oversized ones anyway.
    # 0 disables the preference.
    tracker_auto_send_max_size_mb: float = 10240
    # 缺漏自動補檔:auto_send 全掃時把「歷史缺漏」也送進下載佇列。
    # 關閉後仍會重算缺漏數(看板照常更新),只是不送件。執行期可在
    # 設定頁切換(切換值不落地,重啟回到這裡的預設)。
    tracker_backfill_enabled: bool = True
    # 每次全掃最多送出的缺漏數(page-1 新作不受限)。超出的部分留給
    # 下一輪 tick 重掃時繼續補,避免一個大目錄瞬間灌爆佇列。<=0 不設限。
    tracker_backfill_batch_limit: int = 100

    # Global download queue: every PikPak offline submission (tracker
    # auto-send, bulk send-all, single magnet) routes through one queue
    # with a fixed worker pool. Higher = faster but more likely to trip
    # PikPak's rate limits. 5 is a safe default; bump to 8 for bursty
    # backlog catch-up.
    download_queue_concurrency: int = 5

    # Tracker per-tick concurrency. JavBus outbound HTTP is already
    # serialised by the 1.2 s global throttle in scrapers/javbus.py, so
    # this only bounds DB sessions + CPU-side parsing — tune up to keep
    # SQLite from contention, down if you see lock-busy spam.
    tracker_check_concurrency: int = 8

    # Adaptive full-catalog scan: skip the per-listing JavBus walk in
    # _enqueue_auto_send when the listing has been quiet for this many
    # consecutive tracker ticks. Forces a full scan every
    # ``tracker_quiet_skip_every`` ticks regardless so a backfilled
    # earlier code can't slip past forever.
    tracker_quiet_skip_threshold: int = 6
    tracker_quiet_skip_every: int = 12

    # Auto-prune offline_task_log: rows where archived=True and
    # archived_at < cutoff get deleted by services/log_cleanup.run_loop.
    # 0 disables pruning entirely.
    offline_log_retention_days: int = 90

    http_proxy: str = ""

    # JavBus shared client + adaptive rate limiter. The old global 1.2 s
    # serial throttle was a hard ceiling that defeated every downstream
    # concurrency knob. The limiter now caps in-flight to
    # ``javbus_concurrency`` requests with a per-request minimum spacing
    # that widens on 429 and gently recovers toward the base on success.
    javbus_concurrency: int = 5
    javbus_min_interval: float = 0.35
    javbus_429_penalty: float = 2.5
    javbus_429_recovery: float = 0.92
    # Wrapper timeout for one attempt of ``fetch_detail`` in pCloud
    # organize. The scraper itself does 4× 429-retries × 30s requests
    # (~150s worst case), so any wrapper limit shorter than that cuts
    # the scraper's retries short. 90s covers "first request + one
    # 429 retry" comfortably. Organize ALSO retries the whole attempt
    # one extra time per code (inside organize_folder_stream), so total
    # budget per slow code is ~2× this value.
    pcloud_organize_javbus_timeout: float = 90.0
    # httpx pool capacity (HTTP/2 multiplexes within each connection).
    javbus_pool_size: int = 20
    javbus_http2: bool = True

    # fetch_detail in-memory cache. Same code requested by tracker, bulk
    # send and manual movie page collapses to a single fetch within TTL.
    # Set to 0 to disable caching entirely (every call hits JavBus).
    javbus_detail_cache_ttl_seconds: int = 1800
    javbus_detail_cache_max: int = 2000

    # Parallel page-walk batch size for listing scans (missing detection
    # and bulk send-all). Set to 1 to restore strict sequential walks.
    javbus_page_batch_size: int = 3

    # Optional webhook fired after each successful auto-archive. Body is
    # `{"content": "..."}` which is compatible with Discord webhooks.
    webhook_url: str = ""

    # ----- Telegram 通知(選用) -----
    # 兩者皆設定時,通知會同時發到 webhook 與 Telegram。
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # 各事件的預設開關(可在設定頁調整,調整值存 DB 並優先於這裡)。
    notify_tracked_new: bool = True
    notify_archive_done: bool = True
    notify_archive_failed: bool = True
    # PikPak 不穩時失敗通知可能很吵,預設關閉。
    notify_download_failed: bool = False
    # 爬蟲哨兵:偵測到 JavBus 疑似改版/封鎖時的告警(每類每小時最多一次)。
    notify_scraper_alert: bool = True
    # 自動備份失敗(靜默失敗的備份比沒備份更危險)。
    notify_backup_failed: bool = True
    # 定期重複掃描發現重複番號時。
    notify_duplicates_found: bool = True
    # pCloud 傳輸:單檔完成可能很吵(整資料夾遞迴=一檔一則),預設關;
    # 用盡重試仍失敗的告警預設開。
    notify_transfer_done: bool = False
    notify_transfer_failed: bool = True

    # 允許的前端來源(CORS),逗號分隔。含 "*" 時視為全開(此時
    # 瀏覽器規範強制 allow_credentials=False)。
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # 圖片代理白名單:逗號分隔的域名後綴(".javbus.com" 匹配所有子網
    # 域;寫完整域名則精確匹配)。留空時由 javbus_base_url 派生並附上
    # 常見 JavBus 圖片 CDN。代理是無認證端點,白名單 + DNS 私有位址
    # 檢查防止它被當開放代理 / SSRF 跳板。
    img_proxy_allowed_hosts: str = ""

    # 圖片磁碟快取:代理抓回的圖存到 data/img_cache(compose volume 內,
    # 重建映像不掉),同一張圖第二次起直接從磁碟回,不再打上游 CDN。
    # 超過上限時依 mtime(LRU)淘汰最舊的直到降回上限的 90%。
    img_cache_enabled: bool = True
    img_cache_dir: str = "./data/img_cache"
    img_cache_max_gb: float = 2.0
    img_cache_evict_interval_seconds: int = 300

    # ----- 自動資料庫備份 -----
    # 每隔 N 小時把 SQLite 用 online-backup API 複製到
    # data/backups/avbt-<timestamp>.db,保留最新 keep 份。
    auto_backup_enabled: bool = True
    auto_backup_interval_hours: int = 24
    auto_backup_keep: int = 7

    # ----- 定期重複掃描(PikPak↔pCloud)-----
    # 預設關(要走訪兩邊雲端樹,屬重操作):開啟後每 N 小時做一次
    # 唯讀掃描,發現重複番號就發 duplicates_found 通知,絕不自動刪。
    duplicates_scan_enabled: bool = False
    duplicates_scan_interval_hours: int = 168  # 一週
    # 掃描根:PikPak 空字串=雲端根;pCloud "0"=根。
    duplicates_scan_pikpak_folder: str = ""
    duplicates_scan_pcloud_folder: str = "0"

    database_url: str = "sqlite+aiosqlite:///./data/avbt.db"

    # Root logger level (see logging_setup.py). DEBUG/INFO/WARNING/ERROR.
    log_level: str = "INFO"

    # ----- 登入門禁 -----
    # 用來簽 JWT 的密鑰。留空時啟動會自動產生一組並寫入
    # data/auth_secret.txt(沿用 PikPak token 的持久化模式),讓 token
    # 在重啟後仍有效。多台部署共用同一把密鑰時可在此明確指定。
    auth_secret: str = ""
    # 登入 token 的有效期(小時)。預設 30 天。
    auth_token_ttl_hours: int = 720

    # ----- pCloud: PikPak → pCloud 遠端傳輸 -----
    # 帳密 / token 任一即可。token 優先;若無 token 但有帳密,啟動時
    # 自動換 token 並寫入 data/pcloud_token.txt。
    pcloud_username: str = ""
    pcloud_password: str = ""
    pcloud_access_token: str = ""
    # us = api.pcloud.com (美國) / eu = eapi.pcloud.com (歐洲) /
    # auto = 先試 us,失敗 fallback eu
    pcloud_region: str = "auto"
    # 預設要把檔案丟到 pCloud 的哪個資料夾路徑(以 / 起頭)。空字串 = 根目錄
    pcloud_default_folder: str = "/From PikPak"
    # 後台 transfer worker 同時處理幾個 job。pCloud savefilefromurl 是
    # 非同步的(它自己拉檔),所以這只控制併發送出的速度,不影響頻寬
    pcloud_transfer_concurrency: int = 3
    # 輪詢一個 pCloud 上傳任務狀態的間隔(秒)
    pcloud_poll_interval_seconds: int = 15
    # 暫時性失敗(PikPak 連結逾時、pCloud 限流/下載失敗)自動重試:
    # 總嘗試次數上限(含首次)。1 = 關閉自動重試。
    pcloud_transfer_max_attempts: int = 3
    # 重試退避基數(秒),第 n 次失敗後等 base * 2^(n-1)。
    pcloud_transfer_retry_base_seconds: int = 60


settings = Settings()


def cors_origin_list() -> list[str]:
    """Parsed ``cors_origins``. ``*`` anywhere collapses to wildcard."""
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if "*" in origins:
        return ["*"]
    return origins


# Image CDN suffixes JavBus covers/avatars are actually served from.
# Suffix entries (leading dot) match any subdomain; bare entries match
# the exact host.
_IMG_PROXY_DEFAULT_SUFFIXES = (
    ".javbus.com",
    ".javbus22.com",
    ".dmm.co.jp",
    ".dmm.com",
    ".buscdn.art",
    ".buscdn.cloud",
)


def img_proxy_allowed_hosts() -> tuple[str, ...]:
    """Host allowlist for the image proxy.

    ``IMG_PROXY_ALLOWED_HOSTS`` entries are ADDED to the defaults (the
    default list must keep working or every thumbnail breaks)."""
    from urllib.parse import urlparse

    hosts: list[str] = list(_IMG_PROXY_DEFAULT_SUFFIXES)
    base_host = (urlparse(settings.javbus_base_url).hostname or "").lower()
    if base_host:
        hosts.append(base_host)
    for entry in settings.img_proxy_allowed_hosts.split(","):
        entry = entry.strip().lower()
        if entry:
            hosts.append(entry)
    return tuple(dict.fromkeys(hosts))


_TRACKED_KINDS = ("star", "series", "studio", "label", "director", "genre")


def kind_base_path(kind: str) -> str:
    """Where ``<root>/<kind_label>/<name>/`` lives in PikPak for a given
    kind.

    Default: ``<pikpak_download_folder>/<chinese_label>`` (matches the
    archiver's natural-language layout, e.g. ``AVBT/系列/回胴錄``).
    Override per kind with env vars when your layout is custom — e.g.
    ``PIKPAK_SERIES_FOLDER=AVBT/AVBT/系列/系列``."""
    explicit = getattr(settings, f"pikpak_{kind}_folder", "") or ""
    explicit = explicit.strip().strip("/")
    if explicit:
        return explicit
    # Import locally to avoid a circular import (jav_code is stdlib-only
    # at import time but services depend on config).
    from .services.jav_code import KIND_LABELS_CH
    label = KIND_LABELS_CH.get(kind, kind)
    download = (settings.pikpak_download_folder or "AVBT").strip().strip("/")
    return f"{download}/{label}"


def all_kind_paths() -> list[tuple[str, str]]:
    """[(kind, path), ...] for every tracked kind."""
    return [(k, kind_base_path(k)) for k in _TRACKED_KINDS]


def task_folder_path() -> str:
    """Where new offline-download tasks land. Falls back to the legacy
    download folder when ``pikpak_task_folder`` is blank (back-compat
    for installs that downloaded directly into AVBT root)."""
    explicit = (settings.pikpak_task_folder or "").strip().strip("/")
    if explicit:
        return explicit
    return (settings.pikpak_download_folder or "AVBT").strip().strip("/")
