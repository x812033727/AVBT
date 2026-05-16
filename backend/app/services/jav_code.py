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
    r"(?:^|[^A-Z0-9])(\d{0,4}[A-Z]{2,8}-?\d{2,6})(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_SPLIT_RE = re.compile(r"(\d{0,4}[A-Z]{2,8})(\d{2,6})$", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^(\d{1,4})([A-Z]{2,8}-\d{2,6})$")

# Numeric prefixes that are legitimate parts of a JAV studio label
# (Prestige amateur lines and similar). Keys are the digit clusters as
# strings; anything not in here is treated as BT noise and stripped.
KNOWN_NUMERIC_PREFIXES: frozenset[str] = frozenset({
    "200", "221", "230", "259", "261", "277",
    "300", "326", "345", "348", "358",
    "390", "408", "418", "432", "451", "463", "477", "498",
})

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv",
    ".ts", ".m4v", ".webm",
}


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
