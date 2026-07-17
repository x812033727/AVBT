import os

# Keep tests hermetic: never let importing app.config/app.database create
# or touch the real ./data/avbt.db. Individual tests that need a file DB
# build their own engine against tmp_path.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
# ... and never let fake services persist move-source stamps into
# ./data/move_sources.json — fresh stamps written by one test would
# preload the gate of every service built later (generic ids like "f1"
# collide across tests). Persistence itself is covered by
# test_move_settle_persist.py against tmp_path.
os.environ.setdefault("PIKPAK_MOVE_LOG", "")
