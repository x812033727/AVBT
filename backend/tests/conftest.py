import os

# Keep tests hermetic: never let importing app.config/app.database create
# or touch the real ./data/avbt.db. Individual tests that need a file DB
# build their own engine against tmp_path.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
