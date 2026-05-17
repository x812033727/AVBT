"""Cached index of every JAV code currently present in the PikPak account.

The archiver writes new completions into a hierarchical layout
``AVBT/<kind>/<name>/<code>/`` (e.g. ``AVBT/series/MIDV/MIDV-001``). Codes
that pre-date the hierarchy still sit under ``AVBT/已完成/<code>``.

A "missing code" UI needs a flat lookup ("is code X present anywhere?"),
so we walk those known roots once and keep the resulting set in memory
with a short TTL. Cross-category membership is handled at query time:
the index doesn't care which kind/name folder physically stores a code.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from .jav_code import normalize_code
from .pikpak import PikPakError, pikpak_service


logger = logging.getLogger(__name__)


# Direct children of AVBT/ that we treat as category buckets.
_KIND_DIRS = ("star", "series", "studio", "label", "director")

_LIST_CONCURRENCY = 4
_LIST_PAGE_SIZE = 500


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

    def invalidate(self) -> None:
        self._built_at = None  # next get() will rebuild

    def peek(self) -> set[str] | None:
        """Non-blocking access. Returns whatever is currently cached
        (may be stale / None). Used by stale-while-revalidate paths."""
        return set(self._codes) if self._codes is not None else None

    async def get(self, *, force: bool = False) -> set[str]:
        if not force and self._is_fresh():
            return set(self._codes or set())
        return await self.rebuild()

    async def rebuild(self) -> set[str]:
        async with self._lock:
            # Another coroutine may have rebuilt while we were waiting.
            if self._is_fresh():
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
                return await pikpak_service.list_files(
                    parent_id=parent_id, size=_LIST_PAGE_SIZE
                )
            except PikPakError as exc:
                logger.debug("list_files(%s) failed: %s", parent_id, exc)
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning("list_files(%s) failed: %s", parent_id, exc)
                return []

    async def _build(self) -> set[str]:
        # Resolve the root folder once. We accept any failure here by
        # propagating it up so the caller records last_error.
        root_path = settings.pikpak_download_folder or "AVBT"
        root_id = await pikpak_service.folder_id(root_path)
        # Reset path/root tracking on every build so stale diagnostics
        # don't survive a failed rebuild.
        self._paths = {}
        self._roots = []
        self._unrecognized = []
        if not root_id:
            return set()

        top_children = await self._list(root_id)
        codes: set[str] = set()

        kind_jobs: list = []
        legacy_jobs: list = []

        legacy_name = (settings.pikpak_archive_folder or "AVBT/已完成").rsplit(
            "/", 1
        )[-1]

        for child in top_children:
            if child.kind != "drive#folder":
                continue
            name = child.name or ""
            if name in _KIND_DIRS:
                kind_jobs.append(self._collect_kind(f"{root_path}/{name}", child.id))
            elif name == legacy_name:
                legacy_jobs.append(
                    self._collect_legacy(f"{root_path}/{name}", child.id)
                )

        results = await asyncio.gather(
            *kind_jobs, *legacy_jobs, return_exceptions=True
        )
        for r in results:
            if isinstance(r, set):
                codes |= r

        return codes

    def _record(self, code: str, path: str) -> None:
        bucket = self._paths.setdefault(code, [])
        if path not in bucket:
            bucket.append(path)

    async def _collect_kind(self, root_path: str, kind_dir_id: str) -> set[str]:
        """For an ``AVBT/<kind>`` dir: list name dirs, then list each
        name dir's children — leaves may be code-named folders
        (``DAM-043/``) OR bare video files (``DAM-044.mp4``); both
        count as the code being present."""
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
        for name_dir, leaves in zip(targets, leaf_lists):
            if isinstance(leaves, Exception):
                continue
            parent = f"{root_path}/{name_dir.name}"
            for leaf in leaves:
                # Leaves may be code-named folders (``DAM-043/``) OR bare
                # video files (``DAM-044.mp4``); both count as present.
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
