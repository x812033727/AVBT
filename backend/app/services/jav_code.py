"""JAV code extraction for messy BT-downloaded filenames.

BT releases on PikPak tend to look like ``kfa55.com@483DAM-043`` or
``madoubt.com 252322.xyz DAM-057``. The cleanup feature needs to pull
just the code (``483DAM-043`` / ``DAM-057``) out of those.
"""

from __future__ import annotations

import re


_CODE_RE = re.compile(
    r"(?:^|[^A-Z0-9])(\d{0,4}[A-Z]{2,8}-?\d{2,6})(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_SPLIT_RE = re.compile(r"(\d{0,4}[A-Z]{2,8})(\d{2,6})$", re.IGNORECASE)

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv",
    ".ts", ".m4v", ".webm",
}


def ext_of(name: str) -> str:
    """Return the lower-cased dotted extension or empty string."""
    m = _EXT_RE.search(name or "")
    return m.group(0).lower() if m else ""


def extract_jav_code(name: str) -> str | None:
    """Return the canonical JAV code embedded in *name* (e.g. ``483DAM-043``).

    Algorithm: strip extension → scan for every code-like match → take the
    LAST one (real codes usually sit at the tail of dirty BT names) →
    upper-case → re-insert a hyphen if PikPak gave us the squished form.
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
    return raw


def is_video(name: str) -> bool:
    return ext_of(name) in VIDEO_EXTS
