"""Cross-cloud duplicate JAV-code detection.

Given a PikPak folder and a pCloud folder, recursively walk both subtrees,
extract the JAV code from every file / folder name, and report the codes
that appear in BOTH clouds — i.e. works the user is storing twice.

Read-only: nothing is moved, renamed or deleted on either side. The walk
mirrors :func:`episode_finder.walk_for_episodes` — BFS, one layer in
parallel under a small concurrency cap, bounded by ``max_depth`` and a
total-item ``cap``.

Matching uses the *base* JAV code (:func:`extract_jav_code`), so
resolution / Chinese-sub / variant-letter differences collapse onto the
same product: ``DAM-043``, ``DAM-043C`` and ``483DAM-043`` all count as
``DAM-043``. A code is recorded both when it names a folder
(``DAM-043/``) and when it names a bare file (``DAM-043.mp4``), so either
storage layout is detected.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import AsyncIterator, Awaitable, Callable

from .jav_code import extract_jav_code
from .pcloud import pcloud_service
from .pikpak import pikpak_service


logger = logging.getLogger(__name__)


_LIST_CONCURRENCY = 4
_SCAN_PROGRESS_EVERY = 5  # emit a progress event every N folders listed
# Cap the recorded paths per code so a pathological tree (the same code in
# hundreds of places) can't bloat the streamed payload. The count is still
# accurate; only the example paths are truncated.
_PATHS_PER_CODE_CAP = 50


async def _walk_codes(
    root_id: str,
    list_fn: Callable[[str], Awaitable[list]],
    folder_kinds: tuple[str, ...],
    *,
    max_depth: int,
    cap: int,
) -> AsyncIterator[dict]:
    """BFS from ``root_id``, extracting a JAV code from every item name.

    ``list_fn`` returns the direct children of a folder id; ``folder_kinds``
    are the ``kind`` strings that mark an item as a recursable folder
    (``"drive#folder"`` for PikPak, ``"folder"`` for pCloud).

    Yields ``{"kind": "progress", folders_done, items_seen, codes}`` every
    few folders, then a terminal ``{"kind": "result", codes, items_seen,
    folders_done, partial}`` where ``codes`` is ``{code: [paths]}``.
    """
    sem = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def _list(parent_id: str) -> list:
        async with sem:
            return await list_fn(parent_id)

    codes: dict[str, list[str]] = {}
    queue: deque[tuple[str, str, int]] = deque([(root_id, "", 0)])
    folders_done = 0
    items_seen = 0
    partial = False

    while queue:
        # Drain one BFS layer in parallel for smoother throughput.
        layer = list(queue)
        queue.clear()
        results = await asyncio.gather(
            *[_list(fid) for fid, _p, _d in layer], return_exceptions=True
        )
        for (folder_id, folder_path, depth), res in zip(layer, results):
            folders_done += 1
            if isinstance(res, Exception):
                # A failure listing the root (depth 0) is almost always a
                # logged-out account or a bad folder id — surface it so the
                # caller can emit an error instead of silently reporting 0
                # codes. Deeper failures (a single unreadable subfolder) are
                # logged and skipped so one bad branch can't abort the scan.
                if depth == 0:
                    raise res
                logger.debug(
                    "duplicate-walk list(%s) failed: %s", folder_id, res
                )
                continue
            for child in res:
                items_seen += 1
                child_path = (
                    f"{folder_path}/{child.name}" if folder_path else child.name
                )
                code = extract_jav_code(child.name)
                if code:
                    bucket = codes.setdefault(code, [])
                    if (
                        len(bucket) < _PATHS_PER_CODE_CAP
                        and child_path not in bucket
                    ):
                        bucket.append(child_path)
                if child.kind in folder_kinds and depth < max_depth:
                    queue.append((child.id, child_path, depth + 1))
                if items_seen >= cap:
                    partial = True
                    break
            if folders_done % _SCAN_PROGRESS_EVERY == 0:
                yield {
                    "kind": "progress",
                    "folders_done": folders_done,
                    "items_seen": items_seen,
                    "codes": len(codes),
                }
            if partial:
                break
        if partial:
            break

    yield {
        "kind": "result",
        "codes": codes,
        "items_seen": items_seen,
        "folders_done": folders_done,
        "partial": partial,
    }


async def find_duplicates_stream(
    pikpak_folder_id: str,
    pcloud_folder_id: str,
    *,
    max_depth: int = 8,
    cap: int = 20000,
) -> AsyncIterator[dict]:
    """Scan the PikPak subtree then the pCloud subtree and emit the codes
    present in both.

    Event shapes (NDJSON):
      ``start``    { pikpak_folder_id, pcloud_folder_id }
      ``progress`` { side: "pikpak"|"pcloud", folders_done, items_seen, codes }
      ``error``    { side?, message }
      ``done``     { result: { duplicates: [{code, pikpak_paths, pcloud_paths}],
                               duplicate_count, pikpak_codes, pcloud_codes,
                               pikpak_items, pcloud_items,
                               pikpak_partial, pcloud_partial } }

    The two sides are scanned sequentially (PikPak first) so progress reads
    cleanly; neither cloud is mutated.
    """
    yield {
        "type": "start",
        "pikpak_folder_id": pikpak_folder_id,
        "pcloud_folder_id": pcloud_folder_id,
    }

    async def _pikpak_list(parent_id: str) -> list:
        files, _partial = await pikpak_service.list_all_files(parent_id)
        return files

    async def _pcloud_list(parent_id: str) -> list:
        return await pcloud_service.list_files(parent_id)

    sides = (
        ("pikpak", pikpak_folder_id, _pikpak_list, ("drive#folder",)),
        ("pcloud", pcloud_folder_id, _pcloud_list, ("folder",)),
    )

    collected: dict[str, dict] = {}
    for side, root_id, list_fn, folder_kinds in sides:
        try:
            async for ev in _walk_codes(
                root_id, list_fn, folder_kinds, max_depth=max_depth, cap=cap
            ):
                if ev["kind"] == "progress":
                    yield {
                        "type": "progress",
                        "side": side,
                        "folders_done": ev["folders_done"],
                        "items_seen": ev["items_seen"],
                        "codes": ev["codes"],
                    }
                else:
                    collected[side] = ev
        except Exception as exc:  # noqa: BLE001
            label = "PikPak" if side == "pikpak" else "pCloud"
            yield {
                "type": "error",
                "side": side,
                "message": f"{label} 掃描失敗: {exc}",
            }
            return

    pikpak = collected.get("pikpak", {})
    pcloud = collected.get("pcloud", {})
    pikpak_codes: dict[str, list[str]] = pikpak.get("codes", {})
    pcloud_codes: dict[str, list[str]] = pcloud.get("codes", {})

    shared = sorted(set(pikpak_codes) & set(pcloud_codes))
    duplicates = [
        {
            "code": code,
            "pikpak_paths": pikpak_codes.get(code, []),
            "pcloud_paths": pcloud_codes.get(code, []),
        }
        for code in shared
    ]

    yield {
        "type": "done",
        "result": {
            "duplicates": duplicates,
            "duplicate_count": len(duplicates),
            "pikpak_codes": len(pikpak_codes),
            "pcloud_codes": len(pcloud_codes),
            "pikpak_items": pikpak.get("items_seen", 0),
            "pcloud_items": pcloud.get("items_seen", 0),
            "pikpak_partial": pikpak.get("partial", False),
            "pcloud_partial": pcloud.get("partial", False),
        },
    }
