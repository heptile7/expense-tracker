"""Logging for the backend: rotating files under data/logs/.

What gets logged: API requests (method, path, status, time), imports, categorization runs,
Claude calls (which route answered, token counts, errors), net banking sessions, money plans,
Gmail syncs, and every error with its traceback.

What never gets logged: amounts, balances, narrations, payee or people's names, account
numbers, search text, passwords or API keys. Log by id and count, not by content.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

import config

LOG_DIR = config.DATA_DIR / "logs"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUPS = 5

# Chatty libraries: their INFO lines add noise (and can echo URLs or payloads).
_QUIET = ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic", "uvicorn.access", "multipart", "pdfminer", "pdfplumber",
          "urllib3", "googleapiclient", "google_auth_oauthlib", "asyncio")


def setup(name: str = "app", level: int = logging.INFO, console: bool = True) -> Path:
    """Send all logging to data/logs/<name>.log (rotated at 1 MB, 5 kept). Safe to call more than once."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{name}.log"
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_et_path", None) == str(path) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        fh._et_path = str(path)
        fh.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(fh)
    # No console when started in the background (pythonw.exe at Windows sign-in): stderr is None there.
    if console and sys.stderr is not None and not any(getattr(h, "_et_console", False) for h in root.handlers):
        ch = logging.StreamHandler()  # stderr
        ch._et_console = True
        ch.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(ch)
    for noisy in _QUIET:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
