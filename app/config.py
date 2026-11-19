"""Runtime configuration, read from environment variables (see /etc/mydrive/env)."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("MYDRIVE_DATA_DIR", "/data")).resolve()
FILES_DIR = DATA_DIR / "files"
USERS_DIR = FILES_DIR / "users"
SHARED_DIR = FILES_DIR / "shared"
TRASH_DIR = DATA_DIR / "trash"
DB_PATH = DATA_DIR / "mydrive.db"

# Public URL, used to build share links and connector snippets.
BASE_URL = os.environ.get("MYDRIVE_BASE_URL", "http://localhost:8080").rstrip("/")
COOKIE_SECURE = os.environ.get("MYDRIVE_COOKIE_SECURE", "1") == "1"

SESSION_DAYS = int(os.environ.get("MYDRIVE_SESSION_DAYS", "30"))
TRASH_DAYS = int(os.environ.get("MYDRIVE_TRASH_DAYS", "30"))
# Uploads are refused when they would leave less than this much free disk.
MIN_FREE_BYTES = int(os.environ.get("MYDRIVE_MIN_FREE_MB", "512")) * 1024 * 1024
# Largest file the MCP read_file tool returns inline.
MCP_MAX_READ = int(os.environ.get("MYDRIVE_MCP_MAX_READ_KB", "512")) * 1024

SHARED_NAME = "Shared"  # top-level virtual folder every user sees
APP_NAME = "MyDrive"
VERSION = "1.0.0"


def ensure_dirs() -> None:
    for d in (DATA_DIR, FILES_DIR, USERS_DIR, SHARED_DIR, TRASH_DIR):
        d.mkdir(parents=True, exist_ok=True)
