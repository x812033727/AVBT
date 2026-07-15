"""Persistent index of every JAV code currently present in the PikPak account.

The archiver writes new completions into a hierarchical layout like
``AVBT/<kind label>/<name>/<code>/`` (e.g. ``AVBT/系列/MIDV/MIDV-001``).
Codes that pre-date the hierarchy still sit under ``AVBT/已完成/<code>``.

A "missing code" UI needs a flat lookup ("is code X present anywhere?").
The answer is stored in the ``presence_entry`` table and mirrored in
memory; cross-category membership is handled at query time — the index
doesn't care which kind/name folder physically stores a code.

Walking the whole drive is EXPENSIVE (10k+ codes, minutes of PikPak
calls). It therefore happens only when explicitly asked for (settings →
重建索引 / ``rebuild(force=True)``) or to bootstrap an empty table. Every
other update is per-code: the pipeline calls :meth:`refresh_codes` for
the codes it just landed / finalized, which costs one folder listing
each instead of a full walk. A snapshot loaded from the DB is trusted
indefinitely — out-of-band changes (files moved by hand in the PikPak
web UI) surface as a stale hint in ``status()``, not as a background
re-walk.

Each scan target is taken from ``config.all_kind_paths()``, which honours
per-kind env overrides like ``PIKPAK_SERIES_FOLDER`` — so a non-standard
layout (e.g. ``AVBT/AVBT/系列/系列/<name>``) can still be reached.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from ..config import all_kind_paths, settings
from ..database import SessionLocal
from ..models import AppMeta, PresenceEntry
from .jav_code import normalize_code
from .pikpak import PikPakError, pikpak_service

logger = logging.getLogger(__name__)


_LIST_CONCURRENCY = 4
# Max folder levels to descend under a kind root when looking for code
# leaves. The flat layout keeps codes at depth 1 (``<name>/<code>``); the
# nested 製作商 layout keeps them at depth 2
# (``<studio>/<series>/<code>``). Descending up to 3 covers both without
# an unbounded walk into unexpected trees.
_MAX_KIND_DEPTH = 3
# Per-folder item ceiling for the paginated walk. The old single-page
# ``size=500`` call silently truncated large folders (e.g. a busy
# ``AVBT/已完成``), making present codes look missing.
_LIST_MAX_ITEMS = 5000
# app_meta key holding the last full-walk timestamp (ISO string).
_BUILT_AT_KEY = "presence:built_at"
# Per-code refresh fan-out. Each code costs ~1-2 listings, so a modest
# width keeps a finalize pass responsive without hammering PikPak.
_REFRESH_CONCURRENCY = 4


class PikPakPresenceIndex:
    def __init__(self) -> None:
        self._codes: set[str] | None = None
        self._paths: dict[str, list[str]] = {}
        self._roots: list[dict[str, Any]] = []
        self._unrecognized: list[dict[str, str]] = []
        self._built_at: datetime | None = None
        self._last_error: str = ""
        self._stale: bool = False
        self._loaded_from_db: bool = False
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    # ---------- public ----------

    @property
    def built_at(self) -> datetime | None:
        """When the current snapshot finished building (naive UTC)."""
        return self._built_at

    def status(self) -> dict[str, Any]:
        return {
            "built_at": self._built_at.isoformat() if self._built_at else None,
            "size": len(self._codes) if self._codes is not None else 0,
            "last_error": self._last_error,
            "ttl_seconds": settings.presence_ttl_seconds,
            "ready": self._codes is not None,
            # A bulk operation touched the drive in ways the per-code
            # updates can't track — the numbers still work, but a
            # 重建索引 would make them exact.
            "stale": self._stale,
        }

    def detail(self) -> dict[str, Any]:
        """Status + roots scanned + sample of leaf folders we couldn't
        normalise into a code. Lets the user see *what* the index actually
        looked at, so they can spot files stored under unexpected paths."""
        return {
            **self.status(),
            "roots": list(self._roots),
            "unrecognized": list(self._unrecognized[:50]),
            "unrecognized_total": len(self._unrecognized),
        }

    def paths_for(self, code: str) -> list[str]:
        c = normalize_code(code) or (code or "").upper()
        return list(self._paths.get(c, []))

    def codes_under(self, *prefixes: str) -> dict[str, list[str]]:
        """Return ``{code: [matching_paths]}`` for every indexed code
        whose recorded path starts with one of the given folder
        prefixes. Drives the "extras" detection: codes physically
        located under a tracked listing's folder."""
        if not self._paths:
            return {}
        needles = tuple(p.rstrip("/") + "/" for p in prefixes if p)
        if not needles:
            return {}
        out: dict[str, list[str]] = {}
        for code, paths in self._paths.items():
            matched = [p for p in paths if any(p.startswith(n) for n in needles)]
            if matched:
                out[code] = matched
        return out

    def invalidate(self) -> None:
        """Flag the snapshot as possibly-behind (bulk reorganize, manual
        cleanup). Deliberately does NOT drop the data or arm a re-walk:
        a full walk is minutes of PikPak calls and now only happens on
        an explicit rebuild(force=True). Per-code refreshes keep the
        pipeline's own landings exact in the meantime."""
        self._stale = True

    def peek(self) -> set[str] | None:
        """Non-blocking access. Returns whatever is currently cached
        (may be stale / None). Used by stale-while-revalidate paths."""
        return set(self._codes) if self._codes is not None else None

    async def get(self, *, force: bool = False) -> set[str]:
        """Codes currently archived. Cheap by default: memory, else the
        persisted table. Only an explicit ``force`` (or a never-built
        index) pays for a full drive walk."""
        if force:
            return await self.rebuild(force=True)
        if self._codes is not None:
            return set(self._codes)
        if await self._load_from_db():
            return set(self._codes or set())
        # Nothing persisted yet — bootstrap once.
        return await self.rebuild(force=True)

    async def rebuild(self, *, force: bool = False) -> set[str]:
        async with self._lock:
            # Skip the rebuild if a concurrent caller already finished
            # one within the TTL — but ONLY when this call wasn't an
            # explicit refresh. Without the ``force`` carve-out an
            # /presence/refresh click right after a fresh build would
            # be a silent no-op, leaving the user staring at stale data
            # (the "deleted files still show as 多餘" case).
            if not force and self._is_fresh():
                return set(self._codes or set())
            try:
                codes = await self._build()
                self._codes = codes
                self._built_at = datetime.utcnow()
                self._last_error = ""
                self._stale = False
                self._loaded_from_db = True
                await self._save_to_db()
                logger.info("presence index rebuilt: %d codes", len(codes))
                return set(codes)
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("presence index rebuild failed: %s", exc)
                # Keep whatever stale data we had to avoid empty results.
                return set(self._codes or set())

    # ---------- persistence ----------

    async def _load_from_db(self) -> bool:
        """Populate the in-memory mirror from ``presence_entry``. Returns
        False when nothing is persisted yet (caller bootstraps)."""
        try:
            async with SessionLocal() as session:
                rows = (
                    await session.execute(
                        select(PresenceEntry.code, PresenceEntry.path)
                    )
                ).all()
                meta = await session.get(AppMeta, _BUILT_AT_KEY)
        except Exception as exc:  # noqa: BLE001 — never fail a read path
            logger.warning("presence load from DB failed: %s", exc)
            return False
        if not rows:
            return False
        paths: dict[str, list[str]] = {}
        for code, path in rows:
            paths.setdefault(code, []).append(path)
        self._paths = paths
        self._codes = set(paths)
        if meta and meta.value:
            try:
                self._built_at = datetime.fromisoformat(meta.value)
            except ValueError:
                self._built_at = None
        logger.info("presence index loaded from DB: %d codes", len(self._codes))
        self._loaded_from_db = True
        return True

    async def _save_to_db(self) -> None:
        """Replace the persisted snapshot with the in-memory one."""
        try:
            async with SessionLocal() as session:
                await session.execute(delete(PresenceEntry))
                session.add_all(
                    [
                        PresenceEntry(code=code, path=path)
                        for code, bucket in self._paths.items()
                        for path in bucket
                    ]
                )
                await session.merge(
                    AppMeta(
                        key=_BUILT_AT_KEY,
                        value=(
                            self._built_at.isoformat() if self._built_at else ""
                        ),
                    )
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("presence save to DB failed: %s", exc)

    async def _persist_code(self, code: str, paths: list[str]) -> None:
        """Upsert one code's rows (empty ``paths`` deletes it)."""
        try:
            async with SessionLocal() as session:
                await session.execute(
                    delete(PresenceEntry).where(PresenceEntry.code == code)
                )
                session.add_all(
                    [PresenceEntry(code=code, path=p) for p in paths]
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("presence persist %s failed: %s", code, exc)

    # ---------- incremental updates ----------

    async def refresh_codes(
        self, codes: list[str], *, exclude_ids: set[str] | None = None
    ) -> int:
        """Re-read just these codes' archive folders and update the index
        (memory + DB). Costs ~1-2 listings per code — the cheap
        alternative to a full walk when the pipeline lands or finalizes
        specific codes. Returns how many codes changed.

        ``exclude_ids`` are entries the caller has just deleted. PikPak's
        listing is eventually consistent, so refreshing straight after a
        trash still sees the dead entry — which reproduces the cached
        paths exactly, trips the no-change check below, and strands the
        phantom path in the index for good (nothing re-reads a finalized
        code, and only an unrelated full walk ever cleared it). Naming
        the ids makes the update deterministic instead of a race.
        """
        wanted = [c for c in {normalize_code(c) or c for c in codes} if c]
        if not wanted:
            return 0
        if self._codes is None:
            await self._load_from_db()
        sem = asyncio.Semaphore(_REFRESH_CONCURRENCY)
        gone = frozenset(exclude_ids or ())

        async def one(code: str) -> tuple[str, list[str]] | None:
            async with sem:
                try:
                    return code, await self._live_paths_for(
                        code, exclude_ids=gone
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("presence refresh %s failed: %s", code, exc)
                    return None

        results = await asyncio.gather(*[one(c) for c in wanted])
        changed = 0
        for res in results:
            if res is None:
                continue
            code, paths = res
            if sorted(self._paths.get(code, [])) == sorted(paths):
                continue
            changed += 1
            if paths:
                self._paths[code] = paths
                if self._codes is None:
                    self._codes = set()
                self._codes.add(code)
            else:
                self._paths.pop(code, None)
                if self._codes is not None:
                    self._codes.discard(code)
            await self._persist_code(code, paths)
        if changed:
            logger.info("presence refreshed %d/%d codes", changed, len(wanted))
        return changed

    async def _live_paths_for(
        self, code: str, *, exclude_ids: frozenset[str] = frozenset()
    ) -> list[str]:
        """Current archive paths for one code, read live from PikPak.

        Looks in the code's resolved 製作商/<studio>/<series> folder (both
        layouts: a ``CODE`` folder leaf and loose ``CODE*.ext`` videos)
        and the legacy archive folder. Returns [] when the code isn't
        there any more (caller drops it from the index). ``exclude_ids``
        skips entries the caller deleted but the listing may still
        return — see :meth:`refresh_codes`."""
        from .archiver import studio_series_dir_for_code  # avoid cycle

        found: list[str] = []
        dirs: list[str] = []
        # Where the archiver would put it (detail cache only — a miss
        # must not become a JavBus fetch on this per-code hot path).
        try:
            nested = await studio_series_dir_for_code(code, allow_fetch=False)
        except Exception:  # noqa: BLE001
            nested = None
        if nested:
            dirs.append(nested)
        # Where we last saw it: catches codes whose detail isn't cached
        # and codes that moved out (the re-list then returns nothing and
        # the caller drops the entry).
        for known in self._paths.get(code, []):
            parent = known.rsplit("/", 1)[0]
            if parent and parent not in dirs:
                dirs.append(parent)
        legacy = (settings.pikpak_archive_folder or "AVBT/已完成").strip("/")
        if legacy and legacy not in dirs:
            dirs.append(legacy)
        for d in dirs:
            try:
                folder_id = await pikpak_service.lookup_folder_id(d)
            except Exception:  # noqa: BLE001
                continue
            if not folder_id:
                continue
            for child in await self._list(folder_id):
                if child.id in exclude_ids:
                    continue
                if normalize_code(child.name) == code:
                    path = f"{d}/{child.name}"
                    if path not in found:
                        found.append(path)
        return found

    # ---------- internals ----------

    def _is_fresh(self) -> bool:
        """Whether the last FULL walk is within the TTL. No longer gates
        reads (the persisted snapshot is authoritative) — kept so a
        concurrent double-click on 重建索引 doesn't walk twice."""
        if self._codes is None or self._built_at is None:
            return False
        ttl = max(30, settings.presence_ttl_seconds)
        return datetime.utcnow() - self._built_at < timedelta(seconds=ttl)

    async def _list(self, parent_id: str) -> list:
        async with self._sem:
            try:
                files, partial = await pikpak_service.list_all_files(
                    parent_id=parent_id, cap=_LIST_MAX_ITEMS
                )
                if partial:
                    logger.warning(
                        "presence walk truncated at %d items under folder %s "
                        "— codes beyond the cap will look missing",
                        len(files), parent_id,
                    )
                return files
            except PikPakError as exc:
                logger.debug("list_all_files(%s) failed: %s", parent_id, exc)
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_all_files(%s) failed: %s", parent_id, exc)
                return []

    def _record(self, code: str, path: str) -> None:
        bucket = self._paths.setdefault(code, [])
        if path not in bucket:
            bucket.append(path)

    async def _build(self) -> set[str]:
        # Reset diagnostics so stale results don't survive a failed rebuild.
        self._paths = {}
        self._roots = []
        self._unrecognized = []

        codes: set[str] = set()

        # Walk each configured kind base path. lookup_folder_id avoids
        # creating side-effect empty folders for unused kinds.
        kind_jobs = [
            self._scan_kind_path(path) for _kind, path in all_kind_paths()
        ]
        legacy_path = (
            settings.pikpak_archive_folder or "AVBT/已完成"
        ).strip().strip("/")
        legacy_job = self._scan_legacy_path(legacy_path)

        results = await asyncio.gather(
            *kind_jobs, legacy_job, return_exceptions=True
        )
        for r in results:
            if isinstance(r, set):
                codes |= r

        return codes

    async def _scan_kind_path(self, kind_path: str) -> set[str]:
        """Resolve ``kind_path`` (e.g. ``AVBT/系列`` or a custom override
        like ``AVBT/AVBT/系列/系列``), then walk depth 2 (name dirs →
        code leaves). Returns an empty set when the path doesn't exist."""
        try:
            kind_id = await pikpak_service.lookup_folder_id(kind_path)
        except Exception:  # noqa: BLE001
            return set()
        if not kind_id:
            return set()
        return await self._collect_kind(kind_path, kind_id)

    async def _scan_legacy_path(self, legacy_path: str) -> set[str]:
        try:
            legacy_id = await pikpak_service.lookup_folder_id(legacy_path)
        except Exception:  # noqa: BLE001
            return set()
        if not legacy_id:
            return set()
        return await self._collect_legacy(legacy_path, legacy_id)

    async def _collect_kind(
        self, root_path: str, kind_dir_id: str
    ) -> set[str]:
        """Walk a kind dir for code leaves, tolerating variable nesting.

        A code leaf may be a code-named folder (``DAM-043/``) OR a bare
        video file (``DAM-044.mp4``); both count as the code being
        present. Between the root and the leaves there may be one level
        of name dirs (flat layout ``<name>/<code>``) or two levels
        (nested 製作商 layout ``<studio>/<series>/<code>``). We descend
        into any non-code subfolder up to ``_MAX_KIND_DEPTH`` and record
        every code we find, so old and new layouts coexist during the
        migration.
        """
        codes: set[str] = set()
        stats = {"leaves": 0, "unrecognized": 0}
        await self._walk_kind(root_path, kind_dir_id, 0, codes, stats)
        self._roots.append(
            {
                "path": root_path,
                "leaves": stats["leaves"],
                "codes": len(codes),
                "unrecognized": stats["unrecognized"],
            }
        )
        return codes

    async def _walk_kind(
        self,
        path: str,
        dir_id: str,
        depth: int,
        codes: set[str],
        stats: dict[str, int],
    ) -> None:
        children = await self._list(dir_id)
        recurse: list = []
        for ch in children:
            c = normalize_code(ch.name)
            if c:
                # A code-named leaf (folder or file) — record it, don't
                # descend further (the code folder's video lives inside
                # but presence only needs the code + this path).
                stats["leaves"] += 1
                codes.add(c)
                self._record(c, f"{path}/{ch.name}")
            elif ch.kind == "drive#folder" and depth < _MAX_KIND_DEPTH:
                recurse.append(ch)
            else:
                stats["leaves"] += 1
                stats["unrecognized"] += 1
                self._unrecognized.append({"parent": path, "name": ch.name})
        if recurse:
            await asyncio.gather(
                *[
                    self._walk_kind(
                        f"{path}/{ch.name}", ch.id, depth + 1, codes, stats
                    )
                    for ch in recurse
                ],
                return_exceptions=True,
            )

    async def _collect_legacy(
        self, root_path: str, legacy_dir_id: str
    ) -> set[str]:
        """``AVBT/已完成/<leaf>`` — depth 1. Leaves may be code-named
        folders or bare video files; both count."""
        leaves = await self._list(legacy_dir_id)
        codes: set[str] = set()
        unrecognized_count = 0
        leaves_total = 0
        for leaf in leaves:
            leaves_total += 1
            c = normalize_code(leaf.name)
            if c:
                codes.add(c)
                self._record(c, f"{root_path}/{leaf.name}")
            else:
                unrecognized_count += 1
                self._unrecognized.append(
                    {"parent": root_path, "name": leaf.name}
                )
        self._roots.append(
            {
                "path": root_path,
                "leaves": leaves_total,
                "codes": len(codes),
                "unrecognized": unrecognized_count,
            }
        )
        return codes


presence_index = PikPakPresenceIndex()
