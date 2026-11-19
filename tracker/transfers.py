"""Spot money moving between your own accounts so it isn't counted as spending.

Two signals:
  * a debit in one account matched by a credit of the same amount in another of your
    accounts within ±2 days (a card account on either side -> Credit Card Payment);
  * a UPI/NEFT/IMPS counterparty whose name contains your account holder name.
Your own rules and manual choices always win over this.
"""

import sqlite3
from datetime import date, timedelta

from tracker import db

_TRANSFER_CHANNELS = ("UPI", "NEFT", "IMPS", "RTGS", "TRANSFER", "CHQ", "OTHER")
_PROTECTED = ("manual", "rule")  # never override what you decided


def _mark(conn, txn_id: int, category_id: int, linked: int | None = None) -> None:
    conn.execute(
        """UPDATE transactions SET category_id = ?, category_source = 'transfer', rule_id = NULL, confidence = 1.0,
           needs_review = 0, linked_txn_id = COALESCE(?, linked_txn_id) WHERE id = ?""",
        (category_id, linked, txn_id),
    )


def detect_transfers(conn: sqlite3.Connection, txn_ids: list[int]) -> int:
    if not txn_ids:
        return 0
    self_id = db.category_id_by_name(conn, "Self Transfer")
    card_id = db.category_id_by_name(conn, "Credit Card Payment") or self_id
    if self_id is None:
        return 0
    accounts = {r["id"]: dict(r) for r in conn.execute("SELECT id, type, holder_name FROM accounts")}
    marked: set[int] = set()

    placeholders = ",".join("?" * len(txn_ids))
    rows = conn.execute(
        f"""SELECT id, account_id, txn_date, amount_paise, direction, channel, counterparty, category_source, linked_txn_id
            FROM transactions WHERE id IN ({placeholders})""",
        txn_ids,
    ).fetchall()

    # 1. Matching debit/credit pairs across your accounts.
    if len(accounts) > 1:
        for row in rows:
            if row["id"] in marked or row["linked_txn_id"] or row["category_source"] in _PROTECTED:
                continue
            if row["channel"] not in _TRANSFER_CHANNELS and accounts[row["account_id"]]["type"] != "credit_card":
                continue
            d = date.fromisoformat(row["txn_date"])
            other_dir = "credit" if row["direction"] == "debit" else "debit"
            exclude = f"AND id NOT IN ({','.join('?' * len(marked))})" if marked else ""
            match = conn.execute(
                f"""SELECT id, account_id FROM transactions
                    WHERE account_id != ? AND direction = ? AND amount_paise = ? AND linked_txn_id IS NULL
                      AND txn_date BETWEEN ? AND ? AND category_source NOT IN ('manual', 'rule') {exclude}
                    ORDER BY abs(julianday(txn_date) - julianday(?)) LIMIT 1""",
                (
                    row["account_id"], other_dir, row["amount_paise"],
                    (d - timedelta(days=2)).isoformat(), (d + timedelta(days=2)).isoformat(),
                    *marked, row["txn_date"],
                ),
            ).fetchone()
            if not match:
                continue
            is_card = "credit_card" in (accounts[row["account_id"]]["type"], accounts[match["account_id"]]["type"])
            cat = card_id if is_card else self_id
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
            words = set((row["counterparty"] or "").upper().split())
            if any(len(n) >= 2 and n <= words or (len(n) == 1 and n == words) for n in names):
                _mark(conn, row["id"], self_id)
                marked.add(row["id"])
    return len(marked)
