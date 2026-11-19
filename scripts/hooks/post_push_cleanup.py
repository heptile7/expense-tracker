"""Claude Code PostToolUse hook: tidy the project after a successful `git push`.

Reads the hook payload (JSON) from stdin and does nothing unless the tool was a shell command that
ran `git push` and the push went through. Then it:
  * deletes __pycache__, .pytest_cache and .ruff_cache folders (never anything under .venv)
  * deletes files in data/inbox/uploads that no imported statement refers to and that are over an
    hour old (leftovers from interrupted uploads); statements you imported are kept
  * stops headless browsers left behind by Playwright tests (never a visible window, so an open
    net banking session is safe)
It never touches the database, logs, .env, statements in data/inbox/netbanking or data/inbox/gmail.
Results go to .claude/logs/cleanup.log. Always exits 0: a cleanup problem must never block Claude.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / ".claude" / "logs" / "cleanup.log"
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_DIRS = {".venv", ".git", "node_modules"}
PUSH_RE = re.compile(r"\bgit(?:\.exe)?\s+push\b")
FAILED_RE = re.compile(r"\b(rejected|fatal:|error:|failed to push)", re.IGNORECASE)


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def pushed(payload: dict) -> bool:
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return False
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not PUSH_RE.search(command) or "--dry-run" in command:
        return False
    output = json.dumps(payload.get("tool_response", ""))
    return not FAILED_RE.search(output)


def remove_caches(root: Path) -> int:
    removed = 0
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for d in list(dirnames):
            if d in CACHE_DIRS:
                shutil.rmtree(Path(dirpath) / d, ignore_errors=True)
                dirnames.remove(d)
                removed += 1
    return removed


def _data_dir() -> Path:
    return Path(os.getenv("EXPENSE_TRACKER_DATA", ROOT / "data"))


def remove_upload_leftovers(data_dir: Path, min_age_s: int = 3600) -> int:
    uploads = data_dir / "inbox" / "uploads"
    db_path = Path(os.getenv("EXPENSE_TRACKER_DB", data_dir / "expenses.db"))
    if not uploads.is_dir() or not db_path.exists():
        return 0
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        known = {r[0] for r in conn.execute("SELECT file_sha256 FROM statements WHERE file_sha256 IS NOT NULL")}
    removed, now = 0, time.time()
    for f in uploads.iterdir():
        if not f.is_file() or now - f.stat().st_mtime < min_age_s:
            continue
        if hashlib.sha256(f.read_bytes()).hexdigest() not in known:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def stop_test_browsers() -> int:
    """Headless Edge/Chrome started by Playwright (its throwaway profile folder is in the command line)."""
    if sys.platform != "win32":
        return 0
    script = (
        "$p = Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*playwright_chromiumdev_profile*' -and $_.CommandLine -like '*--headless*' -and "
        "$_.CommandLine -notlike '*--type=*' }; "
        "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; ($p | Measure-Object).Count"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int((out.stdout or "0").strip().splitlines()[-1] or 0)
    except Exception:
        return 0


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    if not pushed(payload):
        return
    try:
        caches = remove_caches(ROOT)
        uploads = remove_upload_leftovers(_data_dir())
        browsers = stop_test_browsers()
        summary = f"after push: removed {caches} cache folders, {uploads} upload leftovers, stopped {browsers} test browsers"
        _log(summary)
        print(json.dumps({"systemMessage": f"Post-push cleanup {summary}."}))
    except Exception as exc:  # never block Claude
        _log(f"cleanup failed: {exc!r}")


if __name__ == "__main__":
    main()
    sys.exit(0)
