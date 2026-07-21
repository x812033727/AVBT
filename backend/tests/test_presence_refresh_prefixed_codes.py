"""refresh_codes must locate prefixed codes via the raw detail-cache key.

The presence index keys on normalized codes (``300MIUM-1147`` →
``MIUM-1147``), but the movie-detail cache — the only way to find the
studio/series folder — is keyed by the code as submitted, prefix intact.
refresh_codes used to normalize FIRST and query the detail cache with
the normalized spelling, so every prefixed code whose path wasn't
already indexed came back empty: presence stayed blank and finalize
looped on 找不到歸檔資料夾 forever (live cases 300MIUM-1147 /
3DSVR-1981, 2026-07-20).
"""

from types import SimpleNamespace

import app.services.archiver as archiver_mod
import app.services.pikpak_presence as pp

SERIES_DIR = "AVBT/製作商/プレステージプレミアム/ぴえん"


def _index(monkeypatch):
    index = pp.PikPakPresenceIndex()
    index._codes = set()
    index._paths = {}

    async def noop_persist(code, paths):
        return None

    monkeypatch.setattr(index, "_persist_code", noop_persist)
    monkeypatch.setattr(pp.settings, "pikpak_archive_folder", "AVBT/已完成")
    return index


def _wire(monkeypatch, *, detail_keys, children):
    """Detail rows exist only under ``detail_keys`` (raw spellings)."""

    async def nested_dir(code, *, allow_fetch=False):
        return SERIES_DIR if code in detail_keys else None

    monkeypatch.setattr(archiver_mod, "studio_series_dir_for_code", nested_dir)

    async def fake_lookup(path):
        return "dir1" if path == SERIES_DIR else None

    async def fake_list_all(parent_id, *, cap):
        assert parent_id == "dir1"
        return [SimpleNamespace(id=f"f{i}", name=n)
                for i, n in enumerate(children)], False

    monkeypatch.setattr(pp.pikpak_service, "lookup_folder_id", fake_lookup)
    monkeypatch.setattr(pp.pikpak_service, "list_all_files", fake_list_all)


async def test_prefixed_code_found_via_raw_detail_key(monkeypatch):
    index = _index(monkeypatch)
    _wire(monkeypatch, detail_keys={"300MIUM-1147"},
          children=["MIUM-1147.mp4"])

    changed = await index.refresh_codes(["300MIUM-1147"])

    assert changed == 1
    assert index._paths["MIUM-1147"] == [f"{SERIES_DIR}/MIUM-1147.mp4"]


async def test_prefixed_code_matches_prefixed_wrapper_folder(monkeypatch):
    index = _index(monkeypatch)
    _wire(monkeypatch, detail_keys={"3DSVR-1981"}, children=["3dsvr-1981"])

    changed = await index.refresh_codes(["3DSVR-1981"])

    assert changed == 1
    assert index._paths["DSVR-1981"] == [f"{SERIES_DIR}/3dsvr-1981"]


async def test_normalized_only_caller_still_works(monkeypatch):
    index = _index(monkeypatch)
    _wire(monkeypatch, detail_keys={"MIUM-1147"}, children=["MIUM-1147.mp4"])

    changed = await index.refresh_codes(["MIUM-1147"])

    assert changed == 1
    assert index._paths["MIUM-1147"] == [f"{SERIES_DIR}/MIUM-1147.mp4"]
