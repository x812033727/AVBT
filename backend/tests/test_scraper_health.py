import app.services.scraper_health as sh
from app.scrapers.javbus import _looks_like_challenge


def _fresh(monkeypatch):
    """New instance + captured alerts (webhook queue stays untouched)."""
    health = sh.ScraperHealth()
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sh.ScraperHealth,
        "_alert",
        lambda self, key, message: sent.append((key, message)),
    )
    return health, sent


def _real_alert_capture(monkeypatch):
    """Keep real _alert (cooldown logic) but capture the webhook call."""
    from app.services import webhook_queue as wq

    sent: list[str] = []
    monkeypatch.setattr(
        wq.webhook_queue, "enqueue_nowait", lambda msg, event="generic": sent.append(msg)
    )
    return sh.ScraperHealth(), sent


def test_gid_missing_alerts_over_threshold(monkeypatch):
    health, sent = _real_alert_capture(monkeypatch)
    for _ in range(5):
        health.record_detail("ok_magnets")
    for _ in range(6):
        health.record_detail("gid_missing")
    assert len(sent) == 1
    assert "token" in sent[0]
    assert health.degraded()
    # Cooldown: further failures don't re-alert.
    for _ in range(5):
        health.record_detail("gid_missing")
    assert len(sent) == 1


def test_gid_missing_quiet_below_min_sample(monkeypatch):
    health, sent = _fresh(monkeypatch)
    for _ in range(9):  # < _GID_MIN_SAMPLE relevant events
        health.record_detail("gid_missing")
    assert sent == []


def test_ok_no_magnets_is_not_a_failure(monkeypatch):
    health, sent = _fresh(monkeypatch)
    for _ in range(50):
        health.record_detail("ok_no_magnets")
    assert sent == []
    assert not health.degraded()


def test_irrelevant_outcomes_do_not_dilute_ratio(monkeypatch):
    # errors are excluded from the gid ratio — 10 errors plus 10
    # gid_missing must still gid-alert (10/10, not 10/20). The errors
    # additionally trip their own error-rate alert; the gid alert must
    # still be present (non-dilution is the property under test).
    health, sent = _fresh(monkeypatch)
    for _ in range(10):
        health.record_detail("error")
    for _ in range(10):
        health.record_detail("gid_missing")
    assert "gid_missing" in [k for k, _ in sent]


def test_error_rate_alerts_over_threshold(monkeypatch):
    health, sent = _real_alert_capture(monkeypatch)
    for _ in range(5):
        health.record_detail("ok_magnets")
    for _ in range(6):
        health.record_detail("error")   # 6/11 > 0.5
    assert len(sent) == 1
    assert "封鎖" in sent[0] or "429" in sent[0]
    assert health.degraded()


def test_error_alert_quiet_below_min_sample(monkeypatch):
    health, sent = _fresh(monkeypatch)
    for _ in range(9):  # < _ERROR_MIN_SAMPLE
        health.record_detail("error")
    assert sent == []


def test_dead_code_empty_html_never_error_alerts(monkeypatch):
    # empty_html is JavBus answering "does not exist" for a dead code —
    # a full window of it must NOT look like an outage.
    health, sent = _fresh(monkeypatch)
    for _ in range(50):
        health.record_detail("empty_html")
    assert sent == []
    assert not health.degraded()


def test_listing_error_alerts(monkeypatch):
    health, sent = _real_alert_capture(monkeypatch)
    for _ in range(12):
        health.record_listing("error")
    assert len(sent) == 1          # cooldown dedups the repeated trips
    assert "列表" in sent[0]
    assert health.degraded()


def test_snapshot_exposes_limiter_spacing(monkeypatch):
    health, _ = _fresh(monkeypatch)
    snap = health.snapshot()
    assert "limiter" in snap
    # In the test process the shared limiter is importable; spacing keys
    # present and base is the floor.
    lim = snap["limiter"]
    assert lim is None or {"current_s", "base_s", "penalised"} <= lim.keys()


def test_challenge_alert_after_three_in_window(monkeypatch):
    health, sent = _real_alert_capture(monkeypatch)
    health.record_challenge()
    health.record_challenge()
    assert sent == []
    health.record_challenge()
    assert len(sent) == 1
    assert health.degraded()


def test_snapshot_shape():
    health = sh.ScraperHealth()
    health.record_detail("ok_magnets")
    health.record_listing("zero_items")
    snap = health.snapshot()
    assert snap["detail"]["counts"] == {"ok_magnets": 1}
    assert snap["listing"]["counts"] == {"zero_items": 1}
    assert snap["degraded"] is False
    assert snap["challenges_10m"] == 0


def test_challenge_marker_detection():
    assert _looks_like_challenge("<title>Just a moment...</title>")
    assert _looks_like_challenge('<div id="cf-browser-verification"></div>')
    assert not _looks_like_challenge("<html><body>ABP-123 高清</body></html>")
