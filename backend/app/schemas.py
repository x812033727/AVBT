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


class CollectionOut(CollectionIn):
    created_at: datetime
    updated_at: datetime


# ---------- PikPak ----------

class PikPakLogin(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None


class OfflineSubmit(BaseModel):
    magnet: str
    code: str = ""
    folder: Optional[str] = None


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
