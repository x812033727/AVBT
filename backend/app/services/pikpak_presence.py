"""Cached index of every JAV code currently present in the PikPak account.

The archiver writes new completions into a hierarchical layout like
``AVBT/<kind label>/<name>/<code>/`` (e.g. ``AVBT/系列/MIDV/MIDV-001``).
Codes that pre-date the hierarchy still sit under ``AVBT/已完成/<code>``.

A "missing code" UI needs a flat lookup ("is code X present anywhere?"),
so we walk those known roots once and keep the resulting set in memory
with a short TTL. Cross-category membership is handled at query time:
the index doesn't care which kind/name folder physically stores a code.

Each scan target is taken from ``config.all_kind_paths()``, which honours
per-kind env overrides like ``PIKPAK_SERIES_FOLDER`` — so a non-standard
layout (e.g. ``AVBT/AVBT/系列/系列/<name>``) can still be reached.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from ..config import all_kind_paths, settings
from .jav_code import normalize_code
from .pikpak import PikPakError, pikpak_service

logger = logging.getLogger(__name__)


_LIST_CONCURRENCY = 4
# Per-folder item ceiling for the paginated walk. The old single-page
# ``size=500`` call silently truncated large folders (e.g. a busy
# ``AVBT/已完成``), making present codes look missing.
_LIST_MAX_ITEMS = 5000


class PikPakPresenceIndex:
    def __init__(self) -> None:
        self._codes: set[str] | None = None
        self._paths: dict[str, list[str]] = {}
        self._roots: list[dict[str, Any]] = []
        self._unrecognized: list[dict[str, str]] = []
        self._built_at: datetime | None = None
        self._last_error: str = ""
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    # ---------- public ----------

    def status(self) -> dict[str, Any]:
        return {
            "built_at": self._built_at.isoformat() if self._built_at else None,
            "size": len(self._codes) if self._codes is not None else 0,
            "last_error": self._last_error,
            "ttl_seconds": settings.presence_ttl_seconds,
            "ready": self._codes is not None,
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
        self._built_at = None  # next get() will rebuild

    def peek(self) -> set[str] | None:
        """Non-blocking access. Returns whatever is currently cached
        (may be stale / None). Used by stale-while-revalidate paths."""
        return set(self._codes) if self._codes is not None else None

    async def get(self, *, force: bool = False) -> set[str]:
        if not force and self._is_fresh():
            return set(self._codes or set())
        return await self.rebuild(force=force)

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
                logger.info("presence index rebuilt: %d codes", len(codes))
                return set(codes)
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("presence index rebuild failed: %s", exc)
                # Keep whatever stale data we had to avoid empty results.
                return set(self._codes or set())

    # ---------- internals ----------

    def _is_fresh(self) -> bool:
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
        """For a kind dir: list name dirs, then list each name dir's
        children. Leaves may be code-named folders (``DAM-043/``) OR
        bare video files (``DAM-044.mp4``); both count as the code
        being present."""
        name_dirs = await self._list(kind_dir_id)
        targets = [n for n in name_dirs if n.kind == "drive#folder"]
        if not targets:
            self._roots.append(
                {"path": root_path, "leaves": 0, "codes": 0, "unrecognized": 0}
            )
            return set()

        leaf_lists = await asyncio.gather(
            *[self._list(n.id) for n in targets], return_exceptions=True
        )
        codes: set[str] = set()
        leaves_total = 0
        unrecognized_count = 0
        for name_dir, leaves in zip(targets, leaf_lists, strict=True):
            if isinstance(leaves, Exception):
                continue
            parent = f"{root_path}/{name_dir.name}"
            for leaf in leaves:
                leaves_total += 1
                c = normalize_code(leaf.name)
                if c:
                    codes.add(c)
                    self._record(c, f"{parent}/{leaf.name}")
                else:
                    unrecognized_count += 1
                    self._unrecognized.append(
                        {"parent": parent, "name": leaf.name}
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
