"""refresh_codes must record paths under the on-disk folder spelling.

``lookup_folder_id`` silently follows spacing drift to the twin folder,
so a stale spelling in the JavBus detail cache (or in the index's own
known-parents list) used to mint a phantom second presence row for the
same physical file — and the phantom then seeded the next refresh's
candidate dirs, self-perpetuating (live 2026-08-04: SNIS-494 indexed
under both 新人NO.1STYLE and 新人NO.1 STYLE for one 23.8GB iso).
"""

from types import SimpleNamespace

import app.services.archiver as archiver_mod
import app.services.pikpak_presence as pp

STUDIO = "AVBT/製作商/エスワンナンバーワンスタイル"
DRIFTED = f"{STUDIO}/新人NO.1STYLE"
ON_DISK = f"{STUDIO}/新人NO.1 STYLE"


def _fresh_index(monkeypatch):
    index = pp.PikPakPresenceIndex()
    index._codes = set()
    index._paths = {}

    async def noop_persist(code, paths):
        return None

    monkeypatch.setattr(index, "_persist_code", noop_persist)
    return index


def _wire_drifted_fs(monkeypatch, *, canonical_calls=None, listed=None):
    """JavBus cache still says the DRIFTED spelling; only ON_DISK exists.

    ``canonical_path`` maps any drifted spelling to the on-disk twin,
    exactly like the real segment-rewriting walk would.
    """

    async def fake_nested(code, *, allow_fetch=False):
        return DRIFTED if code == "SNIS-494" else None

    async def fake_canonical(path):
        if canonical_calls is not None:
            canonical_calls.append(path)
        return ON_DISK if path.replace(" ", "") == DRIFTED else path

    async def fake_lookup(path, *, strict=False):
        return "series" if path.strip("/") == ON_DISK else ""

    async def fake_list_all(parent_id, *, cap):
        assert parent_id == "series"
        if listed is not None:
            listed.append(parent_id)
        return [SimpleNamespace(id="iso1", name="SNIS-494.iso")], False

    monkeypatch.setattr(
        archiver_mod, "studio_series_dir_for_code", fake_nested
    )
    monkeypatch.setattr(pp.pikpak_service, "canonical_path", fake_canonical)
    monkeypatch.setattr(pp.pikpak_service, "lookup_folder_id", fake_lookup)
    monkeypatch.setattr(pp.pikpak_service, "list_all_files", fake_list_all)
    monkeypatch.setattr(pp.settings, "pikpak_archive_folder", "AVBT/已完成")


async def test_drifted_spelling_collapses_to_one_on_disk_row(monkeypatch):
    """A stale drifted row + a drifted JavBus dir both resolve to the
    on-disk twin — the refresh must end with ONE row, spelled as on disk."""
    index = _fresh_index(monkeypatch)
    index._codes = {"SNIS-494"}
    index._paths = {
        "SNIS-494": [f"{DRIFTED}/SNIS-494.iso", f"{ON_DISK}/SNIS-494.iso"]
    }
    _wire_drifted_fs(monkeypatch)

    changed = await index.refresh_codes(["SNIS-494"])

    assert changed == 1
    assert index._paths["SNIS-494"] == [f"{ON_DISK}/SNIS-494.iso"]


async def test_twin_spellings_listed_once(monkeypatch):
    """Both spellings canonicalize to the same dir — it is listed once,
    not once per spelling."""
    index = _fresh_index(monkeypatch)
    index._codes = {"SNIS-494"}
    index._paths = {"SNIS-494": [f"{DRIFTED}/SNIS-494.iso"]}
    listed: list[str] = []
    _wire_drifted_fs(monkeypatch, listed=listed)

    changed = await index.refresh_codes(["SNIS-494"])

    assert changed == 1
    assert index._paths["SNIS-494"] == [f"{ON_DISK}/SNIS-494.iso"]
    assert listed == ["series"]


async def test_canonical_flake_falls_back_to_caller_spelling(monkeypatch):
    """canonical_path is best-effort: when it raises, the refresh keeps
    the caller's spelling instead of dropping the dir as unchecked."""
    index = _fresh_index(monkeypatch)
    _wire_drifted_fs(monkeypatch)

    async def boom(path):
        raise RuntimeError("walk flaked")

    async def fake_lookup(path, *, strict=False):
        return "series" if path.strip("/") == DRIFTED else ""

    monkeypatch.setattr(pp.pikpak_service, "canonical_path", boom)
    monkeypatch.setattr(pp.pikpak_service, "lookup_folder_id", fake_lookup)

    changed = await index.refresh_codes(["SNIS-494"])

    assert changed == 1
    assert index._paths["SNIS-494"] == [f"{DRIFTED}/SNIS-494.iso"]
