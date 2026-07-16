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
#   quality tags — HD / SD / 720p / 1080p / 高清 / …
#   "ch"    — Chinese-subbed scene tag (``SNOS-015ch`` → ``SNOS-015``).
#             Optional ``-`` / ``_`` separator before the marker.
# CD1/CD2 / variant letters A/B/C live on the BASE side of the regex
# and survive (they mark different content). So does anything naming a
# different CUT rather than a different encode — ``-UNCENSORED`` /
# ``_UNC`` / ``-UC`` / ``-AI`` (AI-decensored). Stripping those would let
# an uncensored rip group with the censored retail release, and the
# dedupe keeps the BIGGER file — which is usually the censored one.
# Quality is same-content-worse; a cut is different content.
_DUP_SUFFIX_RE = re.compile(
    r"\s*(?:\(\d+\)|_\d+|HD|FHD|UHD|SD|4KS|4K|2K|8K|720P|1080P|2160P|4320P"
    r"|\((?:HD|FHD|UHD|SD|4KS|4K|2K|8K|720P|1080P|2160P|4320P)\)"
    r"|[-_.](?:H26[45]|X26[45]|HEVC|AV1)"
    r"|高清|超清|[-_]?CH)[-_ ]*$",
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
_SITE_TLDS = r"(?:COM|NET|ORG|CC|CO|ME|TV|XYZ|LA|CLUB|VIP|INFO)"
_BT_SUFFIX_DOMAIN_RE = re.compile(
    rf"[-_. ][A-Z0-9]+\.{_SITE_TLDS}$",
    re.IGNORECASE,
)
# Bare host at the HEAD, glued onto the code with no separator
# (``hhd800.comHRSM-130``, ``carib.com010112-123``). The ``@`` and
# bracket rules need a separator and the domain rules above are
# end-anchored, so nothing claimed this host: its tld fused onto the
# code instead (``COMHRSM-130``), which cost the code its presence
# entry. Anchored at the start and stopping at the tld, so a code-like
# head token whose tail isn't a real tld (``FC2.PPV-1234567``) is
# untouched. The optional separator lets the separated spellings share
# this one rule.
_BT_PREFIX_DOMAIN_RE = re.compile(
    rf"^(?:[A-Z0-9]+\.)+{_SITE_TLDS}[-_. ]?",
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
        m = _BT_PREFIX_DOMAIN_RE.match(stem)
        if m:
            site_labels.add(m.group(0).strip(" -_.").split(".")[0].upper())
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
    ``_1``. ``-<n>``/``_<n>`` cap at two digits and must not be glued to
    more alphanumerics, so a resolution tail (``-1080p``, ``-4K``) stays
    unmarked instead of claiming part slot 1080. Composite ``CD<n>-<letter>`` markers return the disc number;
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
        r"|CD(?P<cd>\d+)|HHB(?P<hhb>\d+)"
        r"|-(?P<dash>\d{1,2})(?![A-Za-z0-9])"
        r"|_(?P<us>\d{1,2})(?![A-Za-z0-9])"
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


# How close two runtimes must be to call them the same film, and how much
# smaller a same-length file must be before it is obviously a re-encode
# rather than a disc. Both gates must pass, and the size one carries the
# safety: OFJE-276's six real discs run 115-123 min — a 6.5% spread, only
# just outside the duration gate — but they are 5.1-5.5GB each, so the
# size gate would still spare them if their runtimes happened to cluster.
# Whereas GDHH-167's fake _5 is 195 min beside a 195-min _1 at a seventh
# of the size. Two files of the same length where one is under half the
# other is a bitrate difference, not a different disc.
_SAME_FILM_DURATION_TOLERANCE = 0.05
_COPY_MAX_SIZE_FRACTION = 0.5


def low_bitrate_copies(files: list) -> list:
    """Members that are the biggest file again, just re-encoded smaller.

    Returns [] unless every member's runtime is known — an unprobed file
    must never be judged, and a group where PikPak knows nothing tells us
    nothing. Judgement stays conservative on purpose: same-size copies
    (STOL-094: 239 min / 11.01GB beside 236 min / 10.65GB) are NOT
    reported, because at that point only a human can say whether the
    release really is two discs.
    """
    known = [f for f in files if getattr(f, "duration", 0) > 0]
    if len(known) != len(files) or len(files) < 2:
        return []
    biggest = max(files, key=lambda f: (f.size or 0))
    out = []
    for f in files:
        if f is biggest:
            continue
        same_length = (abs(f.duration - biggest.duration)
                       <= max(f.duration, biggest.duration)
                       * _SAME_FILM_DURATION_TOLERANCE)
        much_smaller = (f.size or 0) <= (biggest.size or 0) * _COPY_MAX_SIZE_FRACTION
        if same_length and much_smaller:
            out.append(f)
    return out


# The quality tags from _DUP_SUFFIX_RE, end-anchored on their own — used
# to recognise a file whose NAME declares it a re-encode. ``(N)`` / ``_N``
# / ``CH`` are deliberately absent: a dedupe or part suffix says nothing
# about the encode.
_QUALITY_TAG_RE = re.compile(
    r"(?:HD|FHD|UHD|SD|4KS|4K|2K|8K|720P|1080P|2160P|4320P"
    r"|[-_.](?:H26[45]|X26[45]|HEVC|AV1)"
    r"|高清|超清)[-_ ]*$",
    re.IGNORECASE,
)


def quality_tagged_copies(files: list, code: str) -> list:
    """Members whose own name declares a re-encode of ANOTHER member: the
    stem ends with a quality tag while the group also holds a different
    encode (a bare name, or a different tag).

    ``low_bitrate_copies`` needs every runtime probed and a ≤½ size gap;
    a same-torrent SD rip at 62% of the HD file (live: KBTK-012-SD,
    2.85GB beside 4.56GB, 2026-07-16) slips both gates and would claim a
    fake ``_2``. The name itself is evidence enough here: ``SD`` names an
    encode, not content, so the file must never be numbered as a disc —
    dropping it from the group hands it to the keep-the-biggest dedup.

    The "another encode" requirement carries the safety. When every
    member wears the SAME tag (``CODE-SD`` + ``CODE-SD (2)``) the group
    is one encode whose same-name collision means discs — judging those
    as copies would trash a real disc, so a single-tag group returns [].
    Marker-bearing names (``CODE-HD_2``) never read as tagged at all:
    the tag must be terminal, and a part index sits after it.
    """
    tags: dict[str, str | None] = {}
    for f in files:
        stem = f.name
        m = re.search(r"\.[A-Za-z0-9]{1,5}$", stem)
        if m:
            stem = stem[: m.start()]
        # A ``(N)`` collision suffix belongs to the whole name, not the
        # encode — look through it so both halves of a pair agree.
        stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
        m = _QUALITY_TAG_RE.search(stem)
        tags[f.name] = m.group(0).strip(" -_.").upper() if m else None
    if len(set(tags.values())) < 2:
        return []  # all bare, or all one encode — nothing to judge
    return [
        f for f in files
        if tags[f.name] is not None
        and _part_marker_index(f.name, code) == 0
    ]


def _build_video_rename_plan(
    children: list,  # list[PikPakFile]; type kept loose to avoid forward ref
    min_size: int,
    is_video_fn,
    *,
    require_marker: bool = False,
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

    ``require_marker`` — only treat a group as multi-part when a member
    actually carries a disc marker (``CD2``, ``-3``, ``_2_``, a variant
    letter). It exists because the bare ``CODE.mp4`` + ``CODE (2).mp4``
    shape means different things in different places:

    - inside one task's wrapper, two files claiming the same name came
      from the same torrent, so they are two discs (the default, False);
    - in a 系列 folder they arrived from separate downloads months apart
      and are two copies of the whole film. Every one of the 112 such
      pairs found on 2026-07-16 was a duplicate, and the ten whose
      duration PikPak knew were all full-length (SONE-092 153 min beside
      its 8.16GB twin; REBD-1013 184 min).

    Reading the second case as discs is not cosmetic: multi-part members
    are deliberately excluded from the dedup that would otherwise remove
    the loser, so both survive forever as a fake ``_1``/``_2`` pair.
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
        if require_marker and not any(
            _part_marker_index(f.name, canon) > 0 for f in files
        ):
            continue  # copies of one film, not discs — leave them to dedup
        # A marker can lie. GDHH-167_5 and CLUB-512_5 sat on disk as _N
        # for weeks; their runtimes matched _1 exactly and they were a
        # seventh of its size — re-encodes that a marker-only rule (and a
        # size-only rule) both waved through as discs. Drop them here so
        # the dedup can take them. Name-declared re-encodes (a trailing
        # quality tag) get the same treatment — they beat gates the
        # runtime rule can't judge (unprobed files, >½-size SD rips).
        copies = low_bitrate_copies(files)
        copies += [f for f in quality_tagged_copies(files, canon)
                   if f not in copies]
        if copies:
            files = [f for f in files if f not in copies]
            if len(files) < 2:
                continue  # nothing left to number — the dedup owns it
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
