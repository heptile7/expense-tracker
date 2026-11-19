"""Claude Code hook scripts: post-push cleanup and the audit log."""

import hashlib
import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "hooks" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cleanup = _load("post_push_cleanup")
audit = _load("audit_log")


def test_push_detection():
    ok = {"tool_name": "PowerShell", "tool_input": {"command": "Set-Location x; git push origin master"},
          "tool_response": "To https://github.com/x/y.git\n   a..b  master -> master"}
    assert cleanup.pushed(ok)
    assert not cleanup.pushed({**ok, "tool_input": {"command": "git status"}})
    assert not cleanup.pushed({**ok, "tool_input": {"command": "git push --dry-run"}})
    assert not cleanup.pushed({**ok, "tool_response": " ! [rejected] master -> master (fetch first)\nerror: failed to push"})
    assert not cleanup.pushed({**ok, "tool_name": "Edit"})


def test_upload_leftovers_keep_imported_and_recent(tmp_path):
    up = tmp_path / "inbox" / "uploads"
    up.mkdir(parents=True)
    imported, stray, fresh = up / "a.xlsx", up / "b.pdf", up / "c.pdf"
    imported.write_bytes(b"imported")
    stray.write_bytes(b"stray")
    fresh.write_bytes(b"fresh")
    old = time.time() - 7200
    os.utime(imported, (old, old))
    os.utime(stray, (old, old))
    with sqlite3.connect(tmp_path / "expenses.db") as db:
        db.execute("CREATE TABLE statements (file_sha256 TEXT)")
        db.execute("INSERT INTO statements VALUES (?)", (hashlib.sha256(b"imported").hexdigest(),))
    assert cleanup.remove_upload_leftovers(tmp_path) == 1
    assert sorted(p.name for p in up.iterdir()) == ["a.xlsx", "c.pdf"]


def test_cache_removal_skips_venv(tmp_path):
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".venv" / "lib" / "__pycache__").mkdir(parents=True)
    assert cleanup.remove_caches(tmp_path) == 2
    assert (tmp_path / ".venv" / "lib" / "__pycache__").exists()


def test_audit_redacts_secrets_and_skips_contents():
    cmd = "export ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnop && curl -H 'Authorization: Bearer abcdefghijklmnopqrst' --password=hunter22"
    out = audit.redact(audit.describe("Bash", {"command": cmd}))
    for secret in ("abcdefghijklmnop", "hunter22", "abcdefghijklmnopqrst"):
        assert secret not in out
    assert audit.describe("Edit", {"file_path": str(ROOT / "tracker" / "db.py"), "new_string": "SECRET"}) == str(Path("tracker") / "db.py")
    assert audit.describe("mcp__expense-tracker__recategorize", {"transaction_id": 5, "category": "Rent"}) == "args: category,transaction_id"
    assert audit.describe("mcp__Claude_Browser__computer", {"action": "type", "text": "my-password"}) == "type"


def test_audit_writes_jsonl(tmp_path, monkeypatch, capsys):
    import io
    import sys

    log = tmp_path / "actions.jsonl"
    monkeypatch.setattr(audit, "LOG", log)
    payload = {"session_id": "1234567890", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "pytest"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    audit.main()
    entry = json.loads(log.read_text(encoding="utf-8"))
    assert entry == {**entry, "session": "12345678", "event": "PostToolUseFailure", "tool": "Bash", "ok": False, "detail": "pytest"}
