"""Pull statement attachments from Gmail (read-only) and feed them to the importer.

One-time setup (you do this): create an OAuth client of type "Desktop app" in Google
Cloud Console with the Gmail API enabled, download its JSON as data/credentials.json,
then click "Connect Gmail" in Settings (or run `python -m tracker.fetch.gmail auth`).

Only the gmail.readonly scope is requested. Each account has its own Gmail search
query (e.g. ``from:(estatement@yourbank.example) subject:(statement)``), set in Settings.
"""

import base64
import logging
import re
import sys
from pathlib import Path

import config
from tracker import db
from tracker.fetch.secrets import get_pdf_password
from tracker.importer import NeedsAccount, import_file
from tracker.parsers import SUPPORTED, PasswordRequired, StatementError

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DONE_STATUSES = ("imported", "duplicate", "no_attachment", "unreadable")


def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not config.GMAIL_TOKEN.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        config.GMAIL_TOKEN.write_text(creds.to_json())
    return creds if creds and creds.valid else None


def status() -> dict:
    out = {"credentials_file": config.GMAIL_CREDENTIALS.exists(), "connected": False, "email": None, "error": None}
    try:
        creds = _load_credentials()
        if creds:
            profile = _service(creds).users().getProfile(userId="me").execute()
            out.update(connected=True, email=profile.get("emailAddress"))
    except Exception as exc:
        out["error"] = str(exc)
    return out


def connect() -> dict:
    """Opens the Google consent page in your browser and stores the read-only token locally."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GMAIL_CREDENTIALS.exists():
        raise FileNotFoundError(f"Put your OAuth client file at {config.GMAIL_CREDENTIALS} first (see README).")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GMAIL_CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    config.GMAIL_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN.write_text(creds.to_json())
    return status()


def disconnect() -> None:
    config.GMAIL_TOKEN.unlink(missing_ok=True)


def _service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _attachments(payload: dict):
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(part.get("parts", []) or [])
        name = part.get("filename") or ""
        att_id = (part.get("body") or {}).get("attachmentId")
        if name and att_id and Path(name).suffix.lower() in SUPPORTED:
            yield name, att_id


def _record(message_id: str, account_id: int, status_: str, detail: str | None = None) -> None:
    with db.session() as conn:
        conn.execute(
            """INSERT INTO fetched_messages (message_id, account_id, kind, status, detail) VALUES (?, ?, 'statement', ?, ?)
               ON CONFLICT (message_id) DO UPDATE SET status = excluded.status, detail = excluded.detail,
                 processed_at = CURRENT_TIMESTAMP""",
            (message_id, account_id, status_, detail),
        )


def _already_done(message_id: str) -> bool:
    with db.session() as conn:
        row = conn.execute("SELECT status FROM fetched_messages WHERE message_id = ?", (message_id,)).fetchone()
    return bool(row and row["status"] in DONE_STATUSES)


def sync(account_id: int | None = None, max_messages: int = 50) -> dict:
    creds = _load_credentials()
    if not creds:
        raise RuntimeError("Gmail isn't connected yet. Click 'Connect Gmail' in Settings.")
    service = _service(creds)
    with db.session() as conn:
        sql = "SELECT * FROM accounts WHERE gmail_query IS NOT NULL AND gmail_query != ''"
        accounts = [dict(r) for r in conn.execute(sql + (" AND id = ?" if account_id else ""), (account_id,) if account_id else ())]
    if not accounts:
        raise ValueError("No account has a Gmail search query yet. Add one under Settings > Accounts.")

    inbox = config.INBOX_DIR / "gmail"
    inbox.mkdir(parents=True, exist_ok=True)
    report = []
    for acct in accounts:
        summary = {"account": acct["name"], "messages": 0, "imported": 0, "new_transactions": 0, "skipped": 0, "problems": []}
        resp = service.users().messages().list(userId="me", q=f"({acct['gmail_query']}) has:attachment", maxResults=max_messages).execute()
        for ref in resp.get("messages", []):
            mid = ref["id"]
            if _already_done(mid):
                summary["skipped"] += 1
                continue
            summary["messages"] += 1
            msg = service.users().messages().get(userId="me", id=mid).execute()
            files = list(_attachments(msg.get("payload", {})))
            if not files:
                _record(mid, acct["id"], "no_attachment")
                continue
            outcome = "imported"
            for name, att_id in files:
                data = service.users().messages().attachments().get(userId="me", messageId=mid, id=att_id).execute()
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
                path = inbox / f"{mid}_{safe}"
                path.write_bytes(base64.urlsafe_b64decode(data["data"]))
                try:
                    result = import_file(path, acct["id"], get_pdf_password(acct["id"]), name, source="gmail")
                except PasswordRequired:
                    outcome = "needs_password"
                    summary["problems"].append(f"{name}: PDF is password-protected. Save the password for {acct['name']} in Settings.")
                    continue
                except (StatementError, NeedsAccount) as exc:
                    outcome = "unreadable"
                    summary["problems"].append(f"{name}: {exc}")
                    continue
                if result.duplicate_file:
                    outcome = "duplicate" if outcome == "imported" else outcome
                else:
                    summary["imported"] += 1
                    summary["new_transactions"] += result.n_new
            _record(mid, acct["id"], outcome)
        report.append(summary)
    return {"accounts": report}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        print(connect())
    else:
        print(sync())
