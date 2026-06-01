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


class LinkRef(BaseModel):
    """Generic {name, id} pair used for studio / label / series / director."""
    name: str
    id: str = ""


class MovieDetail(BaseModel):
    code: str
    title: str
    cover: str = ""
    release_date: str = ""
    duration: str = ""
    studio: Optional[LinkRef] = None
    label: Optional[LinkRef] = None
    director: Optional[LinkRef] = None
    series: Optional[LinkRef] = None
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


class TrackedListingIn(BaseModel):
    """kind ∈ {star, studio, label, series, director}."""
    kind: str
    id: str
    name: str = ""
    avatar: str = ""
    uncensored: bool = False
    auto_send: bool = False


class TrackedListingOut(TrackedListingIn):
    last_seen_code: str = ""
    last_checked_at: Optional[datetime] = None
    last_error: str = ""
    new_count: int = 0
    created_at: datetime


class CheckListingResult(BaseModel):
    kind: str
    id: str
    name: str = ""
    new_codes: list[str] = Field(default_factory=list)
    error: str = ""


# Kept for backwards compat with old backup files.
TrackedActressIn = TrackedListingIn
TrackedActressOut = TrackedListingOut
CheckActressResult = CheckListingResult


class CollectionOut(CollectionIn):
    created_at: datetime
    updated_at: datetime


# ---------- PikPak ----------

class PikPakLogin(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    encoded_token: Optional[str] = None  # 直接貼 token 就不用帳密
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
    # Soft upper bound: prefer magnets at or below this size, but fall
    # back to oversized candidates when nothing fits. 0 / None = no soft cap.
    prefer_max_size_mb: Optional[float] = None


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


# ---------- pCloud ----------

class PCloudLogin(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    # ``access_token`` lets the user paste a pCloud-issued token instead
    # of supplying username + password. Token wins when both are set.
    access_token: Optional[str] = None
    remember: bool = True


class PCloudFile(BaseModel):
    id: str
    name: str
    kind: str  # "folder" | "file"
    size: Optional[int] = None
    parent_id: Optional[str] = None
    created_time: Optional[str] = None


class PCloudQuota(BaseModel):
    used: int = 0
    limit: int = 0


# ----- transfer queue (PikPak → pCloud via savefilefromurl) -----

class PCloudTransferRequest(BaseModel):
    """Enqueue body. Exactly one of ``pikpak_file_ids`` (single/multi-file
    mode) and ``pikpak_folder_id`` (recursive folder mode) should be set.
    ``folder`` is the destination path on pCloud — auto-created if it
    doesn't exist. Empty string = use ``pcloud_default_folder``."""
    pikpak_file_ids: list[str] = Field(default_factory=list)
    pikpak_folder_id: str = ""
    folder: str = ""
    delete_source: bool = False
    # Only used in recursive folder mode. Mirror PikPak's subfolder tree
    # under the destination instead of flattening everything into one
    # bucket.
    preserve_subfolders: bool = True


class PCloudTransferOut(BaseModel):
    id: int
    parent_id: Optional[int] = None
    pikpak_file_id: str
    pikpak_name: str
    pikpak_size: int = 0
    pikpak_path: str = ""
    pcloud_folder_id: int = 0
    pcloud_folder_path: str = ""
    pcloud_upload_id: int = 0
    pcloud_file_id: int = 0
    status: str
    message: str = ""
    bytes_downloaded: int = 0
    delete_source: bool = False
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime] = None


class PCloudTransferPage(BaseModel):
    items: list[PCloudTransferOut]
    total: int
    pending: int
    running: int
    done: int
    failed: int


class PCloudEnqueueResult(BaseModel):
    enqueued: int
    transfer_ids: list[int] = Field(default_factory=list)
    folder_path: str = ""
    folder_id: int = 0


# ---------- Missing-codes / presence index ----------

class ExtraCode(BaseModel):
    """A code physically present in a tracked listing's folder that is
    NOT in the JavBus catalog for that listing — i.e. likely misplaced
    or no longer listed upstream."""

    code: str
    paths: list[str] = Field(default_factory=list)


class MissingCodesResult(BaseModel):
    kind: str
    id: str
    name: str = ""
    total: int = 0
    present_codes: list[str] = Field(default_factory=list)
    missing: list[MovieListItem] = Field(default_factory=list)
    extras: list[ExtraCode] = Field(default_factory=list)
    pages_scanned: int = 0
    # The folder the archiver would put a newly-completed code under
    # for this tracked listing, e.g. "AVBT/系列/回胴錄".
    # Lets the UI display the exact path it's looking for.
    expected_root: str = ""
    built_at: datetime


class MissingSummaryItem(BaseModel):
    kind: str
    id: str
    name: str = ""
    total: int = 0
    missing_count: int = 0
    extras_count: int = 0
    pages_scanned: int = 0
    expected_root: str = ""
    error: str = ""


class MissingSummary(BaseModel):
    built_at: datetime
    presence_built_at: Optional[datetime] = None
    items: list[MissingSummaryItem] = Field(default_factory=list)


class AggregatedMissingItem(BaseModel):
    kind: str
    id: str
    name: str = ""
    missing: list[MovieListItem] = Field(default_factory=list)


class AggregatedMissing(BaseModel):
    built_at: datetime
    presence_built_at: Optional[datetime] = None
    items: list[AggregatedMissingItem] = Field(default_factory=list)


class PresenceStatus(BaseModel):
    built_at: Optional[datetime] = None
    size: int = 0
    last_error: str = ""
    ttl_seconds: int = 0
    ready: bool = False


class PresenceRoot(BaseModel):
    path: str
    leaves: int = 0
    codes: int = 0
    unrecognized: int = 0


class PresenceUnrecognized(BaseModel):
    parent: str
    name: str


class PresenceDetail(PresenceStatus):
    roots: list[PresenceRoot] = Field(default_factory=list)
    unrecognized: list[PresenceUnrecognized] = Field(default_factory=list)
    unrecognized_total: int = 0


class PresenceCodeLookup(BaseModel):
    code: str
    paths: list[str] = Field(default_factory=list)


class ReorganizeOptions(BaseModel):
    dry_run: bool = True


# ---------- pCloud ----------

class PCloudLogin(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    access_token: Optional[str] = None


class PCloudStatus(BaseModel):
    logged_in: bool = False
    username: str = ""
    user_id: int = 0
    region: str = "us"
    has_stored_token: bool = False
    has_env_credentials: bool = False
    has_env_token: bool = False
    default_folder: str = ""


class PCloudFolderEntry(BaseModel):
    """One row of a pCloud folder listing."""
    folder_id: int = 0
    file_id: int = 0
    name: str
    is_folder: bool = False
    size: int = 0


class PCloudFolderListing(BaseModel):
    folder_id: int
    path: str
    parent_folder_id: Optional[int] = None
    entries: list[PCloudFolderEntry] = Field(default_factory=list)


class PCloudTransferRequest(BaseModel):
    """Enqueue request body.

    Exactly one of ``pikpak_file_ids`` (single/multi-file mode) and
    ``pikpak_folder_id`` (recursive folder mode) should be set. ``folder``
    is the destination path on pCloud — auto-created if missing. ``folder``
    blank = use ``pcloud_default_folder`` from settings.
    """
    pikpak_file_ids: list[str] = Field(default_factory=list)
    pikpak_folder_id: str = ""
    folder: str = ""  # destination path on pCloud
    delete_source: bool = False
    # Only used when pikpak_folder_id is set. Mirror PikPak's subfolder
    # tree under the destination instead of flattening everything.
    preserve_subfolders: bool = True


class PCloudTransferOut(BaseModel):
    id: int
    parent_id: Optional[int] = None
    pikpak_file_id: str
    pikpak_name: str
    pikpak_size: int = 0
    pikpak_path: str = ""
    pcloud_folder_id: int = 0
    pcloud_folder_path: str = ""
    pcloud_upload_id: int = 0
    pcloud_file_id: int = 0
    status: str
    message: str = ""
    bytes_downloaded: int = 0
    delete_source: bool = False
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime] = None


class PCloudTransferPage(BaseModel):
    items: list[PCloudTransferOut]
    total: int
    pending: int
    running: int
    done: int
    failed: int


class PCloudEnqueueResult(BaseModel):
    enqueued: int
    transfer_ids: list[int] = Field(default_factory=list)
    folder_path: str = ""
    folder_id: int = 0


class EpisodeItem(BaseModel):
    """A multi-part video file surfaced by the episode-finder walk."""

    file_id: str
    name: str
    code: str
    category: str  # "canonical" (already _N) | "multifile" (raw/CD/-N/variant)
    marker_index: int = 0
    parent_id: str
    parent_path: str
    size: Optional[int] = None


# ---------- Auth (single-account login gate) ----------

class AuthStatus(BaseModel):
    needs_setup: bool


class SetupIn(BaseModel):
    username: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    token: str
    username: str


class MeOut(BaseModel):
    username: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str
