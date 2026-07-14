"""Pre-download heuristic: is a title likely multi-part (分集/多碟)?

Before anything is downloaded there is no authoritative part count —
JavBus doesn't publish one and magnet names are often just the bare code
(S1/OFJE box sets are the worst offenders: a 471-minute compilation whose
magnets are named plain "OFJE-585"). So we guess from the signals we do
have, in priority order: an explicit part marker in a magnet name, then
the running time (the strongest discriminator for the marker-less
majority), with the largest magnet's size as a corroborating hint.

This is deliberately conservative (bias toward *not* over-claiming
"multi") because the authoritative post-download ``video_count`` corrects
any miss. See ``estimate_multipart``.
"""
from __future__ import annotations

import re

from ..schemas import MovieDetail, PartEstimate

# Typical single JAV runs 90–180 min; long single works rarely exceed
# ~240. ≥300 leaves headroom so a 3-hour single doesn't false-positive
# while still catching 6–8h compilations (the real multi-disc case).
MULTI_DURATION_MIN = 300
# Middle band: long-ish runtime that only implies multi when paired with
# an unusually large file (stacked discs), not on its own.
LONG_DURATION_MIN = 180
# A genuine single HD film is ~5–8GB; ≥15GB alongside 3h+ implies discs.
LARGE_GB = 15.0

_INT_RE = re.compile(r"\d{1,4}")

# Marker triage. BT sites glue single letters onto codes as site /
# leak tags ("HMN-875J") — live sampling showed the majority of letter
# hints are noise, not disc 10. Only A/B/D read as genuine variant
# letters (C is already excluded at detection time — subtitle
# collision), and even those need a companion to be convincing.
_MARKER_NUM_RE = re.compile(r"(\d{1,2})")
_CJK_MARKER_IDX = {"上": 1, "中": 2, "下": 3}
_VARIANT_LETTERS = {"A", "B", "D"}


def _significant_markers(markers: list[str]) -> list[str]:
    """The subset of ``markers`` that genuinely evidences a multi-part
    release: a numeric/CJK part index ≥2, two distinct numeric indices
    (CD1+CD2), or two distinct variant letters (A+B). A lone ``CD1`` /
    ``-1`` / single letter proves nothing — re-encodes and site tags
    look exactly like that."""
    by_num: dict[int, str] = {}
    by_letter: dict[str, str] = {}
    for raw in markers:
        m = _MARKER_NUM_RE.search(raw)
        if m:
            by_num.setdefault(int(m.group(1)), raw)
            continue
        cjk = next((v for k, v in _CJK_MARKER_IDX.items() if k in raw), None)
        if cjk is not None:
            by_num.setdefault(cjk, raw)
            continue
        u = raw.strip().upper()
        if len(u) == 1 and u in _VARIANT_LETTERS:
            by_letter.setdefault(u, raw)
    if any(n >= 2 for n in by_num) or len(by_num) >= 2:
        return [by_num[n] for n in sorted(by_num)]
    if len(by_letter) >= 2:
        return [by_letter[c] for c in sorted(by_letter)]
    return []


def parse_duration_minutes(duration: str) -> int | None:
    """``"471分鐘"`` / ``"471 min"`` → 471; empty / no digits → None.

    Takes the first integer run. JavBus uses the ``N分鐘`` form; the
    ``N時間M分`` form (rare here) would yield the hour number — a known,
    accepted limitation (it only under-estimates, never over-claims)."""
    if not duration:
        return None
    m = _INT_RE.search(duration)
    return int(m.group()) if m else None


def estimate_multipart(detail: MovieDetail) -> PartEstimate:
    """Heuristic pre-download multipart guess. Reuses the magnet
    ``part_hint`` (already computed at scrape time) and ``parse_size``;
    never fetches or downloads anything."""
    # Local import mirrors the repo's avoid-cycle pattern: the scraper
    # imports jav_code, this module imports the scraper, the scraper never
    # imports this module (only the router does).
    from ..scrapers.javbus import parse_size

    duration_min = parse_duration_minutes(detail.duration)
    markers = list(dict.fromkeys(m.part_hint for m in detail.magnets if m.part_hint))
    max_bytes = max((parse_size(m.size) for m in detail.magnets), default=0.0)
    max_gb = round(max_bytes / (1024 ** 3), 2) if max_bytes > 0 else None

    def out(likely: str, reason: str) -> PartEstimate:
        return PartEstimate(
            likely=likely,
            reason=reason,
            duration_min=duration_min,
            part_markers=markers,
            max_size_gb=max_gb,
        )

    # 1. A *significant* marker combination is the cleanest signal and
    #    overrides a short runtime (a real 2-disc release can be
    #    ~120/disc). Lone letters / lone part-1 markers are BT-site
    #    noise ("HMN-875J") and fall through to the duration rules.
    significant = _significant_markers(markers)
    if significant:
        return out("multi", f"磁力名稱含分集標記({', '.join(significant)})")
    # 2. No runtime → don't guess from size alone (a single 4K remux can be
    #    20GB). Authoritative count will settle it after download.
    if duration_min is None:
        return out("unknown", "無片長資訊,無法估計分集")
    # 3. Very long → almost always a multi-disc compilation.
    if duration_min >= MULTI_DURATION_MIN:
        return out("multi", f"片長 {duration_min} 分鐘,通常為多碟合集")
    # 4. Long-ish AND a large file → stacked discs.
    if duration_min >= LONG_DURATION_MIN and max_gb is not None and max_gb >= LARGE_GB:
        return out("multi", f"片長 {duration_min} 分鐘且最大檔約 {max_gb:g}GB,可能為分集")
    # 5. Long-ish but no size corroboration → treat as single.
    if duration_min >= LONG_DURATION_MIN:
        return out("single", f"片長 {duration_min} 分鐘,略長但無其他分集跡象")
    # 6. Normal single length.
    return out("single", f"片長 {duration_min} 分鐘,為單片常見長度")
