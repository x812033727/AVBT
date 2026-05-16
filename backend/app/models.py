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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# Legacy table kept so an existing DB still loads cleanly; new code uses
# TrackedListing and we copy rows over in init_db().
class TrackedActress(Base):
    __tablename__ = "tracked_actresses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    avatar: Mapped[str] = mapped_column(String(1024), default="")
    uncensored: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_send: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_code: Mapped[str] = mapped_column(String(64), default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
