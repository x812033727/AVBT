"""JAV code extraction for messy BT-downloaded filenames.

BT releases on PikPak tend to look like ``kfa55.com@483DAM-043`` or
``madoubt.com 252322.xyz DAM-057``. The cleanup feature needs to pull
just the code (``DAM-043`` / ``DAM-057``) out of those.

Some JAV labels — mostly Prestige amateur lines — legitimately include
a 3-digit prefix as part of the official code (``300MIUM``, ``259LUXU``,
``200GANA``, …). For everything else, a leading digit cluster is BT
release noise and must be stripped.
"""

from __future__ import annotations

import re

# Video extensions that BT release tools often append directly into the
# folder name itself (``SACE022MP4`` rather than ``SACE-022/SACE022.mp4``).
# Treated as part of the noise so the code extractor can see past it.
_FAKE_EXT_RE = r"(?:MP4|M4V|AVI|WMV|MKV|MOV|WEBM|FLV|TS|MPE?G|RMVB)"

# Chinese-subtitle scene tag: ``SNOS-015ch.mp4`` / ``SNOS-015-CH.mkv`` /
# ``SNOS-015_Ch.mp4`` all denote "same product, with Chinese subs glued
# on". JavBus indexes the base code only, so we strip the marker for
# both extraction and grouping — Chinese-subbed and raw versions collapse
# to the same canonical, just like resolution / dup variants do.
_CH_SUFFIX_RE = r"(?:[-_]?CH)?"

# Old-scene disc markers glued straight onto the code: ``SOE00829HHB3``
# (HHB release tag + disc number) and ``OFJE-296CD1-B`` (disc + sub-part
# letter). Without consuming these, the tail boundary check fails and
# the whole name reads as code-less.
_PART_SUFFIX_RE = r"(?:CD\d+(?:-[A-Z])?|HHB\d*)?"

_CODE_RE = re.compile(
    rf"(?:^|[^A-Z0-9])(\d{{0,4}}[A-Z]{{2,8}}-?\d{{2,6}})[A-Z]?{_PART_SUFFIX_RE}{_CH_SUFFIX_RE}{_FAKE_EXT_RE}?(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_SPLIT_RE = re.compile(r"(\d{0,4}[A-Z]{2,8})(\d{2,6})$", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^(\d{1,4})([A-Z]{2,8}-\d{2,6})$")

# Numeric prefixes (300MIUM, 259LUXU, 200GANA, …) are ALWAYS stripped
# from the canonical code: JavBus catalogs these without the prefix —
# e.g. the series listing for 60b shows MIUM-1098, not 300MIUM-1098 —
# so keeping them on the PikPak side meant the presence index never
# matched.
#
# The detail PAGE for these labels, however, often lives only under the
# prefixed id (``/259LUXU-1543``); the stripped code 404s. Series
# resolution (archiver / reorganize / pCloud organize) therefore goes
# through ``scraper.fetch_detail_resolved``, which falls back to a JavBus
# search to recover the prefixed id when the bare code's detail is empty —
# so these works still get archived under their series instead of stranded
# in the fallback bucket.

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv",
    ".ts", ".m4v", ".webm",
    # Legacy rip containers. Missing these is not cosmetic: finalize
    # permanently purges "non-video" files, so an unlisted container
    # (live near-miss: PPPD-539.mpg, 2026-07-15) is one cleanup pass
    # away from destruction. SACE-045's sace-045.asf (2026-07-15) sat
    # unrecognised for hours: finalize saw "no video" and spun on the
    # retry loop — one real .mp4 sibling away from a permanent purge.
    ".mpg", ".mpeg", ".rmvb", ".m2ts", ".vob",
    ".asf", ".rm", ".divx", ".ogm",
}


# Chinese labels for each tracked-listing kind. Used as the folder name
# under AVBT/ so users see ``AVBT/系列/回胴錄`` instead of
# ``AVBT/series/回胴錄`` — both the archiver and the missing-code service
# read from this same source so paths stay consistent.
KIND_LABELS_CH: dict[str, str] = {
    "star": "女優",
    "series": "系列",
    "studio": "製作商",
    "label": "發行商",
    "director": "導演",
    "genre": "類別",
}


# JavBus appends "<kind label> - 影片" to every listing page title — e.g.
# the series 11pb's H3 reads "回胴錄 - 系列 - 影片" instead of just
# "回胴錄". Strip that template suffix so archived folders are named with
# the bare title.
_LISTING_SUFFIX_RE = re.compile(
    r"\s*[-‐－–—]\s*(?:女優|系列|製作商|發行商|導演|類別)\s*[-‐－–—]\s*影片\s*$"
)


def clean_listing_name(name: str) -> str:
    """Strip JavBus' template suffix from a scraped listing title.
    Idempotent: passing an already-clean name returns it unchanged."""
    if not name:
        return ""
    return _LISTING_SUFFIX_RE.sub("", name.strip()).strip()


def ext_of(name: str) -> str:
    """Return the lower-cased dotted extension or empty string."""
    m = _EXT_RE.search(name or "")
    return m.group(0).lower() if m else ""


def _depad_number(raw: str) -> str:
    """Collapse DMM content-id zero padding: ``SOE-00480`` → ``SOE-480``.

    Only 5+ digit numerics with a leading zero are touched — that is the
    DMM squished style (``soe00480``). Shorter forms keep their zeros
    because labels like HEYZO catalog genuine leading-zero 4-digit codes
    (``HEYZO-0123``), and 2-3 digit codes are padded on purpose
    (``SONE-001``). Result is re-padded to at least 3 digits.
    """
    label, _, num = raw.partition("-")
    if num and len(num) >= 5 and num[0] == "0":
        return f"{label}-{num.lstrip('0').zfill(3)}"
    return raw


def extract_jav_code(name: str) -> str | None:
    """Return the canonical JAV code embedded in *name* (e.g. ``DAM-043``,
    ``300MIUM-1090``).

    Pipeline: strip extension → scan for every code-like substring → take
    the LAST match (real codes sit at the tail of dirty BT names) →
    upper-case → re-insert a hyphen if the form was squished
    (``483DAM043`` → ``483DAM-043``) → drop any leading digit cluster
    (``259LUXU-1543`` → ``LUXU-1543``).
    Returns None when nothing matches.

    A single trailing letter (``SDMM-14903C``, ``ABP-123A``) is allowed
    but stripped — JavBus catalogs the base code, so variant suffixes
    collapse to the same product for our purposes.
    """
    if not name:
        return None
    stem = _EXT_RE.sub("", name)
    matches = _CODE_RE.findall(stem)
    if not matches:
        return None
    raw = matches[-1].upper()
    if "-" not in raw:
        m = _SPLIT_RE.match(raw)
        if not m:
            return None
        raw = f"{m.group(1)}-{m.group(2)}"
    m = _PREFIX_RE.match(raw)
    if m:
        raw = m.group(2)
    return _depad_number(raw)


def is_video(name: str) -> bool:
    return ext_of(name) in VIDEO_EXTS


# Same shape as _CODE_RE but the trailing variant letter is captured
# instead of consumed by [A-Z]?. Used by extract_jav_code_full so file
# / folder names keep their variant suffix (SDMM-14903C, ABP-123A).
# The Chinese-sub marker (``ch``/``-ch``/``_ch``) is consumed but NOT
# captured — different sub languages aren't different products.
_CODE_RE_FULL = re.compile(
    rf"(?:^|[^A-Z0-9])(\d{{0,4}}[A-Z]{{2,8}}-?\d{{2,6}}[A-Z]?){_PART_SUFFIX_RE}{_CH_SUFFIX_RE}{_FAKE_EXT_RE}?(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)


def extract_jav_code_full(name: str) -> str | None:
    """Like :func:`extract_jav_code` but keeps the trailing variant
    letter when present (``SDMM-14903C`` stays as ``SDMM-14903C``).

    Use this when naming a folder or file on PikPak so multiple
    variants of the same base code (A/B/C…) can coexist as distinct
    files instead of colliding on the same target.
    """
    if not name:
        return None
    stem = _EXT_RE.sub("", name)
    matches = _CODE_RE_FULL.findall(stem)
    if not matches:
        return None
    raw = matches[-1].upper()
    # Split off any trailing variant letter so the existing
    # prefix/split helpers (which expect LABEL-NNN, no tail) still work.
    tail = ""
    if raw and raw[-1].isalpha() and "-" in raw:
        tail = raw[-1]
        raw = raw[:-1]
    if "-" not in raw:
        # Squished form: 483DAM043 (no hyphen) → re-split.
        if raw and raw[-1].isalpha():
            tail = raw[-1]
            raw = raw[:-1]
        m = _SPLIT_RE.match(raw)
        if not m:
            return None
        raw = f"{m.group(1)}-{m.group(2)}"
    m = _PREFIX_RE.match(raw)
    if m:
        raw = m.group(2)
    return _depad_number(raw) + tail


# Mirrored from archiver._safe_name so missing-code services can compute
# the archive path without importing the archiver (would cause a cycle).
_PATH_UNSAFE = re.compile(r'[/\\:<>*?|"\x00-\x1f]+')


def safe_folder_name(name: str, *, fallback: str = "") -> str:
    """Strip path-unsafe chars from a display name. Returns ``fallback``
    (or empty string) when the cleaned result is empty. Matches the
    archiver's sanitisation so frontends and missing-code services see
    the same path the archiver writes to."""
    cleaned = _PATH_UNSAFE.sub("", (name or "").strip()).strip()
    return cleaned[:64] or fallback


def normalize_code(s: str) -> str:
    """Canonicalise an already-clean JAV code (e.g. PikPak folder name or
    a JavBus code) to ``LABEL-NNN`` form.

    Reuses extract_jav_code so leading-zero / missing-hyphen variants all
    collapse to the same string. Returns '' when nothing parses.
    """
    code = extract_jav_code(s or "")
    return code or ""


# ---------- multi-part (分集) name heuristics ----------
#
# Used to flag magnets / file names that LOOK like one part of a
# multi-part release before anything is downloaded. Heuristic only —
# the magnet name doesn't have to reflect the torrent's real contents.
#
# ⚠️ A trailing ``-C`` / ``ch`` next to the code means Chinese SUBTITLE
# in JAV release names, not "part C" — it must never fire, so the code-
# anchored letter rule skips C entirely and subtitle tokens are stripped
# before matching.

_PART_GENERIC_RES = (
    re.compile(r"\b(CD ?\d{1,2})\b"),
    re.compile(r"\b(DIS[CK] ?\d{1,2})\b"),
    re.compile(r"\b(P(?:AR)?T[ ._-]?\d{1,2})\b"),
    re.compile(r"\b(VOL(?:UME)?\.? ?\d{1,3})\b"),  # weak: compilations also use Vol.N
)
_PART_CJK_RE = re.compile(r"([上中下](?:集|巻|卷)|[上中下]$)")


def detect_part_hint(name: str, code: str | None = None) -> str:
    """Return the multipart marker found in *name* ("CD2", "PART1",
    "上集", "-2", "B", …) or '' when the name looks like a single video.

    ``code`` anchors the tighter rules (``<code>-2`` / ``<code>B``); when
    omitted it is derived via extract_jav_code. Mirrors the marker forms
    ``_part_marker_index`` (services/pikpak.py) understands, plus the
    generic CD/part/vol/上中下 tokens seen in BT release names."""
    if not name:
        return ""
    stem = name
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", stem)
    # An all-digit tail is a part marker (vol.3), not an extension.
    if m and not m.group(1).isdigit():
        stem = stem[: m.start()]
    stem = stem.strip()
    if not stem:
        return ""
    up = stem.upper()
    code = (code or extract_jav_code(stem) or "").upper()

    if code:
        # Strip the Chinese-subtitle token glued to the code so it can't
        # feed the code-anchored rules below (ABC-123-C / ABC-123CH).
        up = re.sub(
            rf"({re.escape(code)})(?:[-_ ]?CH|-C)(?=$|[^A-Z0-9])",
            r"\1",
            up,
        )

    for pat in _PART_GENERIC_RES:
        m = pat.search(up)
        if m:
            return m.group(1)
    m = _PART_CJK_RE.search(stem)
    if m:
        return m.group(1)

    if code:
        # <code>CD2 / <code>-2 / <code>_2 — the digit lookahead keeps
        # resolution suffixes (-4K, -1080P) and dates from matching.
        m = re.search(
            rf"{re.escape(code)}(CD\d{{1,2}}|[-_][1-9])(?![0-9KP])", up
        )
        if m:
            return m.group(1)
        # <code>B — lone variant letter as part marker; C is excluded
        # (subtitle collision).
        m = re.search(rf"{re.escape(code)}([ABD-Z])(?=$|[^A-Z0-9])", up)
        if m:
            return m.group(1)
    return ""
