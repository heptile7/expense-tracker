"""parse -> pick account -> dedupe -> normalize -> insert -> categorize -> detect transfers."""

import hashlib
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from tracker import db
from tracker.categorizer import categorize
from tracker.llm import Classifier
from tracker.normalize import extract_ref, normalize
from tracker.parsers import ParsedStatement, RawTxn, parse_statement
from tracker.transfers import detect_transfers

log = logging.getLogger(__name__)


class NeedsAccount(Exception):
    """The statement couldn't be matched to one of your accounts automatically."""

    def __init__(self, bank: str | None, last4: str | None):
        self.bank, self.last4 = bank, last4
        hint = " ".join(x for x in (bank, f"account ending {last4}" if last4 else None) if x)
        super().__init__(f"Choose which account this statement belongs to{f' (detected: {hint})' if hint else ''}.")


class AccountMismatch(NeedsAccount):
    """You picked an account, but the statement says it belongs to a different account number."""

    def __init__(self, bank: str | None, last4: str, chosen: str):
        Exception.__init__(self, f"This statement is for the account ending {last4}, not {chosen}. Choose the right account.")
        self.bank, self.last4 = bank, last4


@dataclass
class ImportResult:
    account_id: int | None = None
    account_name: str | None = None
    filename: str | None = None
    statement_id: int | None = None
    bank: str | None = None
    layout: str = ""
    duplicate_file: bool = False
    n_rows: int = 0
    n_new: int = 0
    n_duplicates: int = 0
    period_start: str | None = None
    period_end: str | None = None
    transfers: int = 0
    categorize: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def to_paise(amount: Decimal | None) -> int | None:
    return None if amount is None else int((amount * 100).quantize(Decimal(1)))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(account_id: int, raw: RawTxn) -> str:
    """Stable identity for a statement row, so overlapping statements don't double count."""
    ref = (raw.ref_no or extract_ref(raw.narration) or "").upper().lstrip("0")
    parts = [str(account_id), raw.txn_date.isoformat(), str(to_paise(raw.amount)), raw.direction]
    if ref:
        parts.append(f"ref:{ref}")
    else:
        parts.append("n:" + re.sub(r"\s+", "", raw.narration.upper()))
        parts.append(f"b:{to_paise(raw.balance)}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def resolve_account(conn: sqlite3.Connection, parsed: ParsedStatement, account_id: int | None) -> sqlite3.Row:
    if account_id:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise ValueError(f"Account {account_id} not found")
        if parsed.account_last4 and row["last4"] and parsed.account_last4 != row["last4"]:
            raise AccountMismatch(parsed.bank, parsed.account_last4, row["name"])
        return row
    accounts = conn.execute("SELECT * FROM accounts").fetchall()
    if parsed.account_last4:
        hits = [a for a in accounts if a["last4"] == parsed.account_last4]
        if len(hits) == 1:
            return hits[0]
    if parsed.bank:
        hits = [a for a in accounts if a["bank"] == parsed.bank]
        if len(hits) == 1 and not (parsed.account_last4 and hits[0]["last4"] and hits[0]["last4"] != parsed.account_last4):
            return hits[0]
    if len(accounts) == 1 and not parsed.account_last4 and not parsed.bank:
        return accounts[0]
    raise NeedsAccount(parsed.bank, parsed.account_last4)


def import_file(
    path: str | Path,
    account_id: int | None = None,
    password: str | None = None,
    filename: str | None = None,
    source: str = "upload",
    classifier: Classifier | None = None,
    use_llm: bool = True,
) -> ImportResult:
    path = Path(path)
    result = ImportResult(filename=filename or path.name)
    sha = file_sha256(path)
    with db.session() as conn:
        existing = conn.execute(
            "SELECT s.id, s.account_id, a.name FROM statements s JOIN accounts a ON a.id = s.account_id WHERE file_sha256 = ?",
            (sha,),
        ).fetchone()
        if existing:
            result.duplicate_file = True
            result.statement_id, result.account_id, result.account_name = existing
            log.info("Skipped a file already imported as statement %s", result.statement_id)
            return result

    parsed = parse_statement(path, password)  # may raise PasswordRequired / StatementError
    result.bank, result.layout, result.n_rows = parsed.bank, parsed.layout, len(parsed.rows)
    result.period_start = parsed.period_start.isoformat() if parsed.period_start else None
    result.period_end = parsed.period_end.isoformat() if parsed.period_end else None

    with db.session() as conn:
        account = resolve_account(conn, parsed, account_id)
        result.account_id, result.account_name = account["id"], account["name"]
        if parsed.account_last4 and not account["last4"]:
            conn.execute("UPDATE accounts SET last4 = ? WHERE id = ?", (parsed.account_last4, account["id"]))

        cur = conn.execute(
            "INSERT INTO statements (account_id, source, filename, file_sha256, period_start, period_end, n_rows) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account["id"], source, result.filename, sha, result.period_start, result.period_end, result.n_rows),
        )
        result.statement_id = cur.lastrowid

        uncategorized = db.uncategorized_id(conn)
        new_ids: list[int] = []
        for raw in parsed.rows:
            norm = normalize(raw.narration, raw.direction)
            cur = conn.execute(
                """INSERT OR IGNORE INTO transactions
                   (account_id, statement_id, txn_date, narration_raw, channel, counterparty, vpa, merchant_key,
                    amount_paise, direction, balance_paise, ref_no, fingerprint, category_id, category_source, needs_review)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fallback', 1)""",
                (
                    account["id"], result.statement_id, raw.txn_date.isoformat(), raw.narration, norm.channel,
                    norm.counterparty, norm.vpa, norm.merchant_key, to_paise(raw.amount), raw.direction,
                    to_paise(raw.balance), raw.ref_no, fingerprint(account["id"], raw), uncategorized,
                ),
            )
            if cur.rowcount:
                new_ids.append(cur.lastrowid)
        result.n_new = len(new_ids)
        result.n_duplicates = result.n_rows - result.n_new
        conn.execute("UPDATE statements SET n_new = ? WHERE id = ?", (result.n_new, result.statement_id))

        stats = categorize(conn, new_ids, classifier=classifier, use_llm=use_llm)
        result.categorize = stats.as_dict()
        result.transfers = detect_transfers(conn, new_ids)
    log.info(
        "Imported statement %s into account %s (%s, %s layout, source %s): %d rows, %d new, %d already present, %d transfers",
        result.statement_id, result.account_id, path.suffix, result.layout, source,
        result.n_rows, result.n_new, result.n_duplicates, result.transfers,
    )
    return result
