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
    r"\s*(?:\(\d+\)|_\d+|HD|FHD|UHD|4K|2K|8K|720P|1080P|2160P|4320P"
    r"|\((?:HD|FHD|UHD|4K|2K|8K|720P|1080P|2160P|4320P)\)"
    r"|[-_](?:H26[45]|X26[45]|HEVC|AV1)"
    r"|高清|超清|[-_]?CH)\s*$",
    re.IGNORECASE,
)

# BT-site naming conventions wrapped around the actual code. Strip
# these out of the canonical so e.g. ``[88K.ME]TRE-112-2.mp4`` and
# ``kfa55.com@TRE-112.mp4`` and ``TRE-112-2.mp4`` group together as the
# same code.
_BT_PREFIX_BRACKET_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
_BT_PREFIX_AT_RE = re.compile(r"^(?:[^@/\s]+@)+")
_BT_SUFFIX_TILDE_RE = re.compile(r"\s*~\s*[A-Z0-9._\-]+\s*$", re.IGNORECASE)
# Site-domain tail glued onto the stem (``OAE-314(4K)-WWW.52IV.NET``).
_BT_SUFFIX_WWW_RE = re.compile(r"[-_ ]WWW\.[A-Z0-9][A-Z0-9.\-]*$", re.IGNORECASE)
# Bare ``<token>.<tld>`` tail without the WWW. prefix
# (``300MIUM-1270-UNCENSORED-NYAP2P.COM``). The token body excludes
# ``-`` and ``.`` on purpose: a greedy class with ``-`` would swallow
# code and title text in front of the domain (live near-miss: the
# repair script's first draft turned ``300MIUM-1270-UNCENSORED-NYAP2P
# .COM`` into ``300MIUM``). Only the final token pair is site noise.
_BT_SUFFIX_DOMAIN_RE = re.compile(
    r"[-_. ][A-Z0-9]+\.(?:COM|NET|ORG|CC|CO|ME|TV|XYZ|LA|CLUB|VIP|INFO)$",
    re.IGNORECASE,
)

# Some releases glue a literal extension token onto the stem without a
# dot (``HUNTA578AMP4.mp4`` = HUNTA-578 disc A + "MP4"). Only a token
# fused directly onto an alphanumeric survives the real-extension strip
# above; a space-separated trailing word stays (could be title text).
_GLUED_EXT_RE = re.compile(
    r"(?<=[0-9A-Za-z])(?:MP4|MKV|AVI|WMV|MPG|MPEG|MOV|M4V|FLV|RMVB)\s*$",
    re.IGNORECASE,
)
# Leading parenthesised tag groups (``(Hunter)(HUNTA-398)<title>``).
_HEAD_PAREN_RE = re.compile(r"^\s*(?:\(([^)]*)\)|\[([^\]]*)\])\s*")


def _flex_code_re(code: str) -> str:
    """Regex source matching ``code`` while tolerating the separator BT
    names routinely drop or swap: ``HUNTA-578`` must also match the
    ``HUNTA578`` / ``HUNTA_578`` spellings, otherwise the code-anchored
    canonical/marker logic goes blind and multi-disc sets fall back to
    the single-winner dedup (live loss: HUNTA-578 disc B trashed as a
    "duplicate" of disc A, 2026-07-14). ``0*`` additionally tolerates
    DMM content-id zero padding (``SOE-829`` ↔ ``SOE00829``)."""
    return re.escape(code).replace(r"\-", "[-_ ]?0*")


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
    site_labels: set[str] = set()
    prev = None
    while prev != stem:
        prev = stem
        m = _BT_PREFIX_BRACKET_RE.match(stem)
        if m:
            site_labels.add(m.group(0).strip(" []").split(".")[0].upper())
            stem = stem[m.end():]
        m = _BT_PREFIX_AT_RE.match(stem)
        if m:
            for part in m.group(0).rstrip("@").split("@"):
                site_labels.add(part.split(".")[0].upper())
            stem = stem[m.end():]
        stem = _BT_SUFFIX_TILDE_RE.sub("", stem)
        stem = _BT_SUFFIX_WWW_RE.sub("", stem)
        stem = _BT_SUFFIX_DOMAIN_RE.sub("", stem)
        stem = _GLUED_EXT_RE.sub("", stem)
        # Release-group tag glued on the tail that mirrors a site prefix
        # stripped from THIS name (``gg5.co@435MFC-248-C_GG5``): it hides
        # the code from the end-anchored match below exactly like the
        # WWW.* tails. An unmatched tail token could be real title text
        # and stays; single-letter labels are skipped so a variant/disc
        # letter can never be mistaken for a site tag.
        for label in site_labels:
            if len(label) >= 2:
                stem = re.sub(rf"[-_ ]{re.escape(label)}$", "", stem,
                              flags=re.IGNORECASE)
        # ``DVDMS-445A..mp4``-style doubled dots leave a trailing ``.``
        # after the extension strip; it hides the code from the
        # end-anchored match below and never carries meaning by itself.
        stem = _DUP_SUFFIX_RE.sub("", stem).strip(" .")
    # Try to anchor on the JAV code, then strip any part marker hanging
    # off the end (CD<n> / -<n> / _<n> / lone variant letter). When the
    # code itself can't be extracted (e.g. ``CD3`` confuses the lookahead
    # in extract_jav_code), retry once with the CD<n> suffix removed.
    code = extract_jav_code(stem)
    if not code:
        stripped = re.sub(
            r"(?:CD\d+|[. _-]?PART\d+_?)\s*$", "", stem, flags=re.IGNORECASE
        )
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
        # ``[. _-]?PART\d+_?`` — VR releases split as ``KAVR-497.PART1_``
        # (trailing underscore included); the trailing ``[-_]?`` tolerates
        # a dangling separator left by a stripped tail (``FUN2048.COM -
        # AP752-``). Both live cases 2026-07-15.
        tail_re = re.compile(
            rf"\d{{0,4}}{_flex_code_re(code)}(?:[. _-]?PART\d+_?|CD\d+(?:-[A-Z])?|HHB\d*|-\d+|_\d+|[-_ ]?[A-Z](?![A-Za-z0-9]))?[-_]?\s*$",
            re.IGNORECASE,
        )
        m = tail_re.search(stem)
        if m:
            # The stem ENDS with the code (+part marker) — whatever sits
            # in front of it is site noise the bracket/at regexes don't
            # catch (``HD-DVDMS-475``, ``139_3XPLANET_TRE-016``). The
            # code alone IS the canonical; a stem where the code is
            # mid-name (``MIDV-001 making-of``) falls through untouched.
            stem = code
        else:
            # ``(Hunter)(HUNTA-398)<long title>`` — the title tail keeps
            # the anchored match above from firing. A LEADING paren/
            # bracket group whose content alone yields the same code is
            # an explicit code tag, so the code IS the canonical. A
            # free-text mid-name code (``MIDV-001 making-of``) has no
            # such tag and stays untouched.
            head = stem
            while True:
                m2 = _HEAD_PAREN_RE.match(head)
                if not m2:
                    break
                if extract_jav_code(m2.group(1) or m2.group(2) or "") == code:
                    stem = code
                    break
                head = head[m2.end():]
    return stem.upper()


def _part_marker_index(name: str, code: str) -> int:
    """1-based index of the multipart marker tucked next to ``code`` in
    ``name``: ``CD<n>`` / ``HHB<n>`` (old-scene disc tag) / ``-<n>`` /
    ``_<n>`` / lone variant letter (``A``=1, ``B``=2 …). Returns 0 when
    no marker is found, so the bare-name file sorts first and becomes
    ``_1``. Composite ``CD<n>-<letter>`` markers return the disc number;
    same-disc sub-parts tie-break alphabetically via the caller's name
    sort and claim consecutive slots."""
    if not code:
        return 0
    stem = name
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", stem)
    if m:
        stem = stem[: m.start()]
    stem = _GLUED_EXT_RE.sub("", stem)
    pattern = re.compile(
        rf"{_flex_code_re(code)}"
        r"(?:[. _-]?PART(?P<part>\d+)_?"
        r"|CD(?P<cd>\d+)|HHB(?P<hhb>\d+)|-(?P<dash>\d+)|_(?P<us>\d+)"
        r"|[-_ ]?(?P<letter>[A-Z])(?![A-Za-z0-9]))",
        re.IGNORECASE,
    )
    m = pattern.search(stem)
    if not m:
        # Some releases hang the part index off the END of a long title
        # (``(Hunter)(HUNTA-398)<title>_2``) where it isn't adjacent to
        # the code. Trust a small trailing index only — a year or
        # resolution tail (``..._2024``) must not claim a part slot.
        m2 = re.search(r"(?:CD|PART|[-_])(\d{1,2})_?\s*$", stem, re.IGNORECASE)
        if m2:
            return int(m2.group(1))
        return 0
    for key in ("part", "cd", "hhb", "dash", "us"):
        if m.group(key):
            return int(m.group(key))
    if m.group("letter"):
        return ord(m.group("letter").upper()) - ord("A") + 1
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
        # Marker-bearing files (CD<n>/-<n>/letter) go first so each can
        # claim its own slot; bare-name files fill the gaps afterwards.
        # In an all-bare group (PikPak ``(N)`` dedup convention) the bare
        # file still becomes ``_1`` and ``(2)``/``(3)`` follow. In a
        # mixed group a stray bare file (old whole-film rip) can no
        # longer shift every real disc up by one slot.
        unnamed.sort(
            key=lambda f: (
                _part_marker_index(f.name, canon) == 0,
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
