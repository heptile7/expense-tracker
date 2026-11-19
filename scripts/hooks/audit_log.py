"""Claude Code PostToolUse / PostToolUseFailure hook: audit trail of what Claude does in this project.

Appends one JSON line per tool call to .claude/logs/claude-actions.jsonl:
  {"ts", "session", "event", "tool", "ok", "detail"}
`detail` is the shell command, the file path, the search pattern or the URL, cut to 300 characters
and with anything that looks like a key, token or password masked. File contents, text Claude types
into pages, tool output and MCP tool arguments are never recorded (MCP calls log argument names only).
The file rotates at 5 MB (one previous file kept as .1). Always exits 0.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / ".claude" / "logs" / "claude-actions.jsonl"
MAX_BYTES = 5_000_000
MAX_DETAIL = 300

_SECRETS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"), r"\1-***"),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{16,}"), r"\1***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|pin|otp)(\s*[=:]\s*|\s+)(['\"]?)[^\s'\";]+"), r"\1\2\3***"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def _rel(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return path


def describe(tool: str, args: dict) -> str:
    if not isinstance(args, dict):
        return ""
    if tool in ("Bash", "PowerShell"):
        return args.get("command", "")
    if tool in ("Read", "Write", "Edit", "NotebookEdit"):
        return _rel(args.get("file_path") or args.get("notebook_path") or "")
    if tool in ("Glob", "Grep"):
        return f"{args.get('pattern', '')} in {_rel(args.get('path') or '.')}"
    if tool == "WebFetch":
        return args.get("url", "")
    if tool == "WebSearch":
        return args.get("query", "")
    if tool in ("Agent", "Task"):
        return args.get("description", "")
    if tool == "Skill":
        return args.get("skill", "")
    if tool.endswith("__navigate") or tool.endswith("preview_start"):
        return args.get("url") or args.get("name") or ""
    if tool.endswith("__computer"):
        return args.get("action_summary") or args.get("action", "")  # never the typed text
    # MCP and anything else: argument names only, never their values
    return "args: " + ",".join(sorted(args))


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    tool = payload.get("tool_name") or ""
    if not tool:
        return
    event = payload.get("hook_event_name") or "PostToolUse"
    detail = redact(describe(tool, payload.get("tool_input") or {})).replace("\n", " ⏎ ")
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session": (payload.get("session_id") or "")[:8],
        "event": event,
        "tool": tool,
        "ok": event != "PostToolUseFailure",
        "detail": detail[:MAX_DETAIL] + ("…" if len(detail) > MAX_DETAIL else ""),
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
        LOG.replace(LOG.with_suffix(".jsonl.1"))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # an audit problem must never block Claude
    sys.exit(0)
