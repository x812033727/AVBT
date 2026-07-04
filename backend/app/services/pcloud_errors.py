"""pCloud error type, in its own module so both the core client
(services/pcloud.py) and the organize mixin (services/pcloud_organize.py)
can raise it without a circular import. Import sites keep using
``from .pcloud import PCloudError`` via re-export."""

from __future__ import annotations


class PCloudError(RuntimeError):
    """pCloud-side error.

    Carries:
      - ``.result`` — the numeric ``result`` code from the JSON body
        (0 if not from a structured response).
      - ``.payload`` — the full parsed response dict, when available,
        so callers can introspect undocumented hint fields (e.g.
        ``tfatoken``, ``hint``, ``region``) without re-parsing.
    """

    result: int = 0
    payload: dict | None = None
