from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- JavBus ----------

class MovieListItem(BaseModel):
    code: str
    title: str
    cover: str = ""
    detail_url: str = ""
    date: str = ""


class SearchResult(BaseModel):
    items: list[MovieListItem]
    page: int
    has_next: bool
    total_pages: Optional[int] = None


class Magnet(BaseModel):
    name: str
    link: str
    size: str = ""
    date: str = ""
    is_hd: bool = False
    has_subtitle: bool = False


class ActressRef(BaseModel):
    name: str
    id: str = ""  # JavBus /star/{id} slug; empty if not linked


class StarProfile(BaseModel):
    id: str
    name: str = ""
    avatar: str = ""
    birthday: str = ""
    age: str = ""
    height: str = ""
    cup: str = ""
    bust: str = ""
    waist: str = ""
    hip: str = ""
    birthplace: str = ""
    hobby: str = ""


class GenreRef(BaseModel):
    name: str
    id: str = ""


class MovieDetail(BaseModel):
    code: str
    title: str
    cover: str = ""
    release_date: str = ""
    duration: str = ""
    studio: str = ""
    label: str = ""
    director: str = ""
    series: str = ""
    actresses: list[ActressRef] = Field(default_factory=list)
    genres: list[GenreRef] = Field(default_factory=list)
    samples: list[str] = Field(default_factory=list)
    magnets: list[Magnet] = Field(default_factory=list)


# ---------- Collection ----------

class CollectionIn(BaseModel):
    code: str
    title: str = ""
    cover: str = ""
    release_date: str = ""
    duration: str = ""
    actresses: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    note: str = ""
    status: str = "wishlist"


class HistoryItem(BaseModel):
    id: int
    code: str = ""
    magnet: str
    task_id: str = ""
    file_id: str = ""
    name: str = ""
    phase: str = ""
    message: str = ""
    archived: bool = False
    archived_at: Optional[datetime] = None
    created_at: datetime


class HistoryPage(BaseModel):
    items: list[HistoryItem]
    total: int
    offset: int
    limit: int


class TrackedActressIn(BaseModel):
    id: str
    name: str = ""
    avatar: str = ""
    uncensored: bool = False
    auto_send: bool = False


class TrackedActressOut(TrackedActressIn):
    last_seen_code: str = ""
    last_checked_at: Optional[datetime] = None
    last_error: str = ""
    new_count: int = 0
    created_at: datetime


class CheckActressResult(BaseModel):
    id: str
    name: str = ""
    new_codes: list[str] = Field(default_factory=list)
    error: str = ""


class CollectionOut(CollectionIn):
    created_at: datetime
    updated_at: datetime


# ---------- PikPak ----------

class PikPakLogin(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    remember: bool = True


class OfflineSubmit(BaseModel):
    magnet: str
    code: str = ""
    folder: Optional[str] = None
    force: bool = False  # send even if this btih hash is already in the log


class SendAllOptions(BaseModel):
    uncensored: bool = False
    max_pages: int = 5
    hd_only: bool = True
    subtitle_only: bool = False
    skip_sent: bool = True
    folder: Optional[str] = None
    # File-size filters in megabytes; 0 / None = unbounded
    min_size_mb: Optional[float] = None
    max_size_mb: Optional[float] = None


class SendAllResult(BaseModel):
    total_movies: int = 0
    sent: int = 0
    skipped_no_magnet: int = 0
    skipped_already_sent: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)


class PikPakFile(BaseModel):
    id: str
    name: str
    kind: str
    size: Optional[int] = None
    parent_id: Optional[str] = None
    created_time: Optional[str] = None
    thumbnail_link: Optional[str] = None


class PikPakTask(BaseModel):
    id: str
    name: str
    phase: str
    progress: Optional[int] = None
    file_id: Optional[str] = None
    file_size: Optional[int] = None
    message: Optional[str] = None
    created_time: Optional[str] = None


class PikPakQuota(BaseModel):
    used: int = 0
    limit: int = 0
    expire: Optional[str] = None
