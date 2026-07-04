import socket

import pytest

from app.routers.img import _host_allowed, _safe_url


async def test_scheme_rejected():
    assert await _safe_url("ftp://www.javbus.com/a.jpg") is False
    assert await _safe_url("file:///etc/passwd") is False


async def test_literal_private_ips_rejected():
    for url in (
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/x",
        "http://[::1]/x",
    ):
        assert await _safe_url(url) is False, url


async def test_literal_public_ip_rejected_by_allowlist():
    # Even a public literal IP can't match the domain allowlist.
    assert await _safe_url("http://93.184.216.34/x") is False


async def test_non_allowlisted_host_rejected():
    assert await _safe_url("https://evil.example.com/img.jpg") is False


async def test_allowlisted_host_with_public_dns(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert await _safe_url("https://www.javbus.com/pics/x.jpg") is True


async def test_allowlisted_host_resolving_private_rejected(monkeypatch):
    # DNS-rebinding style: allowlisted name pointing at a private address.
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert await _safe_url("https://www.javbus.com/pics/x.jpg") is False


async def test_mixed_resolution_rejected(monkeypatch):
    # One public + one private answer → reject (any bad address fails).
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert await _safe_url("https://www.javbus.com/pics/x.jpg") is False


async def test_resolution_failure_rejected(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert await _safe_url("https://www.javbus.com/pics/x.jpg") is False


@pytest.mark.parametrize(
    ("host", "want"),
    [
        ("javbus.com", True),          # suffix entry matches the bare domain
        ("images.javbus.com", True),   # ... and any subdomain
        ("notjavbus.com", False),      # no partial-string tricks
        ("javbus.com.evil.io", False),
        ("pics.dmm.co.jp", True),
    ],
)
def test_host_allowed(host, want):
    assert _host_allowed(host) is want
