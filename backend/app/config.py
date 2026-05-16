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
    pikpak_archive_folder: str = "AVBT/已完成"
    archive_interval_seconds: int = 60

    # Actress tracker: every N seconds, check JavBus for new works of
    # every TrackedActress row.
    tracker_enabled: bool = True
    tracker_interval_seconds: int = 3600  # 1 hour
    tracker_auto_send_hd_only: bool = True
    tracker_auto_send_skip_sent: bool = True

    http_proxy: str = ""

    # Optional webhook fired after each successful auto-archive. Body is
    # `{"content": "..."}` which is compatible with Discord webhooks.
    webhook_url: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/avbt.db"


settings = Settings()
