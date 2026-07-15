"""Unattended-ops report viewer.

The hourly cron worker appends a block per round to
``data/ops/reports.log`` (host: ``backend/data/ops/reports.log``; the
legacy ``/opt/avbt-backfill/reports.log`` path is a symlink to it, so
the worker keeps writing to the path it has always known). Blocks are
delimited by lines starting with ``=== `` — this router parses them and
serves newest-first so the frontend can render a timeline.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query

from ..schemas import OpsReport, OpsReports

router = APIRouter(prefix="/api/ops", tags=["ops"])

REPORTS_PATH = Path("data/ops/reports.log")


def parse_reports(text: str) -> list[OpsReport]:
    """Split the log into blocks on ``=== …`` header lines.

    Stray lines before the first header (or appended by one-off helper
    scripts between rounds) attach to the preceding block so nothing is
    silently dropped."""
    blocks: list[OpsReport] = []
    header = ""
    body: list[str] = []

    def flush() -> None:
        if header or any(line.strip() for line in body):
            joined = "\n".join(body).strip("\n")
            blocks.append(
                OpsReport(
                    header=header or "(未分段紀錄)",
                    body=joined,
                    critical="[CRITICAL" in header or "[CRITICAL" in joined,
                )
            )

    for line in text.splitlines():
        if line.startswith("=== "):
            flush()
            header = line.strip("= ").strip()
            body = []
        else:
            body.append(line)
    flush()
    return blocks


@router.get("/reports", response_model=OpsReports)
async def ops_reports(limit: int = Query(30, ge=1, le=200)):
    try:
        text = REPORTS_PATH.read_text(encoding="utf-8", errors="replace")
        mtime: datetime | None = datetime.fromtimestamp(
            REPORTS_PATH.stat().st_mtime
        )
    except FileNotFoundError:
        return OpsReports(reports=[], total=0, updated_at=None)
    blocks = parse_reports(text)
    blocks.reverse()  # newest first
    return OpsReports(
        reports=blocks[:limit], total=len(blocks), updated_at=mtime
    )
