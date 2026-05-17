from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    javbus_base_url: str = "https://www.javbus.com"
    javbus_lang: str = "zh"

    pikpak_username: str = ""
    pikpak_password: str = ""
    pikpak_download_folder: str = "AVBT"

    # Auto-archiver: every N seconds, scan PikPak completed offline tasks
    # and move their files to <archive_folder>/<code>/.
    archive_enabled: bool = True
    # Legacy / fallback flat archive. New downloads matching a tracked
    # listing go to AVBT/<kind>/<name>/<code>/ instead; codes with no
    # tracked match still land here.
    pikpak_archive_folder: str = "AVBT/已完成"
    archive_interval_seconds: int = 60

    # PikPak presence index + missing-codes feature
    presence_ttl_seconds: int = 300            # cache lifetime
    missing_listing_cache_seconds: int = 3600  # JavBus listing cache
    missing_max_pages: int = 50                # safety cap when crawling a listing

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

    http_proxy: str = ""

    # Optional webhook fired after each successful auto-archive. Body is
    # `{"content": "..."}` which is compatible with Discord webhooks.
    webhook_url: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/avbt.db"


settings = Settings()
