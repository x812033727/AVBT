from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    javbus_base_url: str = "https://www.javbus.com"
    javbus_lang: str = "zh"

    pikpak_username: str = ""
    pikpak_password: str = ""
    pikpak_download_folder: str = "AVBT"

    # pCloud credentials (optional). When set, the pCloud service uses
    # these to re-login automatically if its cached auth token gets
    # invalidated; otherwise the user must log in once via /settings.
    pcloud_username: str = ""
    pcloud_password: str = ""
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

    # Actress tracker: every N seconds, check JavBus for new works of
    # every TrackedActress row.
    tracker_enabled: bool = True
    tracker_interval_seconds: int = 3600  # 1 hour
    tracker_auto_send_hd_only: bool = True
    tracker_auto_send_skip_sent: bool = True
    # Soft cap: prefer magnets ≤ this many MB, but if every candidate
    # exceeds it, fall back to picking from the oversized ones anyway.
    # 0 disables the preference.
    tracker_auto_send_max_size_mb: float = 10240

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

    database_url: str = "sqlite+aiosqlite:///./data/avbt.db"

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


settings = Settings()


_TRACKED_KINDS = ("star", "series", "studio", "label", "director")


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
