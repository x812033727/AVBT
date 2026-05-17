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


_CODE_RE = re.compile(
    r"(?:^|[^A-Z0-9])(\d{0,4}[A-Z]{2,8}-?\d{2,6})[A-Z]?(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_SPLIT_RE = re.compile(r"(\d{0,4}[A-Z]{2,8})(\d{2,6})$", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^(\d{1,4})([A-Z]{2,8}-\d{2,6})$")

# Numeric prefixes were once whitelisted as part of the canonical code
# (300MIUM, 259LUXU, 200GANA, …). In practice JavBus catalogs these
# without the prefix — e.g. the series listing for 60b shows MIUM-1098,
# not 300MIUM-1098 — so keeping them on the PikPak side meant the
# presence index never matched. Strip every leading digit cluster.
KNOWN_NUMERIC_PREFIXES: frozenset[str] = frozenset()

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv",
    ".ts", ".m4v", ".webm",
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
}


# JavBus appends "<kind label> - 影片" to every listing page title — e.g.
# the series 11pb's H3 reads "回胴錄 - 系列 - 影片" instead of just
# "回胴錄". Strip that template suffix so archived folders are named with
# the bare title.
_LISTING_SUFFIX_RE = re.compile(
    r"\s*[-‐－–—]\s*(?:女優|系列|製作商|發行商|導演)\s*[-‐－–—]\s*影片\s*$"
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


def extract_jav_code(name: str) -> str | None:
    """Return the canonical JAV code embedded in *name* (e.g. ``DAM-043``,
    ``300MIUM-1090``).

    Pipeline: strip extension → scan for every code-like substring → take
    the LAST match (real codes sit at the tail of dirty BT names) →
    upper-case → re-insert a hyphen if the form was squished
    (``483DAM043`` → ``483DAM-043``) → drop a leading digit cluster
    unless it belongs to a known numeric-prefix label.
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
    if m and m.group(1) not in KNOWN_NUMERIC_PREFIXES:
        return m.group(2)
    return raw


def is_video(name: str) -> bool:
    return ext_of(name) in VIDEO_EXTS


# Same shape as _CODE_RE but the trailing variant letter is captured
# instead of consumed by [A-Z]?. Used by extract_jav_code_full so file
# / folder names keep their variant suffix (SDMM-14903C, ABP-123A).
_CODE_RE_FULL = re.compile(
    r"(?:^|[^A-Z0-9])(\d{0,4}[A-Z]{2,8}-?\d{2,6}[A-Z]?)(?=$|[^A-Z0-9])",
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
    if m and m.group(1) not in KNOWN_NUMERIC_PREFIXES:
        return m.group(2) + tail
    return raw + tail


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
