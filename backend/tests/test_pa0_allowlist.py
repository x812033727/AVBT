"""PA0 (digit-in-label) allowlist: JavBus catalogs PA0-xxx (verified
live 2026-07-18: /PA0-010 has a real title; /PAO-010 404s) but
_CODE_RE's [A-Z]{2,8} label can never match it — those wrappers were
unarchivable-by-name and invisible to presence. Allowlist exactly PA0;
nothing else loosens."""

from app.services.jav_code import extract_jav_code, extract_jav_code_full, normalize_code


def test_pa0_extracts():
    assert extract_jav_code("PA0-010") == "PA0-010"
    assert extract_jav_code("pa0-007 title") == "PA0-007"
    assert extract_jav_code_full("[88K.ME]PA0-010.mp4") == "PA0-010"
    assert normalize_code("pa0-010") == "PA0-010"
    # Real-world wrappers carry a numeric prefix (483PA0-xxx) — the
    # canonical policy strips it (JavBus catalogs /PA0-010 unprefixed).
    assert extract_jav_code("483PA0-009") == "PA0-009"
    assert extract_jav_code("dccdom.com@483PA0-010") == "PA0-010"
    assert extract_jav_code("第一會所新片@SIS001@483PA0-009") == "PA0-009"


def test_pa0_boundary_not_loosened():
    # Mid-token PA0 must not match (boundary requires non-alnum before).
    assert extract_jav_code("ALPA0-010") is None
    # Other digit-labels stay unparseable (allowlist is literal).
    assert extract_jav_code("XY9-123") is None


def test_existing_classes_byte_identical():
    assert extract_jav_code("SOE00829HHB3") == "SOE-829"
    assert extract_jav_code("300MIUM-1098") == "MIUM-1098"
    assert extract_jav_code_full("OFJE-296CD1-B.mp4") is not None


def test_unhyphenated_pa0_glue_stays_unparsed():
    # The no-hyphen splitter deliberately excludes PA0: this shape was a
    # ghost vector (PA0+exactly-6-digits) and no real input is unhyphenated.
    assert extract_jav_code("PA0123456") is None
    assert extract_jav_code("483PA0123456") is None
