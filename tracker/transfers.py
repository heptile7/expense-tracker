"""Spot money moving between your own accounts so it isn't counted as spending.

Signals:
  * a debit in one account matched by a credit of the same amount in another of your
    accounts within ±2 days -> Self Transfer;
  * a credit card bill: a payment credited on a card account matched by the same amount
    leaving a bank account within ±5 days (bill payments can take a few working days to
    post) -> Credit Card Payment on both sides. Card purchases never pair with anything;
  * a payment received on a card account ("PAYMENT RECEIVED", "NETBANKING TRANSFER",
    "AUTOPAY"...) even when the bank side isn't imported -> Credit Card Payment;
  * a UPI/NEFT/IMPS counterparty whose name contains your account holder name.
Your own rules and manual choices always win over this.
"""

import re
import sqlite3
from datetime import date, timedelta

from tracker import db

_TRANSFER_CHANNELS = ("UPI", "NEFT", "IMPS", "RTGS", "TRANSFER", "CHQ", "OTHER")
_PROTECTED = ("manual", "rule")  # never override what you decided
BANK_DAYS, CARD_DAYS = 2, 5

_CARD_PAYMENT_RE = re.compile(
    r"PAYMENT\s*RECEIVED|PAYMENT\s*REC\b|THANK\s*YOU|NET\s*BANKING|NETBANKING|\bBPPY\b|\bCC\s*PAYMENT|CCPAYMENT|"
    r"AUTO\s*PAY|AUTO\s*DEBIT|\bNEFT\b|\bIMPS\b|\bRTGS\b|BILL\s*DESK|BILL\s*PAY|TELE\s*TRANSFER|\bCRED\b|"
    r"CARD\s+PAYMENT|ONLINE\s+PAYMENT|PAYMENT\s+(?:BY|FROM|VIA)|\bNACH\b"
)
_NOT_PAYMENT_RE = re.compile(r"REFUND|REVERSAL|CASH\s*BACK|WAIVER|REWARD|CANCEL|CHARGEBACK")


def looks_like_card_payment(narration: str) -> bool:
    """A credit on a card account that is a bill payment, not a refund."""
    text = re.sub(r"\s+", " ", (narration or "").upper())
    return bool(_CARD_PAYMENT_RE.search(text)) and not _NOT_PAYMENT_RE.search(text)


def _mark(conn, txn_id: int, category_id: int, linked: int | None = None) -> None:
    conn.execute(
        """UPDATE transactions SET category_id = ?, category_source = 'transfer', rule_id = NULL, confidence = 1.0,
           needs_review = 0, linked_txn_id = COALESCE(?, linked_txn_id) WHERE id = ?""",
        (category_id, linked, txn_id),
    )


def _card_payment_id(conn) -> int | None:
    return db.category_id_by_name(conn, "Credit Card Payment") or db.category_id_by_name(conn, "Self Transfer")


def mark_card_payments(conn: sqlite3.Connection, txn_ids: list[int]) -> int:
    """Payments received on card accounts -> Credit Card Payment, whether or not the bank side is imported."""
    card_id = _card_payment_id(conn)
    if not txn_ids or card_id is None:
        return 0
    rows = conn.execute(
        f"""SELECT t.id, t.narration_raw FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE a.type = 'credit_card' AND t.direction = 'credit' AND t.category_source NOT IN ('manual', 'rule')
              AND t.id IN ({','.join('?' * len(txn_ids))})""",
        txn_ids,
    ).fetchall()
    marked = [r["id"] for r in rows if looks_like_card_payment(r["narration_raw"])]
    for txn_id in marked:
        _mark(conn, txn_id, card_id)
    return len(marked)


def detect_transfers(conn: sqlite3.Connection, txn_ids: list[int]) -> int:
    if not txn_ids:
        return 0
    self_id = db.category_id_by_name(conn, "Self Transfer")
    card_id = _card_payment_id(conn)
    if self_id is None:
        return 0
    accounts = {r["id"]: dict(r) for r in conn.execute("SELECT id, type, holder_name FROM accounts")}
    is_card = {a_id: a["type"] == "credit_card" for a_id, a in accounts.items()}
    marked: set[int] = set()

    placeholders = ",".join("?" * len(txn_ids))
    rows = conn.execute(
        f"""SELECT id, account_id, txn_date, amount_paise, direction, channel, counterparty, category_source, category_id,
                   linked_txn_id, narration_raw
            FROM transactions WHERE id IN ({placeholders})""",
        txn_ids,
    ).fetchall()

    # 1. Matching debit/credit pairs across your accounts.
    if len(accounts) > 1:
        for row in rows:
            if row["id"] in marked or row["linked_txn_id"] or row["category_source"] in _PROTECTED:
                continue
            card_side = is_card[row["account_id"]]
            if card_side and row["direction"] != "credit":
                continue  # a card purchase is spending, never a transfer
            if not card_side and row["channel"] not in _TRANSFER_CHANNELS:
                continue
            d = date.fromisoformat(row["txn_date"])
            other_dir = "credit" if row["direction"] == "debit" else "debit"
            exclude = f"AND id NOT IN ({','.join('?' * len(marked))})" if marked else ""
            candidates = conn.execute(
                f"""SELECT id, account_id, txn_date, direction, category_id, narration_raw FROM transactions
                    WHERE account_id != ? AND direction = ? AND amount_paise = ? AND linked_txn_id IS NULL
                      AND txn_date BETWEEN ? AND ? AND category_source NOT IN ('manual', 'rule') {exclude}
                    ORDER BY abs(julianday(txn_date) - julianday(?)) LIMIT 10""",
                (
                    row["account_id"], other_dir, row["amount_paise"],
                    (d - timedelta(days=CARD_DAYS)).isoformat(), (d + timedelta(days=CARD_DAYS)).isoformat(),
                    *marked, row["txn_date"],
                ),
            ).fetchall()
            match = next((c for c in candidates if _pairs(row, c, is_card, card_id)), None)
            if not match:
                continue
            cat = card_id if (card_side or is_card[match["account_id"]]) else self_id
            _mark(conn, row["id"], cat, match["id"])
            _mark(conn, match["id"], cat, row["id"])
            marked.update({row["id"], match["id"]})

    # 2. Payments to/from a counterparty carrying your own name.
    names = [set(a["holder_name"].upper().split()) for a in accounts.values() if a.get("holder_name")]
    names = [n for n in names if n]
    if names:
        for row in rows:
            if row["id"] in marked or row["category_source"] in _PROTECTED or row["channel"] not in _TRANSFER_CHANNELS:
                continue
            if is_card[row["account_id"]]:
                continue
            words = set((row["counterparty"] or "").upper().split())
            if any(len(n) >= 2 and n <= words or (len(n) == 1 and n == words) for n in names):
                _mark(conn, row["id"], self_id)
                marked.add(row["id"])
    return len(marked)


def _pairs(row, other, is_card: dict[int, bool], card_id: int | None) -> bool:
    """Whether two same-amount, opposite-direction rows in different accounts are one transfer."""
    days = abs((date.fromisoformat(row["txn_date"]) - date.fromisoformat(other["txn_date"])).days)
    row_card, other_card = is_card[row["account_id"]], is_card[other["account_id"]]
    if not (row_card or other_card):
        return days <= BANK_DAYS
    if row_card and other_card:
        return False
    card_row, bank_row = (row, other) if row_card else (other, row)
    if days > CARD_DAYS or card_row["direction"] != "credit":
        return False  # money paid *into* the card; a purchase never pairs
    # The card credit must look like a bill payment (not a refund), unless the bank side is already known to be one.
    return looks_like_card_payment(card_row["narration_raw"]) or (card_id is not None and bank_row["category_id"] == card_id)
