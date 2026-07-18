"""Stream-aware gzip middleware.

Starlette's ``GZipMiddleware`` buffers streaming responses: its
``minimum_size`` floor only exempts a response whose first body chunk is
also its last, and the streaming branch writes every chunk into one
``GzipFile`` with no per-chunk flush. Our ~19 ``application/x-ndjson``
progress feeds (finalize / reorganize / legacy sweep / tracked send-all
/ collection / compare) therefore stop emitting bytes until enough
accumulate to fill zlib's window — the live progress modals freeze for
the whole run, and idle-connection proxies can drop a stream that no
longer sends data (2026-07-18 integration audit, after #228 added the
global middleware).

The base responder already has a clean pass-through path: when the
response carries a ``content-encoding`` header it forwards every chunk
untouched. We reuse it — for streaming content types we set that same
flag from the content-type alone, so those responses flush chunk by
chunk while every other response still gets compressed exactly as
before.
"""

from __future__ import annotations

from fastapi.middleware.gzip import GZipMiddleware
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder
from starlette.types import Message, Receive, Scope, Send

# Content types whose bytes must reach the client as they are produced.
_STREAMING_CONTENT_TYPES = ("application/x-ndjson", "text/event-stream")


def _is_streaming_content_type(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    return ct in _STREAMING_CONTENT_TYPES


class _StreamAwareGZipResponder(GZipResponder):
    async def send_with_gzip(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = Headers(raw=message["headers"])
            if _is_streaming_content_type(headers.get("content-type", "")):
                # Reuse the base responder's already-encoded pass-through:
                # remember the start message and mark the stream so every
                # body chunk is forwarded verbatim (no gzip buffering).
                self.initial_message = message
                self.content_encoding_set = True
                return
        await super().send_with_gzip(message)


class StreamAwareGZipMiddleware(GZipMiddleware):
    """``GZipMiddleware`` that never buffers streaming content types."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if "gzip" in headers.get("Accept-Encoding", ""):
                responder = _StreamAwareGZipResponder(
                    self.app, self.minimum_size, compresslevel=self.compresslevel
                )
                await responder(scope, receive, send)
                return
        await self.app(scope, receive, send)
