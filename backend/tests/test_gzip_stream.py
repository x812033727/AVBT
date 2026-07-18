"""StreamAwareGZipMiddleware: compress bulk JSON, never buffer NDJSON.

#228 added a global GZipMiddleware whose streaming branch buffers every
chunk in one GzipFile with no per-chunk flush, freezing the live
progress modals fed by ~19 application/x-ndjson endpoints. These tests
pin that a streaming content type passes through un-gzipped (bytes reach
the client as produced) while a large ordinary JSON body is still
compressed exactly as before.
"""

import gzip
import json

from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.gzip_stream import StreamAwareGZipMiddleware

# Well over the 8 KiB floor so the ordinary-JSON case is a real compress.
_BIG = json.dumps({"items": ["x" * 100 for _ in range(500)]}).encode()


async def _big_json(request):
    return Response(_BIG, media_type="application/json")


async def _ndjson(request):
    async def gen():
        for i in range(5):
            yield (json.dumps({"i": i}) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


def _client():
    app = Starlette(routes=[
        Route("/big", _big_json),
        Route("/stream", _ndjson),
    ])
    app.add_middleware(StreamAwareGZipMiddleware, minimum_size=8 * 1024)
    return TestClient(app)


def test_bulk_json_is_gzipped():
    r = _client().get("/big", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    # httpx transparently decodes; confirm the body round-trips.
    assert json.loads(r.content)["items"][0] == "x" * 100


def test_ndjson_stream_not_gzipped():
    r = _client().get("/stream", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") != "gzip"
    lines = [ln for ln in r.text.splitlines() if ln]
    assert [json.loads(ln)["i"] for ln in lines] == [0, 1, 2, 3, 4]


def test_ndjson_bytes_are_not_gzip_encoded():
    # The raw bytes must be the plain NDJSON, not a gzip stream — proof
    # nothing buffered/compressed the feed.
    r = _client().get("/stream", headers={"Accept-Encoding": "gzip"})
    assert not r.content.startswith(b"\x1f\x8b")  # gzip magic
    # And it is emphatically not decodable as gzip.
    try:
        gzip.decompress(r.content)
        raise AssertionError("stream body was gzip-compressed")
    except (OSError, EOFError):
        pass


def test_no_gzip_accept_encoding_leaves_everything_plain():
    # A client that does not accept gzip gets the raw body (the base
    # middleware's Accept-Encoding gate, preserved by the subclass).
    r = _client().get("/big", headers={"Accept-Encoding": "identity"})
    assert r.headers.get("content-encoding") != "gzip"
