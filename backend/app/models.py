from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class CollectedMovie(Base):
    __tablename__ = "collected_movies"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    cover: Mapped[str] = mapped_column(String(1024), default="")
    release_date: Mapped[str] = mapped_column(String(32), default="")
    duration: Mapped[str] = mapped_column(String(32), default="")
    actresses: Mapped[list] = mapped_column(JSON, default=list)
    genres: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="wishlist")  # wishlist / downloading / done
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrackedListing(Base):
    """One JavBus listing axis we want to watch for new works.

    kind ∈ {star, studio, label, series, director}. Composite primary key
    (kind, id) so the same slug can exist under multiple axes.
    """
    __tablename__ = "tracked_listing"

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    avatar: Mapped[str] = mapped_column(String(1024), default="")
    uncensored: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_send: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_code: Mapped[str] = mapped_column(String(64), default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    quiet_ticks: Mapped[int] = mapped_column(Integer, default=0)
    last_full_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_missing_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppMeta(Base):
    """Tiny KV store for app-level state that isn't worth a table:
    one-time migration flags (``migrated:*``), notification toggles
    (``notify:*``), last auto-backup timestamp. The physical legacy
    ``tracked_actresses`` table (pre-TrackedListing) is left in existing
    DBs untouched; its one-time copy into tracked_listing is guarded by
    a ``migrated:*`` flag here."""
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class PCloudTransfer(Base):
    """One PikPak → pCloud transfer job.

    States: ``pending`` → ``running`` → ``done`` | ``failed`` | ``cancelled``.
    ``upload_id`` is filled once pCloud has accepted the savefilefromurl
    call; that's also the handle we use to poll progress + cancel.
    ``parent_id`` (FK to another PCloudTransfer.id) chains files that came
    from the same "send whole folder recursively" request, so the UI can
    group them.
    """
    __tablename__ = "pcloud_transfer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # PikPak source
    pikpak_file_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    pikpak_name: Mapped[str] = mapped_column(String(512), default="")
    pikpak_size: Mapped[int] = mapped_column(Integer, default=0)
    pikpak_path: Mapped[str] = mapped_column(String(1024), default="")
    # pCloud target
    pcloud_folder_id: Mapped[int] = mapped_column(Integer, default=0)
    pcloud_folder_path: Mapped[str] = mapped_column(String(1024), default="")
    pcloud_upload_id: Mapped[int] = mapped_column(Integer, default=0)
    pcloud_file_id: Mapped[int] = mapped_column(Integer, default=0)
    # Lifecycle
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    message: Mapped[str] = mapped_column(Text, default="")
    bytes_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    delete_source: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthAccount(Base):
    """The single admin account that gates the whole site.

    This is intentionally single-row: setup creates row id=1 once and
    refuses to create a second. There is no registration / multi-user —
    all data tables stay global. Password is stored as a PBKDF2-SHA256
    digest (see services/auth.py), never in plaintext.
    """
    __tablename__ = "auth_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class OfflineTaskLog(Base):
    __tablename__ = "offline_task_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), index=True, default="")
    magnet: Mapped[str] = mapped_column(Text)
    btih: Mapped[str] = mapped_column(String(64), index=True, default="")
    task_id: Mapped[str] = mapped_column(String(128), default="")
    file_id: Mapped[str] = mapped_column(String(128), default="")
    name: Mapped[str] = mapped_column(String(512), default="")
    phase: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Snapshot of the tracked listing this code belonged to at enqueue
    # time. Lets the archiver pick the right kind/name folder without
    # re-fetching JavBus. Empty for manual submits — those fall back to
    # the JavBus-driven path in _resolve_archive_path.
    tracked_kind: Mapped[str] = mapped_column(String(16), default="")
    tracked_slug: Mapped[str] = mapped_column(String(64), default="")
    tracked_name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
