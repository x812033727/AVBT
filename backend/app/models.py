from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
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


class OfflineTaskLog(Base):
    __tablename__ = "offline_task_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), index=True, default="")
    magnet: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(String(128), default="")
    file_id: Mapped[str] = mapped_column(String(128), default="")
    name: Mapped[str] = mapped_column(String(512), default="")
    phase: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
