"""parse -> pick account -> dedupe -> normalize -> insert -> categorize -> detect transfers."""

import hashlib
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from tracker import db
from tracker.categorizer import categorize
from tracker.llm import Classifier
from tracker.normalize import extract_ref, normalize
from tracker.parsers import BANK, CARD, ParsedStatement, RawTxn, parse_statement
from tracker.transfers import detect_transfers, mark_card_payments

log = logging.getLogger(__name__)


class NeedsAccount(Exception):
    """The statement couldn't be matched to one of your accounts automatically."""

    def __init__(self, bank: str | None, last4: str | None, kind: str = BANK):
        self.bank, self.last4, self.kind = bank, last4, kind
        what = "card" if kind == CARD else "account"
        hint = " ".join(x for x in (bank, "credit card" if kind == CARD else None, f"{what} ending {last4}" if last4 else None) if x)
        super().__init__(f"Choose which {what} this statement belongs to{f' (detected: {hint})' if hint else ''}.")


class AccountMismatch(NeedsAccount):
    """You picked an account, but the statement says it belongs to a different account number."""

    def __init__(self, bank: str | None, last4: str, chosen: str, kind: str = BANK):
        what = "card" if kind == CARD else "account"
        Exception.__init__(self, f"This statement is for the {what} ending {last4}, not {chosen}. Choose the right {what}.")
        self.bank, self.last4, self.kind = bank, last4, kind


class KindMismatch(NeedsAccount):
    """A credit card statement for a bank account, or the other way round."""

    def __init__(self, parsed: ParsedStatement, chosen: str):
        if parsed.kind == CARD:
            msg = f"This is a credit card statement, but {chosen} is a bank account. Choose a credit card account."
        else:
            msg = f"This looks like a bank account statement, but {chosen} is a credit card. Choose a bank account."
        Exception.__init__(self, msg)
        self.bank, self.last4, self.kind = parsed.bank, parsed.account_last4, parsed.kind


@dataclass
class ImportResult:
    account_id: int | None = None
    account_name: str | None = None
    account_created: bool = False
    filename: str | None = None
    statement_id: int | None = None
    bank: str | None = None
    kind: str = BANK
    layout: str = ""
    duplicate_file: bool = False
    n_rows: int = 0
    n_new: int = 0
    n_duplicates: int = 0
    period_start: str | None = None
    period_end: str | None = None
    transfers: int = 0
    card: dict | None = None  # credit cards: statement_date, due_date, total_due, min_due (rupees)
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


def fingerprint(account_id: int, raw: RawTxn, occurrence: int = 1) -> str:
    """Stable identity for a statement row, so overlapping statements don't double count.

    occurrence: the n-th identical row in the same file. Card statements have no running
    balance, so two ₹120 coffees on the same day would otherwise look like one.
    """
    ref = (raw.ref_no or extract_ref(raw.narration) or "").upper().lstrip("0")
    parts = [str(account_id), raw.txn_date.isoformat(), str(to_paise(raw.amount)), raw.direction]
    if ref:
        parts.append(f"ref:{ref}")
    else:
        parts.append("n:" + re.sub(r"\s+", "", raw.narration.upper()))
        parts.append(f"b:{to_paise(raw.balance)}")
    if occurrence > 1:
        parts.append(f"#{occurrence}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def _is_card(account) -> bool:
    return account["type"] == "credit_card"


def resolve_account(conn: sqlite3.Connection, parsed: ParsedStatement, account_id: int | None) -> tuple[sqlite3.Row, bool]:
    """-> (account, created). A card statement with a known card number gets its own account if it has none yet."""
    if account_id:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise ValueError(f"Account {account_id} not found")
        if (parsed.kind == CARD) != _is_card(row):
            raise KindMismatch(parsed, row["name"])
        if parsed.account_last4 and row["last4"] and parsed.account_last4 != row["last4"]:
            raise AccountMismatch(parsed.bank, parsed.account_last4, row["name"], parsed.kind)
        return row, False
    accounts = [a for a in conn.execute("SELECT * FROM accounts") if _is_card(a) == (parsed.kind == CARD)]
    if parsed.account_last4:
        hits = [a for a in accounts if a["last4"] == parsed.account_last4]
        if len(hits) == 1:
            return hits[0], False
    if parsed.bank:
        hits = [a for a in accounts if a["bank"] == parsed.bank]
        if len(hits) == 1 and not (parsed.account_last4 and hits[0]["last4"] and hits[0]["last4"] != parsed.account_last4):
            return hits[0], False
    if len(accounts) == 1 and not parsed.account_last4 and not parsed.bank:
        return accounts[0], False
    if parsed.kind == CARD and parsed.account_last4 and not any(a["last4"] == parsed.account_last4 for a in accounts):
        return _create_card_account(conn, parsed), True
    raise NeedsAccount(parsed.bank, parsed.account_last4, parsed.kind)


def _create_card_account(conn: sqlite3.Connection, parsed: ParsedStatement) -> sqlite3.Row:
    bank = parsed.bank or "OTHER"
    base = f"{parsed.bank} Credit Card" if parsed.bank else "Credit Card"
    taken = {r["name"] for r in conn.execute("SELECT name FROM accounts")}
    name = base if base not in taken else f"{base} {parsed.account_last4}"
    cur = conn.execute(
        "INSERT INTO accounts (name, bank, type, last4) VALUES (?, ?, 'credit_card', ?)", (name, bank, parsed.account_last4)
    )
    log.info("Created credit card account %s for a card statement", cur.lastrowid)
    return conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()


def _parse(path: Path, password: str | None, account_id: int | None) -> ParsedStatement:
    parsed = parse_statement(path, password)  # may raise PasswordRequired / StatementError
    if account_id and parsed.kind == BANK and not any(r.balance is not None for r in parsed.rows):
        # No running balance and the account you chose is a card: read it as a card statement.
        with db.session() as conn:
            row = conn.execute("SELECT type FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row and _is_card(row):
            parsed = parse_statement(path, password, kind=CARD)
    return parsed


def _card_summary(parsed: ParsedStatement) -> dict | None:
    if parsed.kind != CARD:
        return None
    c = parsed.card
    statement_date = (c.statement_date if c else None) or parsed.period_end
    return {
        "statement_date": statement_date.isoformat() if statement_date else None,
        "due_date": c.due_date.isoformat() if c and c.due_date else None,
        "total_due": float(c.total_due) if c and c.total_due is not None else None,
        "min_due": float(c.min_due) if c and c.min_due is not None else None,
    }


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

    parsed = _parse(path, password, account_id)
    result.bank, result.layout, result.n_rows, result.kind = parsed.bank, parsed.layout, len(parsed.rows), parsed.kind
    result.period_start = parsed.period_start.isoformat() if parsed.period_start else None
    result.period_end = parsed.period_end.isoformat() if parsed.period_end else None
    result.card = _card_summary(parsed)

    with db.session() as conn:
        account, result.account_created = resolve_account(conn, parsed, account_id)
        result.account_id, result.account_name = account["id"], account["name"]
        if parsed.account_last4 and not account["last4"]:
            conn.execute("UPDATE accounts SET last4 = ? WHERE id = ?", (parsed.account_last4, account["id"]))

        card = result.card or {}
        cur = conn.execute(
            """INSERT INTO statements (account_id, source, filename, file_sha256, period_start, period_end, n_rows, kind,
                                       statement_date, due_date, total_due_paise, min_due_paise)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account["id"], source, result.filename, sha, result.period_start, result.period_end, result.n_rows, parsed.kind,
                card.get("statement_date"), card.get("due_date"),
                to_paise(parsed.card.total_due) if parsed.card else None, to_paise(parsed.card.min_due) if parsed.card else None,
            ),
        )
        result.statement_id = cur.lastrowid

        uncategorized = db.uncategorized_id(conn)
        new_ids: list[int] = []
        seen: Counter[str] = Counter()
        for raw in parsed.rows:
            norm = normalize(raw.narration, raw.direction)
            channel = "CARD" if parsed.kind == CARD and norm.channel == "OTHER" else norm.channel
            base = fingerprint(account["id"], raw)
            seen[base] += 1
            cur = conn.execute(
                """INSERT OR IGNORE INTO transactions
                   (account_id, statement_id, txn_date, narration_raw, channel, counterparty, vpa, merchant_key,
                    amount_paise, direction, balance_paise, ref_no, fingerprint, category_id, category_source, needs_review)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fallback', 1)""",
                (
                    account["id"], result.statement_id, raw.txn_date.isoformat(), raw.narration, channel,
                    norm.counterparty, norm.vpa, norm.merchant_key, to_paise(raw.amount), raw.direction,
                    to_paise(raw.balance), raw.ref_no, fingerprint(account["id"], raw, seen[base]), uncategorized,
                ),
            )
            if cur.rowcount:
                new_ids.append(cur.lastrowid)
        result.n_new = len(new_ids)
        result.n_duplicates = result.n_rows - result.n_new
        conn.execute("UPDATE statements SET n_new = ? WHERE id = ?", (result.n_new, result.statement_id))

        if parsed.kind == CARD:
            mark_card_payments(conn, new_ids)  # before categorizing, so bill payments never go to Claude
        stats = categorize(conn, new_ids, classifier=classifier, use_llm=use_llm)
        result.categorize = stats.as_dict()
        detect_transfers(conn, new_ids)
        if new_ids:
            result.transfers = conn.execute(
                f"SELECT COUNT(*) FROM transactions WHERE category_source = 'transfer' AND id IN ({','.join('?' * len(new_ids))})",
                new_ids,
            ).fetchone()[0]
    log.info(
        "Imported statement %s into account %s (%s %s, %s layout, source %s): %d rows, %d new, %d already present, %d transfers%s",
        result.statement_id, result.account_id, parsed.kind, path.suffix, result.layout, source,
        result.n_rows, result.n_new, result.n_duplicates, result.transfers, " (new account)" if result.account_created else "",
    )
    return result
