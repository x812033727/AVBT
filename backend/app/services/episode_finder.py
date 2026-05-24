"""Recursive episode (分集) search for a cloud folder tree.

Provider-agnostic: the caller hands in the cloud-storage service
(PikPak or pCloud) and this walks/trashes/renames through it — both
expose the same ``list_all_files`` / ``trash_files`` / ``rename_file``
surface (see ``_CloudFS``).

Walks ``root_id`` BFS, lists each folder in parallel under a small
concurrency cap (matches ``pikpak_presence._LIST_CONCURRENCY``), and
classifies code-bearing video files into two categories:

* **canonical** — file's stem matches ``<code>_<digit>$`` (the
  standardized multipart form produced by ``_build_video_rename_plan``).
* **multifile** — code has 2+ siblings in the same folder where at
  least one isn't already in ``_N`` form (raw duplicates, ``CD1/CD2``,
  ``-1/-2``, variant letters, etc.).

Used by the ``/files/episodes/scan/stream`` endpoint to populate the
"搜尋分集" modal, then by ``/files/episodes/process/stream`` to trash
the user-selected files and strip ``_N`` markers from any code that
ends up a singleton.

Event shapes:
  ``start``         { root_id, root_path }
  ``scan_progress`` { folders_done, folders_queued, files_seen }
  ``item``          { episode: EpisodeItem }
  ``warn``          { folder_path, message }
  ``done``          { summary: { folders_visited, files_scanned,
                                 total_canonical, total_multifile, partial } }
  ``error``         { message }

  ``trash_progress`` { current, total, file_id, name }
  ``trash_done``     { trashed, failed }
  ``strip_progress`` { current, total, file_id, source, target, action }
  ``strip_done``     { renamed, skipped }
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import AsyncIterator, Protocol

from .jav_code import ext_of, extract_jav_code, extract_jav_code_full, is_video
from .pikpak import (
    _build_video_rename_plan,
    _canonical_video_name,
    _part_marker_index,
)


logger = logging.getLogger(__name__)


class _CloudFS(Protocol):
    """The slice of a cloud-storage service this module needs. Both
    ``PikPakService`` and ``PCloudService`` satisfy it structurally, so
    the same walk / trash / strip logic drives either provider — the
    only thing that differs is which service object the router hands in.
    """

    async def list_all_files(
        self, parent_id: str, *, cap: int = ...
    ) -> tuple[list, bool]: ...
    async def trash_files(self, ids: list[str]) -> dict: ...
    async def rename_file(self, file_id: str, new_name: str) -> dict: ...


# PikPak tags folders ``drive#folder``; pCloud tags them ``folder``.
# Normalise so the recursion / classification logic is provider-agnostic.
_FOLDER_KINDS = frozenset({"drive#folder", "folder"})


def _is_folder(child) -> bool:
    return getattr(child, "kind", "") in _FOLDER_KINDS


_LIST_CONCURRENCY = 4
_SCAN_PROGRESS_EVERY = 5  # emit a scan_progress event every N folders
_STRIP_SETTLE_SLEEP = 0.5  # let PikPak settle after trash before re-listing


# ``<anything>_<N>$`` after extension stripped. Used to flag canonical form
# without needing to know the exact code upfront (handles CD<N> etc where
# ``extract_jav_code`` returns None but the file has been normalised).
_CANONICAL_PART_RE = re.compile(r"_\d+$")


def _is_canonical_part(name: str) -> bool:
    stem = name
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", stem)
    if m:
        stem = stem[: m.start()]
    return bool(_CANONICAL_PART_RE.search(stem))


def _resolve_code(name: str) -> str:
    """Best-effort code for display. Falls back to the canonical-name
    derivation when ``extract_jav_code`` can't see past markers like
    ``CD<N>`` that confuse the basic regex."""
    code = extract_jav_code(name) or extract_jav_code_full(name)
    if code:
        return code
    canon = _canonical_video_name(name)
    return canon or ""


async def walk_for_episodes(
    service: _CloudFS,
    root_id: str,
    *,
    max_depth: int = 8,
    cap: int = 20000,
) -> AsyncIterator[dict]:
    """BFS walk from ``root_id``; yield NDJSON-shaped events for episodes
    found in every visited folder. See module docstring for event shapes.

    ``service`` is the cloud-storage service (PikPak or pCloud) to walk."""
    sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def _list(parent_id: str) -> list:
        async with sem:
            try:
                children, _partial = await service.list_all_files(parent_id)
                return children
            except Exception as exc:  # noqa: BLE001
                logger.debug("list_all_files(%s) failed: %s", parent_id, exc)
                raise

    # Resolve the human-readable root path best-effort. The wrapper
    # ``MoveModal`` uses "我的雲盤" for the root; we mirror that here.
    yield {
        "type": "start",
        "root_id": root_id,
        "root_path": "我的雲盤" if not root_id else root_id,
    }

    queue: deque[tuple[str, str, int]] = deque()
    queue.append((root_id, "", 0))

    folders_done = 0
    files_seen = 0
    total_canonical = 0
    total_multifile = 0
    partial = False

    try:
        while queue:
            # Drain one BFS layer in parallel for smoother throughput.
            layer = list(queue)
            queue.clear()

            jobs = [_list(fid) for fid, _p, _d in layer]
            results = await asyncio.gather(*jobs, return_exceptions=True)

            for (folder_id, folder_path, depth), res in zip(layer, results):
                folders_done += 1
                if isinstance(res, Exception):
                    yield {
                        "type": "warn",
                        "folder_path": folder_path or "(root)",
                        "message": f"列出資料夾失敗: {res}",
                    }
                    continue

                children = res

                # Queue subfolders for the next BFS layer.
                if depth < max_depth:
                    for c in children:
                        if _is_folder(c):
                            child_path = (
                                f"{folder_path}/{c.name}" if folder_path else c.name
                            )
                            queue.append((c.id, child_path, depth + 1))

                # ``_build_video_rename_plan`` returns ``group_members``:
                # every video that belongs to a multi-part group (2+
                # files sharing a canonical, regardless of marker style).
                # It correctly groups CD<N>/-<N>/raw-dup files that the
                # bare ``extract_jav_code`` can't pull a code out of.
                _plan, group_members = _build_video_rename_plan(
                    children, 500 * 1024 * 1024, is_video
                )

                for c in children:
                    if _is_folder(c) or not is_video(c.name):
                        continue
                    files_seen += 1
                    canonical = _is_canonical_part(c.name)
                    in_multi_group = c.name in group_members

                    if canonical and not in_multi_group:
                        # Lone ``<code>_N.<ext>`` (singleton) — already in
                        # canonical form but no companions. Surface so the
                        # user can choose to delete or un-split.
                        category = "canonical"
                    elif canonical and in_multi_group:
                        category = "canonical"
                    elif in_multi_group:
                        category = "multifile"
                    else:
                        # Single file with no episode marker; skip.
                        continue

                    if category == "canonical":
                        total_canonical += 1
                    else:
                        total_multifile += 1

                    code = _resolve_code(c.name)
                    yield {
                        "type": "item",
                        "episode": {
                            "file_id": c.id,
                            "name": c.name,
                            "code": code,
                            "category": category,
                            "marker_index": _part_marker_index(c.name, code),
                            "parent_id": folder_id,
                            "parent_path": folder_path or "我的雲盤",
                            "size": c.size,
                        },
                    }

                if folders_done % _SCAN_PROGRESS_EVERY == 0:
                    yield {
                        "type": "scan_progress",
                        "folders_done": folders_done,
                        "folders_queued": len(queue),
                        "files_seen": files_seen,
                    }

                if files_seen >= cap:
                    partial = True
                    break

            if partial:
                break

    except asyncio.CancelledError:
        # Client disconnected — bubble up to terminate the StreamingResponse.
        raise
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)}
        return

    yield {
        "type": "done",
        "summary": {
            "folders_visited": folders_done,
            "files_scanned": files_seen,
            "total_canonical": total_canonical,
            "total_multifile": total_multifile,
            "partial": partial,
        },
    }


async def process_trash_and_strip(
    service: _CloudFS,
    *,
    file_ids_to_trash: list[str],
    parent_ids_touched: list[str],
    auto_strip: bool = True,
) -> AsyncIterator[dict]:
    """Trash the user-selected files, then (optionally) strip ``_N``
    markers from any code that ended up as a singleton in the touched
    parent folders.

    ``service`` is the cloud-storage service (PikPak or pCloud)."""
    trashed = 0
    failed = 0

    total = len(file_ids_to_trash)
    if total:
        # Batch trash — send all at once but emit progress per file by
        # faking a step count. (The API is all-or-nothing.)
        try:
            await service.trash_files(file_ids_to_trash)
            trashed = total
            for i, fid in enumerate(file_ids_to_trash, start=1):
                yield {
                    "type": "trash_progress",
                    "current": i,
                    "total": total,
                    "file_id": fid,
                }
        except Exception as exc:  # noqa: BLE001
            failed = total
            yield {"type": "warn", "message": f"垃圾桶移動失敗: {exc}"}

    yield {"type": "trash_done", "trashed": trashed, "failed": failed}

    if not auto_strip:
        yield {"type": "done", "result": {"trashed": trashed, "renamed": 0, "skipped": 0}}
        return

    # ---- Strip phase ----
    if _STRIP_SETTLE_SLEEP > 0:
        await asyncio.sleep(_STRIP_SETTLE_SLEEP)

    renamed = 0
    skipped = 0
    affected = list(dict.fromkeys(p for p in parent_ids_touched if p))

    strip_step = 0
    for parent_id in affected:
        try:
            children, _ = await service.list_all_files(parent_id)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "warn", "message": f"重新列出失敗: {exc}"}
            continue

        plan, _members = _build_video_rename_plan(
            children, 500 * 1024 * 1024, is_video
        )
        taken = {c.name for c in children}

        # Only act on singleton-strip renames: source ends in ``_N``.
        # That fires the strip-N case in ``_build_video_rename_plan``
        # (and also a lonely-variant strip if applicable, which is
        # still a legitimate "remove the episode marker" outcome).
        # Skip plan entries that are pure BT-noise renames — those
        # belong to the broader cleanup feature, not this targeted op.
        by_name = {c.name: c for c in children}
        for src_name, target in plan.items():
            child = by_name.get(src_name)
            if not child:
                continue
            src_ext = ext_of(src_name)
            src_stem = src_name[: -len(src_ext)] if src_ext else src_name
            if not re.search(r"_\d+$", src_stem):
                continue

            strip_step += 1
            if target in taken and target != src_name:
                skipped += 1
                yield {
                    "type": "strip_progress",
                    "current": strip_step,
                    "file_id": child.id,
                    "source": src_name,
                    "target": target,
                    "action": "skip",
                    "reason": "target_exists",
                }
                continue

            try:
                await service.rename_file(child.id, target)
                renamed += 1
                taken.discard(src_name)
                taken.add(target)
                yield {
                    "type": "strip_progress",
                    "current": strip_step,
                    "file_id": child.id,
                    "source": src_name,
                    "target": target,
                    "action": "rename",
                }
            except Exception as exc:  # noqa: BLE001
                yield {
                    "type": "strip_progress",
                    "current": strip_step,
                    "file_id": child.id,
                    "source": src_name,
                    "target": target,
                    "action": "error",
                    "reason": str(exc),
                }

    yield {"type": "strip_done", "renamed": renamed, "skipped": skipped}
    yield {
        "type": "done",
        "result": {"trashed": trashed, "renamed": renamed, "skipped": skipped},
    }
