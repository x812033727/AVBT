from app.services.auto_backup import _copy_credentials_sync


def test_copies_existing_credentials_preserving_mode(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "auth_secret.txt").write_text("s3cret")
    (data / "auth_secret.txt").chmod(0o600)
    (data / "pikpak_token.txt").write_text("tok")
    # pcloud_token.json deliberately absent — must be skipped, not fail.
    backups = data / "backups"

    copied = _copy_credentials_sync(data, backups)

    assert copied == 2
    cred = backups / "credentials"
    assert (cred / "auth_secret.txt").read_text() == "s3cret"
    assert (cred / "auth_secret.txt").stat().st_mode & 0o777 == 0o600
    assert (cred / "pikpak_token.txt").exists()
    assert not (cred / "pcloud_token.json").exists()


def test_no_credentials_no_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    backups = data / "backups"
    assert _copy_credentials_sync(data, backups) == 0
    assert not (backups / "credentials").exists()


async def test_notify_settings_exposes_queue_counters():
    from app.routers.notify import get_settings

    payload = await get_settings()
    q = payload["queue"]
    assert {"pending", "sent", "failed", "dropped"} <= set(q)
    assert "scraper_alert" in payload["toggles"]
