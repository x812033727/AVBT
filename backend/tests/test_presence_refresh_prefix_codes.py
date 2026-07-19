"""refresh_codes must resolve the studio/series dir with the RAW code.

The index is keyed by normalized (prefix-stripped) codes, but the JavBus
detail cache behind ``studio_series_dir_for_code`` is keyed by the full
submitted code ("200GANA-3078"). Normalizing before the dir resolution
made every numeric-prefixed code a permanent cache miss: the flattened
``GANA-3078.mp4`` was never indexed, ``_already_flattened`` stayed False
and the task row churned 找不到歸檔資料夾 forever (live 2026-07-19:
200GANA-3040/3061/3072/3078, 300NTK-831).
"""

from types import SimpleNamespace

import app.services.archiver as archiver_mod
import app.services.pikpak_presence as pp

SERIES = "AVBT/製作商/ナンパTV/連れ込みSEX隠し撮り"


def _fresh_index(monkeypatch):
    index = pp.PikPakPresenceIndex()
    index._codes = set()
    index._paths = {}

    async def noop_persist(code, paths):
        return None

    monkeypatch.setattr(index, "_persist_code", noop_persist)
    return index


def _wire_series_fs(monkeypatch, *, detail_keys):
    """studio/series dir resolves only for codes in ``detail_keys`` —
    mirrors the detail cache holding the full submitted spelling."""

    async def fake_nested(code, *, allow_fetch=False):
        return SERIES if code in detail_keys else None

    async def fake_lookup(path):
        return "series" if path.strip("/") == SERIES else None

    async def fake_list_all(parent_id, *, cap):
        assert parent_id == "series"
        return [SimpleNamespace(id="v1", name="GANA-3078.mp4")], False

    monkeypatch.setattr(
        archiver_mod, "studio_series_dir_for_code", fake_nested
    )
    monkeypatch.setattr(pp.pikpak_service, "lookup_folder_id", fake_lookup)
    monkeypatch.setattr(pp.pikpak_service, "list_all_files", fake_list_all)
    monkeypatch.setattr(pp.settings, "pikpak_archive_folder", "AVBT/已完成")


async def test_prefixed_code_resolves_dir_via_raw_spelling(monkeypatch):
    index = _fresh_index(monkeypatch)
    _wire_series_fs(monkeypatch, detail_keys={"200GANA-3078"})

    changed = await index.refresh_codes(["200GANA-3078"])

    assert changed == 1
    # Indexed under the normalized key, found via the raw-code dir.
    assert index._paths["GANA-3078"] == [f"{SERIES}/GANA-3078.mp4"]
    assert index.paths_for("200GANA-3078") == [f"{SERIES}/GANA-3078.mp4"]


async def test_unprefixed_code_still_resolves_directly(monkeypatch):
    index = _fresh_index(monkeypatch)
    _wire_series_fs(monkeypatch, detail_keys={"GANA-3078"})

    changed = await index.refresh_codes(["GANA-3078"])

    assert changed == 1
    assert index._paths["GANA-3078"] == [f"{SERIES}/GANA-3078.mp4"]
