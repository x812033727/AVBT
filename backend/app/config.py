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

    http_proxy: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/avbt.db"


settings = Settings()
