"""The move-settle gate must survive restarts. It used to live only in
process memory with a blanket 30-min boot guard — and deploys (the rota
ships its own fixes) reset it so often that shell cleanups took 5
attempts to land. Persisted stamps ARE the pre-restart knowledge, so a
boot with a readable log skips the blanket guard; a boot without one
must still assume the worst."""

import json
import time

import app.services.pikpak as pk


def _svc(monkeypatch, tmp_path, name="move_sources.json"):
    monkeypatch.setattr(pk, "MOVE_SOURCES_FILE", tmp_path / name)
    return pk.PikPakService()


def test_first_boot_without_history_keeps_blanket_guard(monkeypatch, tmp_path):
    svc = _svc(monkeypatch, tmp_path)
    # No log file → unknown history → even never-seen ids stay gated.
    assert svc.move_settled("never-seen") is False
    assert svc._boot_guard_until > time.time()


def test_restart_with_log_gates_per_id_not_globally(monkeypatch, tmp_path):
    first = _svc(monkeypatch, tmp_path)
    first._boot_guard_until = 0.0
    first.record_move_source("hot-folder")

    # "Restart": a fresh service reads the log the first one wrote.
    second = _svc(monkeypatch, tmp_path)
    assert second.move_settled("hot-folder") is False   # still settling
    assert second.move_settled("other-folder") is True  # no blanket block


def test_settled_history_lifts_guard_entirely(monkeypatch, tmp_path):
    old = time.time() - pk.MOVE_SETTLE_SECONDS - 60
    (tmp_path / "move_sources.json").write_text(json.dumps({"done": old}))
    svc = _svc(monkeypatch, tmp_path)
    assert svc.move_settled("done") is True
    assert svc.move_settled("anything") is True


def test_corrupt_log_falls_back_to_blanket_guard(monkeypatch, tmp_path):
    (tmp_path / "move_sources.json").write_text("{not json")
    svc = _svc(monkeypatch, tmp_path)
    assert svc.move_settled("anything") is False


def test_disabled_persistence_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(pk, "MOVE_SOURCES_FILE", pk.Path(""))
    svc = pk.PikPakService()
    assert svc.move_settled("x") is False  # no history → guard holds
    svc.record_move_source("x")
    assert list(tmp_path.iterdir()) == []


def test_record_prunes_and_persists(monkeypatch, tmp_path):
    svc = _svc(monkeypatch, tmp_path)
    svc._move_sources["ancient"] = time.time() - 3 * pk.MOVE_SETTLE_SECONDS
    svc.record_move_source("fresh")
    on_disk = json.loads((tmp_path / "move_sources.json").read_text())
    assert "fresh" in on_disk
    assert "ancient" not in on_disk  # settled entries read same as absent
