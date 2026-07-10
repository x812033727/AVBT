import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import cors_origin_list
from .database import init_db
from .logging_setup import setup_logging
from .routers import (
    auth,
    backup,
    collection,
    compare,
    img,
    javbus,
    pcloud,
    pikpak,
    stats,
    tracked,
)
from .routers import (
    notify as notify_router,
)
from .scrapers import javbus as scraper
from .services import archiver, auto_backup, log_cleanup, notify, tracker
from .services.auth import require_auth
from .services.download_queue import download_queue, warm_sent_hashes
from .services.pcloud import pcloud_service
from .services.pcloud_transfer import pcloud_transfer_queue
from .services.scraper_health import scraper_health
from .services.supervisor import supervise
from .services.webhook_queue import webhook_queue

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Build the shared JavBus HTTP client before workers start — they
    # call into the scraper as soon as a job lands.
    await scraper.init_client()
    await download_queue.start()
    # Pre-load the sent-hash cache so the first job doesn't pay the
    # full-table-scan latency. Cheap on small DBs, ~hundreds of ms on
    # ones with 10k+ rows.
    await warm_sent_hashes()
    await webhook_queue.start()
    await pcloud_transfer_queue.start()
    background = [
        supervise(archiver.run_loop, "archiver"),
        supervise(tracker.run_loop, "tracker"),
        supervise(log_cleanup.run_loop, "log-cleanup"),
        supervise(auto_backup.run_loop, "auto-backup"),
    ]
    try:
        yield
    finally:
        for t in background:
            t.cancel()
        for t in background:
            try:
                await t
            except asyncio.CancelledError:
                pass
        await pcloud_transfer_queue.stop()
        await webhook_queue.stop()
        await download_queue.stop()
        await scraper.aclose_client()
        await img.aclose_client()
        await notify.aclose_client()
        await pcloud_service.aclose()


app = FastAPI(title="AVBT", version="0.1.0", lifespan=lifespan)

_origins = cors_origin_list()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    # The CORS spec forbids credentials with a wildcard origin.
    allow_credentials="*" not in _origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Public: the frontend hits these before it has a token.
app.include_router(auth.router)

# Protected: every data/route below requires a valid login token.
# NB: img.router stays PUBLIC — browser <img src> tags can't carry an
# Authorization header, so gating the image proxy would break every
# thumbnail on the site. Backup IS protected (it dumps user data); the
# frontend downloads it via an authenticated fetch instead of a raw link.
_guard = [Depends(require_auth)]
app.include_router(javbus.router, dependencies=_guard)
app.include_router(pikpak.router, dependencies=_guard)
app.include_router(pcloud.router, dependencies=_guard)
app.include_router(compare.router, dependencies=_guard)
app.include_router(collection.router, dependencies=_guard)
app.include_router(tracked.router, dependencies=_guard)
app.include_router(stats.router, dependencies=_guard)
app.include_router(notify_router.router, dependencies=_guard)
app.include_router(backup.router, dependencies=_guard)
app.include_router(img.router)


@app.get("/api/health")
async def health():
    # scraper_degraded is informational — the container healthcheck must
    # not flap because JavBus is having a bad hour.
    return {"ok": True, "scraper_degraded": scraper_health.degraded()}
