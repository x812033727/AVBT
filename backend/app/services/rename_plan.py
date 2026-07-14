"""Video rename-plan helpers shared by the PikPak sweep, pCloud
organize and the episode finder.

Given a folder's children, ``_build_video_rename_plan`` decides how the
video files should be renamed to the canonical ``<CODE>`` /
``<CODE>_N`` multipart convention. Extracted from services/pikpak.py —
the original module re-exports these names, so import sites are
unchanged."""

from __future__ import annotations

import re

from .jav_code import ext_of, extract_jav_code, extract_jav_code_full


def _uniquify_target(target: str, taken: set[str]) -> str:
    """Return ``target`` if free, otherwise ``<stem> (2).<ext>`` / (3) /
    (4) … until it doesn't collide with anything in ``taken``."""
    if target not in taken:
        return target
    if "." in target:
        stem, _, ext = target.rpartition(".")
        ext = f".{ext}"
    else:
        stem, ext = target, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}){ext}"
        if candidate not in taken:
            return candidate
        n += 1


# Strip suffixes that mark a file as a re-download / variant / part of
# the SAME canonical work:
#   "(N)"   — PikPak's auto-dedupe on download collision
#   "_N"    — our preferred multi-part convention (so we stay idempotent
#             once files have been renamed once)
#   quality tags — HD / 720p / 1080p / 高清 / …
#   "ch"    — Chinese-subbed scene tag (``SNOS-015ch`` → ``SNOS-015``).
#             Optional ``-`` / ``_`` separator before the marker.
# CD1/CD2 / variant letters A/B/C live on the BASE side of the regex
# and survive (they mark different content).
_DUP_SUFFIX_RE = re.compile(
    r"\s*(?:\(\d+\)|_\d+|HD|FHD|UHD|4K|2K|8K|720P|1080P|2160P|4320P|高清|超清|[-_]?CH)\s*$",
    re.IGNORECASE,
)

# BT-site naming conventions wrapped around the actual code. Strip
# these out of the canonical so e.g. ``[88K.ME]TRE-112-2.mp4`` and
# ``kfa55.com@TRE-112.mp4`` and ``TRE-112-2.mp4`` group together as the
# same code.
_BT_PREFIX_BRACKET_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
_BT_PREFIX_AT_RE = re.compile(r"^(?:[^@/\s]+@)+")
_BT_SUFFIX_TILDE_RE = re.compile(r"\s*~\s*[A-Z0-9._\-]+\s*$", re.IGNORECASE)


def _canonical_video_name(name: str) -> str:
    """Return ``name`` with extension + resolution / dup / part-index
    suffixes + BT-site prefixes/suffixes stripped, upper-cased. Files
    whose canonical form matches are treated as the same canonical
    work — they get grouped, large ones become parts, small ones get
    dropped."""
    stem = name
    # Strip extension once.
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", stem)
    if m:
        stem = stem[: m.start()]
    # Iteratively strip BT-site wrappers + resolution/dup suffixes until
    # nothing changes. Multiple passes catch combinations like
    # ``[88K.ME]TRE-112 (2) HD``.
    prev = None
    while prev != stem:
        prev = stem
        stem = _BT_PREFIX_BRACKET_RE.sub("", stem)
        stem = _BT_PREFIX_AT_RE.sub("", stem)
        stem = _BT_SUFFIX_TILDE_RE.sub("", stem)
        stem = _DUP_SUFFIX_RE.sub("", stem).strip()
    # Try to anchor on the JAV code, then strip any part marker hanging
    # off the end (CD<n> / -<n> / _<n> / lone variant letter). When the
    # code itself can't be extracted (e.g. ``CD3`` confuses the lookahead
    # in extract_jav_code), retry once with the CD<n> suffix removed.
    code = extract_jav_code(stem)
    if not code:
        stripped = re.sub(r"CD\d+\s*$", "", stem, flags=re.IGNORECASE)
        if stripped != stem:
            retry = extract_jav_code(stripped)
            if retry:
                code = retry
                stem = stripped
    if code:
        # ``\d{0,4}`` absorbs the BT numeric prefix that ``extract_jav_code``
        # strips from the canonical (``200GANA-3119`` → ``GANA-3119``).
        # Without it, the canonical for ``200GANA-3119_2.mp4`` would stay
        # as ``200GANA-3119`` and fail to group with ``GANA-3119.mp4``.
        tail_re = re.compile(
            rf"\d{{0,4}}{re.escape(code)}(?:CD\d+|-\d+|_\d+|[-_ ]?[A-Z](?![A-Za-z0-9]))?\s*$",
            re.IGNORECASE,
        )
        m = tail_re.search(stem)
        if m:
            stem = stem[: m.start()] + code
    return stem.upper()


def _part_marker_index(name: str, code: str) -> int:
    """1-based index of the multipart marker tucked next to ``code`` in
    ``name``: ``CD<n>`` / ``-<n>`` / ``_<n>`` / lone variant letter
    (``A``=1, ``B``=2 …). Returns 0 when no marker is found, so the
    bare-name file sorts first and becomes ``_1``."""
    stem = name
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", stem)
    if m:
        stem = stem[: m.start()]
    pattern = re.compile(
        rf"{re.escape(code)}(?:CD(\d+)|-(\d+)|_(\d+)|[-_ ]?([A-Z])(?![A-Za-z0-9]))",
        re.IGNORECASE,
    )
    m = pattern.search(stem)
    if not m:
        return 0
    if m.group(1):
        return int(m.group(1))
    if m.group(2):
        return int(m.group(2))
    if m.group(3):
        return int(m.group(3))
    if m.group(4):
        return ord(m.group(4).upper()) - ord("A") + 1
    return 0


def _dup_sort_index(name: str) -> int:
    """Extract the PikPak ``(N)`` suffix as an int; "no suffix" = 0.
    Used to order files within a multi-part group so the bare-name one
    becomes ``_1`` and ``(2)``/``(3)``/... follow naturally."""
    m = re.search(r"\((\d+)\)", name)
    return int(m.group(1)) if m else 0


_PART_INDEX_RE = re.compile(r"^(.+)_(\d+)$")


def _split_size_outliers(files: list, code: str) -> tuple[list, list]:
    """Split a same-canonical, all-substantial group into (parts,
    outliers). A stray whole-film low-res rip sitting next to real
    discs is ≥500MB too, so the substantial-size test alone lets it
    claim a ``_N`` slot (live case: a 1.44GB old rip became ``_5`` of a
    five-disc set while the real CD5 lost its slot). Members without a
    part marker whose size is under half the group median are outliers;
    marker-bearing files are always kept (a bonus disc can be small)."""
    if len(files) < 3:
        return files, []
    sizes = sorted(int(f.size or 0) for f in files)
    median = sizes[len(sizes) // 2]
    parts, outliers = [], []
    for f in files:
        if (int(f.size or 0) >= median * 0.5
                or _part_marker_index(f.name, code) > 0):
            parts.append(f)
        else:
            outliers.append(f)
    if len(parts) >= 2:
        return parts, outliers
    return files, []


def _build_video_rename_plan(
    children: list,  # list[PikPakFile]; type kept loose to avoid forward ref
    min_size: int,
    is_video_fn,
) -> tuple[dict[str, str], set[str]]:
    """Pre-scan video children and return ``(plan, group_members)``:

    - ``plan`` — ``{current_name: target_name}`` covering two corrections:

      1. **Lonely variant** — if a base code has ≤ 1 file with a trailing
         variant letter (``SDMM-14903A`` alone, no ``B`` companion), the
         letter is meaningless and gets stripped so the file becomes
         ``<base>.<ext>``.
      2. **Multi-part group** — when 2+ video files share a canonical and
         all of them are ≥ ``min_size``, rename them to
         ``<canonical>_N.<ext>`` (sorted by PikPak ``(N)`` suffix; bare
         name = 0 = ``_1``). Files already in this form keep their slot
         so re-running cleanup is a no-op.

    - ``group_members`` — every filename that belongs to a multi-part
      group (whether or not it's in ``plan``). The caller uses this to
      avoid blindly applying the single-file default name to a member
      that's already correctly named — without this guard, on a second
      run ``SDMM-053_1.mp4``, ``_2``, ``_3``, ``_4`` would all collapse
      to ``SDMM-053.mp4`` + ``(2)/(3)/(4)`` dedup suffixes.
    """
    # Pass 1: count files-with-variant per base code, so we know which
    # variants are "lonely" and should be stripped.
    variant_count: dict[str, int] = {}
    for c in children:
        if getattr(c, "kind", "") == "drive#folder" or not is_video_fn(c.name):
            continue
        base = extract_jav_code(c.name)
        if not base:
            continue
        full = extract_jav_code_full(c.name) or base
        if full != base:
            variant_count[base] = variant_count.get(base, 0) + 1

    # Pass 2: compute each file's effective canonical (variant possibly
    # stripped) and bucket files by it.
    file_effective: dict[str, str] = {}  # name → effective_full_code
    groups: dict[str, list] = {}
    for c in children:
        if getattr(c, "kind", "") == "drive#folder" or not is_video_fn(c.name):
            continue
        base = extract_jav_code(c.name) or ""
        full = extract_jav_code_full(c.name) or base
        is_lonely = bool(base) and full != base and variant_count.get(base, 0) <= 1
        effective = base if is_lonely else full
        file_effective[c.name] = effective
        canon = _canonical_video_name(c.name)
        if is_lonely and full and full.upper() in canon:
            canon = canon.replace(full.upper(), base.upper(), 1)
        groups.setdefault(canon, []).append(c)

    plan: dict[str, str] = {}
    group_members: set[str] = set()
    for canon, files in groups.items():
        if len(files) == 1:
            # Singleton: rename to the canonical name when the current
            # name carries BT-site noise / lonely variant / case shift.
            c = files[0]
            ext = ext_of(c.name)
            target = f"{canon}{ext}"
            if target != c.name:
                plan[c.name] = target
            continue
        # Multi-file group: multipart naming if all substantial.
        if not all((f.size or 0) >= min_size for f in files):
            continue
        # A stray low-res whole-film rip must not claim a part slot.
        files, _outliers = _split_size_outliers(files, canon)
        # Members get protected from the single-file default-name path.
        for f in files:
            group_members.add(f.name)
        used_indices: set[int] = set()
        unnamed: list = []
        for f in files:
            ext = ext_of(f.name)
            stem = f.name[: -len(ext)] if ext else f.name
            m = _PART_INDEX_RE.match(stem)
            if m and m.group(1).upper() == canon:
                used_indices.add(int(m.group(2)))
            else:
                unnamed.append(f)
        if not unnamed:
            continue  # already fully named
        # Sort by part marker (CD<n>/-<n>/letter) so the bare-name file
        # becomes ``_1`` and ``-2``/``CD2``/``B`` become ``_2`` etc.
        # Fall back to PikPak ``(N)`` suffix + filename for ties.
        unnamed.sort(
            key=lambda f: (
                _part_marker_index(f.name, canon),
                _dup_sort_index(f.name),
                f.name,
            )
        )
        for f in unnamed:
            marker = _part_marker_index(f.name, canon)
            # Marker-bearing files prefer their own index; bare ones
            # grab the next free slot. Collisions skip ahead.
            n = marker if marker > 0 else 1
            while n in used_indices:
                n += 1
            ext = ext_of(f.name)
            plan[f.name] = f"{canon}_{n}{ext}"
            used_indices.add(n)
    return plan, group_members
