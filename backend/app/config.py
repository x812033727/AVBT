from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    javbus_base_url: str = "https://www.javbus.com"
    javbus_lang: str = "zh"

    pikpak_username: str = ""
    pikpak_password: str = ""
    pikpak_download_folder: str = "AVBT"

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

    http_proxy: str = ""

    # Optional webhook fired after each successful auto-archive. Body is
    # `{"content": "..."}` which is compatible with Discord webhooks.
    webhook_url: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/avbt.db"


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
