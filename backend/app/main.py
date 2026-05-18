import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .routers import backup, collection, img, javbus, pikpak, tracked
from .services import archiver, log_cleanup, notify, tracker
from .services.download_queue import download_queue
from .services.webhook_queue import webhook_queue


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await download_queue.start()
    await webhook_queue.start()
    background = [
        asyncio.create_task(archiver.run_loop()),
        asyncio.create_task(tracker.run_loop()),
        asyncio.create_task(log_cleanup.run_loop()),
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
        await webhook_queue.stop()
        await download_queue.stop()
        await img.aclose_client()
        await notify.aclose_client()


app = FastAPI(title="AVBT", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(javbus.router)
app.include_router(pikpak.router)
app.include_router(collection.router)
app.include_router(tracked.router)
app.include_router(backup.router)
app.include_router(img.router)


@app.get("/api/health")
async def health():
    return {"ok": True}
