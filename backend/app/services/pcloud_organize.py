"""JAV-code cleanup + organize passes for pCloud.

Extracted from services/pcloud.py as a mixin: these are long streaming
methods that only need the core client surface (``self._call``,
``self._folder_param`` / ``_file_param``, list/move/rename/trash ops).
``PCloudService`` inherits from :class:`PCloudOrganizeMixin`, so
behaviour and call sites are unchanged."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..config import kind_base_path, settings
from ..schemas import PCloudFile
from .jav_code import ext_of, extract_jav_code, extract_jav_code_full, is_video
from .pcloud_errors import PCloudError
from .rename_plan import _build_video_rename_plan, _uniquify_target

logger = logging.getLogger(__name__)

# Video files below this size are treated as junk samples/ads by the
# cleanup pass.
_JUNK_BYTES = 300 * 1024 * 1024
_ORGANIZE_MAX_DEPTH = 6


class PCloudOrganizeMixin:
    async def cleanup_folder_stream(
        self, folder_id: str, *, dry_run: bool = True
    ) -> AsyncIterator[dict]:
        """Tidy every direct child of ``folder_id`` *in place*:

        - **Files** get their BT-noise name normalised to
          ``<JAV_CODE>.<ext>`` (multi-file groups → ``<canon>_N.<ext>``).
        - **Wrapper folders** are flattened: we walk the folder's subtree
          (recursively, up to :data:`_ORGANIZE_MAX_DEPTH`), pull each
          distinct work's main video OUT into ``folder_id`` renamed
          ``<code>.<ext>``, and trash the now-empty wrapper. This is the
          "我把整個 <番號>/ 資料夾丟進來,影片卻埋在裡面" case — cleanup now
          lifts the video out instead of leaving it nested.

        Unlike the ``organize`` pass this stays within ``folder_id`` (no
        JavBus, no AVBT/<類別>/<名稱>/ categorisation) — it just makes the
        folder's own contents flat and cleanly named. Use ``organize`` to
        additionally sort into category folders.

        For safety the wrapper is only trashed when **every** substantial
        (≥ ``_JUNK_BYTES``) video was successfully extracted; if some
        couldn't be placed or a move failed, the wrapper is left intact.
        """
        try:
            children = await self.list_files(folder_id)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
            return

        taken: set[str] = {c.name for c in children}
        PART_MIN_BYTES = 500 * 1024 * 1024
        multipart_plan, multipart_members = _build_video_rename_plan(
            children, PART_MIN_BYTES, is_video
        )

        summary = {
            "total": len(children),
            "renamed": 0,
            "flattened": 0,
            "skipped": 0,
            "errors": 0,
            "dry_run": dry_run,
        }

        yield {
            "type": "start",
            "total": len(children),
            "dry_run": dry_run,
            "folder_id": folder_id,
        }

        # Monotonic key — a wrapper can fan out into several extractions,
        # so the per-child index is no longer unique.
        seq = 0
        here = self._folder_param(str(folder_id))

        for _idx, child in enumerate(children, start=1):
            await asyncio.sleep(0.02)
            kind = "folder" if child.kind == "folder" else "file"
            code = extract_jav_code(child.name)
            code_full = extract_jav_code_full(child.name) or code

            try:
                # ---- Folder children: flatten the wrapper in place ----
                if kind == "folder":
                    videos, _ = await self._collect_videos_in_subtree(child.id)
                    if videos:
                        # Group by base code (video's own, falling back to
                        # the wrapper name's). Extract the largest of each
                        # group out to THIS folder, renamed to its full
                        # code; trash the wrapper only if nothing
                        # substantial is left behind.
                        groups: dict[str, list[PCloudFile]] = {}
                        for v in videos:
                            vcode = extract_jav_code(v.name) or code
                            if vcode:
                                groups.setdefault(vcode, []).append(v)
                        substantial_total = sum(
                            1 for v in videos
                            if v.size is not None and v.size >= _JUNK_BYTES
                        )

                        if groups:
                            substantial_done = 0
                            extracted_any = False
                            move_failed = False
                            for gcode, gvids in groups.items():
                                keeper = max(
                                    gvids, key=lambda v: int(v.size or 0)
                                )
                                kcode = (
                                    extract_jav_code_full(keeper.name)
                                    or code_full or gcode
                                )
                                seq += 1
                                ev = {
                                    "type": "progress",
                                    "current": seq,
                                    "kind": "folder",
                                    "source": child.name,
                                }
                                final_name = await self._move_keeper_to_target(
                                    keeper, kcode, here, taken, dry_run=dry_run
                                )
                                if final_name is None:
                                    move_failed = True
                                    summary["errors"] += 1
                                    yield {**ev, "action": "error",
                                           "target": None, "reason": "搬移失敗"}
                                    continue
                                extracted_any = True
                                if keeper.size is not None and keeper.size >= _JUNK_BYTES:
                                    substantial_done += 1
                                summary["flattened"] += 1
                                yield {**ev, "action": "flatten",
                                       "target": final_name, "reason": None}

                            # Trash only when every substantial video was
                            # pulled out — never delete a work we couldn't
                            # place (orphan / dup / multi-part remainder).
                            if (
                                extracted_any
                                and not move_failed
                                and substantial_done == substantial_total
                                and not dry_run
                            ):
                                await self._trash_folder(child)
                            continue
                        # No video carried a resolvable code → fall through
                        # to the plain folder-rename below.

                # ---- Files, and video-less folders: normalise the name ----
                if not code:
                    seq += 1
                    summary["skipped"] += 1
                    yield {"type": "progress", "current": seq, "kind": kind,
                           "source": child.name, "action": "skip",
                           "target": None, "reason": "no_code"}
                    continue

                seq += 1
                base_event = {
                    "type": "progress",
                    "current": seq,
                    "kind": kind,
                    "source": child.name,
                }

                if kind == "file":
                    if child.name in multipart_plan:
                        target = multipart_plan[child.name]
                    elif child.name in multipart_members:
                        summary["skipped"] += 1
                        yield {
                            **base_event,
                            "action": "skip",
                            "target": child.name,
                            "reason": "already_clean",
                        }
                        continue
                    else:
                        target = f"{code_full}{ext_of(child.name)}"
                else:
                    target = code_full

                if target == child.name:
                    summary["skipped"] += 1
                    yield {
                        **base_event,
                        "action": "skip",
                        "target": target,
                        "reason": "already_clean",
                    }
                    continue

                target = _uniquify_target(target, taken)
                if not dry_run:
                    await self.rename_file(child.id, target)
                taken.discard(child.name)
                taken.add(target)
                summary["renamed"] += 1
                yield {**base_event, "action": "rename", "target": target, "reason": None}
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning("pcloud cleanup failed for %s: %s", child.name, exc)
                seq += 1
                yield {"type": "progress", "current": seq, "kind": kind,
                       "source": child.name, "action": "error",
                       "target": None, "reason": str(exc)}

        yield {"type": "done", "result": summary}

    async def _collect_videos_in_subtree(
        self, folder_id: str, *, max_depth: int = _ORGANIZE_MAX_DEPTH
    ) -> tuple[list[PCloudFile], int]:
        """Walk the subtree rooted at ``folder_id`` (bounded by
        ``max_depth``) and return ``(all_video_files, direct_child_count)``.

        Unlike the old one-level peek, this recurses so a video buried
        under several torrent-name dirs (``MIUM-1104/<torrent>/<rls>/MIUM-1104.mp4``)
        is still found. ``direct_child_count`` counts only the immediate
        children of ``folder_id`` — used for the cosmetic extras tally.
        """
        try:
            items = await self.list_files(folder_id)
        except PCloudError:
            return [], 0
        direct_count = len(items)
        videos: list[PCloudFile] = []
        for it in items:
            if it.kind == "folder":
                if max_depth > 0:
                    sub, _ = await self._collect_videos_in_subtree(
                        it.id, max_depth=max_depth - 1
                    )
                    videos.extend(sub)
            elif is_video(it.name):
                videos.append(it)
        return videos, direct_count

    async def _resolve_target_id(
        self,
        target_path: str,
        target_cache: dict[str, tuple[int | None, set[str]]],
        *,
        dry_run: bool,
    ) -> tuple[int | None, set[str]]:
        """Resolve ``target_path`` to ``(folder_id, sibling_names)``,
        memoised in ``target_cache``. ``dry_run`` uses read-only
        :meth:`lookup_path` (returns ``(None, set())`` when the folder
        doesn't exist yet); live mode :meth:`ensure_path`-creates it."""
        if target_path not in target_cache:
            if dry_run:
                tid = await self.lookup_path(target_path)
            else:
                tid = await self.ensure_path(target_path)
            if tid is None:
                target_cache[target_path] = (None, set())
            else:
                siblings = await self.list_files(str(tid))
                target_cache[target_path] = (tid, {s.name for s in siblings})
        return target_cache[target_path]

    async def _resolve_listing_with_retry(
        self, code: str, timeout: float
    ) -> tuple[str, tuple[str, str] | None]:
        """Two-attempt JavBus lookup wrapping :func:`resolve_listing_loose`.

        Returns ``(status, resolved)``:
          - ``("ok", (kind, name))`` — JavBus categorised the code.
          - ``("none", None)`` — JavBus answered but has no series /
            label / studio for it (no retry; a None answer is stable).
          - ``("timeout", None)`` — both attempts timed out.

        The scraper already does its own 429 backoff; the single retry
        here only rescues an unlucky first attempt that blew our
        per-code wall-clock budget.
        """
        # Lazy import to break the archiver ↔ pcloud import cycle.
        from .archiver import resolve_listing_loose

        for attempt in (1, 2):
            try:
                resolved = await asyncio.wait_for(
                    resolve_listing_loose(code), timeout=timeout
                )
                return ("ok", resolved) if resolved is not None else ("none", None)
            except TimeoutError:
                if attempt == 1:
                    logger.info(
                        "pCloud organize: JavBus timeout for %s, "
                        "retrying after 2s",
                        code,
                    )
                    await asyncio.sleep(2)
        return ("timeout", None)

    async def _move_keeper_to_target(
        self,
        keeper: PCloudFile,
        code: str,
        target_folder_id: int,
        taken: set[str],
        *,
        dry_run: bool,
    ) -> str | None:
        """Move ``keeper`` to ``target_folder_id`` renamed ``<code>.<ext>``.

        Returns the final name on success, or ``None`` if the move call
        failed. Does **not** trash anything — the caller trashes the
        wrapper once, after every keeper has been pulled out, so a
        multi-video wrapper never loses the works we didn't extract yet.
        ``dry_run`` skips the API call but still reserves the name.
        """
        canonical = f"{code}{ext_of(keeper.name)}"
        final_name = _uniquify_target(canonical, taken)

        if not dry_run:
            params: dict[str, Any] = {
                "fileid": self._file_param(keeper.id),
                "tofolderid": target_folder_id,
            }
            if keeper.name != final_name:
                params["toname"] = final_name
            try:
                await self._call("renamefile", params)
            except PCloudError as exc:
                logger.warning(
                    "flatten move keeper %s → %s failed: %s",
                    keeper.name, final_name, exc,
                )
                return None

        taken.add(final_name)
        return final_name

    async def _trash_folder(self, folder: PCloudFile) -> None:
        """Trash ``folder`` recursively. Best-effort: a failure here only
        leaves a now-junk-only wrapper behind, which is cosmetically ugly
        but not fatal — the keepers are already extracted. pCloud trash is
        recoverable for the account's retention window (15-30 days)."""
        try:
            await self._call(
                "deletefolderrecursive",
                {"folderid": self._file_param(folder.id)},
            )
        except PCloudError as exc:
            logger.warning(
                "flatten trash wrapper %s failed: %s", folder.name, exc
            )

    async def organize_folder_stream(
        self, folder_id: str, *, dry_run: bool = True
    ) -> AsyncIterator[dict]:
        """Move each direct child of ``folder_id`` to the canonical
        archive path ``/<kind_base>/<tracked_name>/`` based on the JAV
        code in its name and JavBus metadata.

        Folder children are always opened and walked **recursively** (up
        to :data:`_ORGANIZE_MAX_DEPTH` levels) to find the main video —
        so a wrapper whose own name has no code, or whose video is buried
        several torrent-name dirs deep, still gets flattened. The code is
        taken from the wrapper name, falling back to the extracted
        video's own name. The keeper video is renamed ``<code>.<ext>``
        and the wrapper trashed.

        Where the video ends up depends on JavBus:
          - **Categorised** (``series → label → studio``) → moved to
            ``AVBT/<類別>/<名稱>/``.
          - **Uncategorised** (JavBus has no listing for the code) → the
            video is still pulled out of its wrapper *in place* (into the
            folder being organised) so it's no longer buried. Previously
            these were skipped with ``no_listing`` and the video stayed
            stuck inside the wrapper.

        **Tracked listings are NOT required.** Items with no recognisable
        code anywhere, or that already live at the resolved target, are
        skipped with a structured ``reason``; a JavBus timeout is an
        ``error`` so the user can retry just that code.

        ``dry_run`` mode uses :meth:`lookup_path` (read-only) so a
        preview never materialises empty target folders.
        """
        try:
            children = await self.list_files(folder_id)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"列出資料夾失敗: {exc}"}
            return

        summary = {
            "total": len(children),
            "moved": 0,
            "flattened": 0,
            "skipped": 0,
            "errors": 0,
            "dry_run": dry_run,
        }

        yield {
            "type": "start",
            "total": len(children),
            "dry_run": dry_run,
            "folder_id": folder_id,
        }

        # Per-run cache: target path → (target_id, taken_names). Avoids
        # re-walking ``lookup_path`` and re-listing siblings for every
        # child that maps to the same tracked listing.
        target_cache: dict[str, tuple[int | None, set[str]]] = {}

        # Names already present at ``folder_id`` itself — the collision
        # set for uncategorised in-place flattens (video pulled out of a
        # wrapper but JavBus couldn't categorise it). Seeded from the
        # current listing and grown as we extract, so two uncategorised
        # wrappers don't both land on ``<code>.<ext>``.
        inplace_taken: set[str] = {c.name for c in children}

        # Per-code JavBus timeout. The scraper itself does up to 4
        # attempts with exponential 429 backoff and a 30s per-request
        # timeout — total worst-case ~2 minutes. We can't reasonably
        # wait that long per file, but ``settings.pcloud_organize_javbus_timeout``
        # defaults to 60s so a single transient slow response (first
        # attempt + one retry) doesn't error-skip an otherwise valid
        # code. Bump via env if your JavBus access is unusually slow.
        JAVBUS_TIMEOUT_SECONDS = settings.pcloud_organize_javbus_timeout
        TIMEOUT_REASON = (
            f"JavBus 兩次查詢都逾時（各 {int(JAVBUS_TIMEOUT_SECONDS)}s）。"
            "該番號頁面可能持續慢或被 429 限流 — "
            "點「再來一次」隔一陣子重試;若常常逾時可在 .env 設 "
            "PCLOUD_ORGANIZE_JAVBUS_TIMEOUT=120"
        )

        # Monotonic key for each emitted progress event. A single folder
        # child can fan out into several extractions, so the per-child
        # ``idx`` is no longer unique — ``seq`` is.
        seq = 0

        for idx, child in enumerate(children, start=1):
            await asyncio.sleep(0.02)
            kind = "folder" if child.kind == "folder" else "file"

            # Heartbeat BEFORE any await — so the UI sees activity even
            # when the very first child triggers a slow JavBus lookup.
            # Without this, a 15s timeout on item 1 leaves the modal
            # stuck on "waiting for first event" the whole time.
            yield {
                "type": "processing",
                "current": idx,
                "total": len(children),
                "source": child.name,
                "kind": kind,
            }

            # Defined before the try so the except handler can report it
            # even if the very first lookup raises.
            code = extract_jav_code(child.name)

            try:
                # ---- Folder children: recursively pull out EVERY work ----
                # Walk the subtree, group videos by code (each video's own,
                # falling back to the wrapper name's), and extract the
                # largest of each code group renamed ``<code>.<ext>``. This
                # reaches videos nested several torrent-dirs deep AND lifts
                # a folder that bundles multiple different works — not just
                # one. Same-code extras are resolution dups, trashed with
                # the wrapper at the end.
                if kind == "folder":
                    videos, direct_count = await self._collect_videos_in_subtree(
                        child.id
                    )
                    if videos:
                        groups: dict[str, list[PCloudFile]] = {}
                        # True if we can't safely trash the wrapper after
                        # extraction (a substantial video we couldn't place,
                        # or a keeper whose move/lookup didn't complete).
                        leftover = False
                        for v in videos:
                            vcode = extract_jav_code(v.name) or code
                            if not vcode:
                                if v.size is not None and v.size >= _JUNK_BYTES:
                                    leftover = True
                                continue
                            groups.setdefault(vcode, []).append(v)

                        if groups:
                            extracted_any = False
                            extras_count = max(0, direct_count - 1)
                            for gcode, gvids in groups.items():
                                keeper = max(
                                    gvids, key=lambda v: int(v.size or 0)
                                )
                                status, resolved = (
                                    await self._resolve_listing_with_retry(
                                        gcode, JAVBUS_TIMEOUT_SECONDS
                                    )
                                )
                                seq += 1
                                ev = {
                                    "type": "progress",
                                    "current": seq,
                                    "kind": "folder",
                                    "source": child.name,
                                }
                                if status == "timeout":
                                    leftover = True  # keeper untouched
                                    summary["errors"] += 1
                                    yield {**ev, "action": "error",
                                           "code": gcode, "reason": TIMEOUT_REASON}
                                    continue

                                if status == "ok":
                                    listing_kind, listing_name = resolved  # type: ignore[misc]
                                    target_path = (
                                        f"{kind_base_path(listing_kind)}/{listing_name}"
                                    )
                                    target_id, taken = await self._resolve_target_id(
                                        target_path, target_cache, dry_run=dry_run
                                    )
                                    move_to = (
                                        self._folder_param(str(target_id))
                                        if target_id is not None else 0
                                    )
                                    move_taken = taken
                                else:
                                    # JavBus can't categorise → pull the
                                    # video out of its wrapper *in place*.
                                    listing_kind = listing_name = None
                                    target_path = None
                                    target_id = None
                                    move_to = self._folder_param(str(folder_id))
                                    move_taken = inplace_taken

                                final_name = await self._move_keeper_to_target(
                                    keeper, gcode, move_to, move_taken,
                                    dry_run=dry_run,
                                )
                                if final_name is None:
                                    leftover = True  # keeper still inside
                                    summary["errors"] += 1
                                    yield {**ev, "action": "error",
                                           "code": gcode, "reason": "搬移失敗"}
                                    continue

                                extracted_any = True
                                summary["flattened"] += 1
                                out = {
                                    **ev,
                                    "action": "flatten",
                                    "code": gcode,
                                    "listing_kind": listing_kind,
                                    "listing_name": listing_name,
                                    "target_path": target_path,
                                    "target_name": final_name,
                                    "extras_count": extras_count,
                                }
                                if status == "ok" and target_id is None:
                                    out["would_create"] = True
                                if status != "ok":
                                    out["uncategorized"] = True
                                yield out

                            # Trash the wrapper once, after every keeper is
                            # out — but only when nothing substantial was
                            # left behind (an un-placed video, a failed
                            # move, a timeout). pCloud trash is recoverable
                            # but we still avoid deleting un-extracted works.
                            if extracted_any and not leftover and not dry_run:
                                await self._trash_folder(child)
                            continue
                        # No video had a resolvable code → fall through to
                        # the wrapper-as-is / skip paths using the name code.

                # ---- File children, or video-less / code-less folders ----
                if not code:
                    seq += 1
                    summary["skipped"] += 1
                    yield {"type": "progress", "current": seq, "kind": kind,
                           "source": child.name, "action": "skip",
                           "reason": "no_code"}
                    continue

                status, resolved = await self._resolve_listing_with_retry(
                    code, JAVBUS_TIMEOUT_SECONDS
                )
                seq += 1
                base_event = {
                    "type": "progress",
                    "current": seq,
                    "kind": kind,
                    "source": child.name,
                }
                if status == "timeout":
                    summary["errors"] += 1
                    yield {**base_event, "action": "error",
                           "code": code, "reason": TIMEOUT_REASON}
                    continue
                if status != "ok":
                    # Uncategorised file / video-less wrapper: nowhere
                    # categorised to go, so it stays put.
                    summary["skipped"] += 1
                    yield {**base_event, "action": "skip",
                           "code": code, "reason": "no_listing"}
                    continue

                listing_kind, listing_name = resolved  # type: ignore[misc]
                target_path = f"{kind_base_path(listing_kind)}/{listing_name}"
                target_id, taken = await self._resolve_target_id(
                    target_path, target_cache, dry_run=dry_run
                )

                # Same-folder no-op: child already lives at the resolved
                # target. ``target_id is None`` (dry_run, target doesn't
                # exist) naturally fails this check.
                if target_id is not None and str(target_id) == str(folder_id):
                    summary["skipped"] += 1
                    yield {
                        **base_event,
                        "action": "skip",
                        "code": code,
                        "listing_kind": listing_kind,
                        "listing_name": listing_name,
                        "target_path": target_path,
                        "reason": "already_organized",
                    }
                    continue

                # dry_run + target doesn't exist yet → report as
                # `would_create` so the UI can flag it without an actual id.
                if target_id is None:
                    summary["moved"] += 1
                    yield {
                        **base_event,
                        "action": "move",
                        "code": code,
                        "listing_kind": listing_kind,
                        "listing_name": listing_name,
                        "target_path": target_path,
                        "target_name": child.name,
                        "would_create": True,
                    }
                    continue

                new_name = _uniquify_target(child.name, taken)

                if not dry_run:
                    # pCloud's renamefile/renamefolder accepts tofolderid
                    # + toname in the same call, so we move and (if
                    # needed) rename atomically. This avoids a brief
                    # window where the source folder would hold a
                    # duplicate name.
                    fid_int = self._file_param(child.id)
                    params: dict[str, Any] = {
                        "tofolderid": self._folder_param(str(target_id)),
                    }
                    if new_name != child.name:
                        params["toname"] = new_name
                    if kind == "file":
                        params["fileid"] = fid_int
                        try:
                            await self._call("renamefile", params)
                        except PCloudError as exc:
                            # 2009 = "file does not exist" — fall back
                            # to renamefolder when our heuristic kind
                            # guess was wrong (e.g. listfolder returned
                            # a folder we treated as a file).
                            if getattr(exc, "result", 0) != 2009:
                                raise
                            params.pop("fileid", None)
                            params["folderid"] = fid_int
                            await self._call("renamefolder", params)
                    else:
                        params["folderid"] = fid_int
                        await self._call("renamefolder", params)

                taken.add(new_name)
                summary["moved"] += 1
                yield {
                    **base_event,
                    "action": "move",
                    "code": code,
                    "listing_kind": listing_kind,
                    "listing_name": listing_name,
                    "target_path": target_path,
                    "target_name": new_name,
                }
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning(
                    "pcloud organize failed for %s: %s", child.name, exc
                )
                seq += 1
                yield {
                    "type": "progress",
                    "current": seq,
                    "kind": kind,
                    "source": child.name,
                    "action": "error",
                    "code": code,
                    "reason": str(exc),
                }

        yield {"type": "done", "result": summary}

    # ---------- PikPak → pCloud transfer support ----------
    #
    # These are used by ``services.pcloud_transfer`` to make pCloud pull
    # files directly from PikPak's CDN. ``savefilefromurl`` is async on
    # the server side: it returns immediately with an ``upload_id`` and
    # downloads in the background; we poll ``savefilefromurlstatus`` for
    # progress.
