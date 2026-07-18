"""Token-encode/persist failures used to vanish into a bare ``except
Exception: pass`` in two spots — a broken keyring/disk write left the
next restart doing a full credential re-login with no trace in the log.
Both call sites must at least warn."""

import app.services.pikpak as pikpak_mod
from app.services.pikpak import PikPakService


class _BoomOnEncode:
    def encode_token(self):
        raise RuntimeError("keyring locked")


class _BoomOnAttr:
    @property
    def encoded_token(self):
        raise RuntimeError("token property exploded")


def test_maybe_encode_token_warns_on_failure(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(pikpak_mod, "TOKEN_FILE", tmp_path / "token.txt")
    service = PikPakService.__new__(PikPakService)
    with caplog.at_level("WARNING"):
        service._maybe_encode_token(_BoomOnEncode())
    assert any(
        "PikPak token persist failed" in r.message for r in caplog.records
    )


async def test_on_token_refresh_warns_on_failure(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(pikpak_mod, "TOKEN_FILE", tmp_path / "token.txt")
    service = PikPakService.__new__(PikPakService)
    with caplog.at_level("WARNING"):
        await service._on_token_refresh(_BoomOnAttr())
    assert any(
        "PikPak token persist failed" in r.message for r in caplog.records
    )
