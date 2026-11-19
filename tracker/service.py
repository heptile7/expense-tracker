"""Operations shared by the web UI and the MCP server, so both teach the categorizer the same way."""

import logging
import re
import sqlite3
from datetime import date

import config
from tracker import db, importer
from tracker.categorizer import categorize, compile_rule
from tracker.llm import Classifier

log = logging.getLogger(__name__)


class NotFound(LookupError):
    pass


def rupees(paise: int | None) -> float | None:
    return None if paise is None else round(paise / 100, 2)


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def _resolve_category(conn: sqlite3.Connection, category: int | str) -> int:
    if isinstance(category, int) or (isinstance(category, str) and category.isdigit()):
        row = conn.execute("SELECT id FROM categories WHERE id = ?", (int(category),)).fetchone()
        if row:
            return row["id"]
    elif isinstance(category, str):
        cid = db.category_id_by_name(conn, category)
        if cid:
            return cid
    names = ", ".join(r["name"] for r in conn.execute("SELECT name FROM categories WHERE is_active = 1 ORDER BY name"))
    raise NotFound(f"Unknown category {category!r}. Valid categories: {names}")


# ── Categories ────────────────────────────────────────────────────────────────


GROUPS = ("needs", "wants", "savings", "income", "transfer")


def list_categories(include_inactive: bool = False) -> list[dict]:
    """Main categories, each followed by its sub-categories."""
    with db.session() as conn:
        rows = conn.execute(
            f"""SELECT c.*, p.name AS parent_name, COUNT(t.id) AS txn_count FROM categories c
                LEFT JOIN categories p ON p.id = c.parent_id
                LEFT JOIN transactions t ON t.category_id = c.id
                {"" if include_inactive else "WHERE c.is_active = 1"}
                GROUP BY c.id
                ORDER BY CASE c.kind WHEN 'expense' THEN 0 WHEN 'income' THEN 1 ELSE 2 END,
                         COALESCE(p.name, c.name), c.parent_id IS NOT NULL, c.name"""
        ).fetchall()
        return [dict(r) for r in rows]


def create_category(name: str, kind: str = "expense", color: str | None = None, description: str | None = None,
                    parent_id: int | None = None, grp: str | None = None) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Category name is required")
    if kind not in ("expense", "income", "transfer"):
        raise ValueError("kind must be expense, income or transfer")
    with db.session() as conn:
        if db.category_id_by_name(conn, name):
            raise ValueError(f"Category {name!r} already exists")
        parent = _check_parent(conn, None, parent_id) if parent_id else None
        if parent:
            kind, color, grp = parent["kind"], color or parent["color"], parent["grp"]
        grp = grp or {"income": "income", "transfer": "transfer"}.get(kind, "wants")
        if grp not in GROUPS:
            raise ValueError(f"group must be one of {', '.join(GROUPS)}")
        cur = conn.execute(
            "INSERT INTO categories (name, kind, color, description, parent_id, grp) VALUES (?, ?, ?, ?, ?, ?)",
            (name, kind, color or "#868e96", description, parent_id or None, grp),
        )
        return _row(conn.execute("SELECT * FROM categories WHERE id = ?", (cur.lastrowid,)).fetchone())


def _check_parent(conn: sqlite3.Connection, category_id: int | None, parent_id: int) -> sqlite3.Row:
    parent = conn.execute("SELECT * FROM categories WHERE id = ?", (parent_id,)).fetchone()
    if not parent:
        raise NotFound(f"Category {parent_id} not found")
    if parent["parent_id"]:
        raise ValueError("Sub-categories can't have sub-categories of their own")
    if category_id is not None:
        if parent_id == category_id:
            raise ValueError("A category can't be its own parent")
        if conn.execute("SELECT 1 FROM categories WHERE parent_id = ?", (category_id,)).fetchone():
            raise ValueError("This category has sub-categories, so it can't become one")
    return parent


def update_category(category_id: int, **fields) -> dict:
    allowed = {k: v for k, v in fields.items() if k in {"name", "kind", "color", "description", "is_active", "grp"} and v is not None}
    if "parent_id" in fields:  # None/"" makes it a main category again
        allowed["parent_id"] = int(fields["parent_id"]) if fields["parent_id"] not in (None, "", 0) else None
    if "grp" in allowed and allowed["grp"] not in GROUPS:
        raise ValueError(f"group must be one of {', '.join(GROUPS)}")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if not row:
            raise NotFound(f"Category {category_id} not found")
        if row["is_system"] and ("name" in allowed or allowed.get("is_active") in (0, False)):
            raise ValueError(f"{row['name']} is built in and can't be renamed or hidden")
        if "kind" in allowed and allowed["kind"] not in ("expense", "income", "transfer"):
            raise ValueError("kind must be expense, income or transfer")
        if "name" in allowed:
            allowed["name"] = allowed["name"].strip()
            other = db.category_id_by_name(conn, allowed["name"])
            if other and other != category_id:
                raise ValueError(f"Category {allowed['name']!r} already exists. Use merge instead.")
        if "is_active" in allowed:
            allowed["is_active"] = int(bool(allowed["is_active"]))
        if allowed.get("parent_id"):
            parent = _check_parent(conn, category_id, allowed["parent_id"])
            allowed["kind"], allowed["grp"] = parent["kind"], parent["grp"]
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            conn.execute(f"UPDATE categories SET {sets} WHERE id = ?", (*allowed.values(), category_id))
        if "grp" in allowed and not row["parent_id"]:  # sub-categories follow their parent's group
            conn.execute("UPDATE categories SET grp = ? WHERE parent_id = ?", (allowed["grp"], category_id))
        return _row(conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone())


# ── Budgets ───────────────────────────────────────────────────────────────────


def list_budgets() -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            """SELECT b.category_id, c.name, b.monthly_paise, b.updated_at FROM budgets b
               JOIN categories c ON c.id = b.category_id ORDER BY c.name"""
        ).fetchall()
    return [{"category_id": r["category_id"], "category": r["name"], "monthly": rupees(r["monthly_paise"]), "updated_at": r["updated_at"]} for r in rows]


def set_budget(category: int | str, monthly: float | None) -> dict:
    """Monthly budget for a main category (covers its sub-categories). None or 0 removes it."""
    with db.session() as conn:
        cid = _resolve_category(conn, category)
        cat = conn.execute("SELECT name, parent_id FROM categories WHERE id = ?", (cid,)).fetchone()
        if cat["parent_id"]:
            raise ValueError("Set budgets on main categories; they cover their sub-categories")
        if not monthly:
            conn.execute("DELETE FROM budgets WHERE category_id = ?", (cid,))
            return {"category": cat["name"], "monthly": None}
        if monthly < 0:
            raise ValueError("A budget can't be negative")
        conn.execute(
            """INSERT INTO budgets (category_id, monthly_paise) VALUES (?, ?)
               ON CONFLICT (category_id) DO UPDATE SET monthly_paise = excluded.monthly_paise, updated_at = CURRENT_TIMESTAMP""",
            (cid, int(round(monthly * 100))),
        )
        return {"category": cat["name"], "monthly": round(float(monthly), 2)}


def merge_categories(source_id: int, target_id: int) -> dict:
    if source_id == target_id:
        raise ValueError("Pick two different categories")
    with db.session() as conn:
        src = conn.execute("SELECT * FROM categories WHERE id = ?", (source_id,)).fetchone()
        dst = conn.execute("SELECT * FROM categories WHERE id = ?", (target_id,)).fetchone()
        if not src or not dst:
            raise NotFound("Category not found")
        if src["is_system"]:
            raise ValueError(f"{src['name']} is built in and can't be merged away")
        moved = conn.execute("UPDATE transactions SET category_id = ? WHERE category_id = ?", (target_id, source_id)).rowcount
        conn.execute("UPDATE merchant_rules SET category_id = ? WHERE category_id = ?", (target_id, source_id))
        conn.execute("DELETE FROM categories WHERE id = ?", (source_id,))
        return {"moved_transactions": moved, "into": dst["name"]}


# ── Accounts ──────────────────────────────────────────────────────────────────

_ACCOUNT_FIELDS = ("name", "bank", "type", "last4", "holder_name", "gmail_query")


def list_accounts() -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            """SELECT a.*, COUNT(t.id) AS txn_count, MAX(t.txn_date) AS last_txn_date
               FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id GROUP BY a.id ORDER BY a.name"""
        ).fetchall()
        return [dict(r) for r in rows]


def _clean_account(fields: dict) -> dict:
    out = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items() if k in _ACCOUNT_FIELDS}
    out = {k: (v or None) for k, v in out.items()}
    if out.get("last4"):
        digits = re.sub(r"\D", "", out["last4"])
        if len(digits) < 4:
            raise ValueError("last4 should be the last 4 digits of the account/card number")
        out["last4"] = digits[-4:]
    if out.get("type") and out["type"] not in ("savings", "current", "credit_card"):
        raise ValueError("type must be savings, current or credit_card")
    return out


def create_account(**fields) -> dict:
    data = _clean_account(fields)
    if not data.get("name") or not data.get("bank"):
        raise ValueError("Account name and bank are required")
    data.setdefault("type", "savings")
    with db.session() as conn:
        cols = ", ".join(data)
        cur = conn.execute(f"INSERT INTO accounts ({cols}) VALUES ({', '.join('?' * len(data))})", tuple(data.values()))
        return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_account(account_id: int, **fields) -> dict:
    data = _clean_account({k: v for k, v in fields.items() if v is not None})
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            raise NotFound(f"Account {account_id} not found")
        if data:
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE accounts SET {sets} WHERE id = ?", (*data.values(), account_id))
        return _row(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())


def delete_account(account_id: int) -> dict:
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account_id,)).fetchone()[0]
        if not conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,)).rowcount:
            raise NotFound(f"Account {account_id} not found")
        return {"deleted_transactions": n}


# ── Statements / import ───────────────────────────────────────────────────────


def import_file(path, account_id=None, password=None, filename=None, source="upload", classifier: Classifier | None = None):
    return importer.import_file(path, account_id, password, filename, source, classifier=classifier)


def list_statements() -> list[dict]:
    with db.session() as conn:
        rows = conn.execute(
            """SELECT s.*, a.name AS account_name FROM statements s JOIN accounts a ON a.id = s.account_id
               ORDER BY s.imported_at DESC, s.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def delete_statement(statement_id: int) -> dict:
    """Undo an import: removes the statement and the transactions it added."""
    with db.session() as conn:
        n = conn.execute("DELETE FROM transactions WHERE statement_id = ?", (statement_id,)).rowcount
        if not conn.execute("DELETE FROM statements WHERE id = ?", (statement_id,)).rowcount:
            raise NotFound(f"Statement {statement_id} not found")
        return {"deleted_transactions": n}


# ── Transactions ──────────────────────────────────────────────────────────────

_TXN_SELECT = """
SELECT t.id, t.account_id, a.name AS account_name, t.txn_date, t.narration_raw, t.channel, t.counterparty, t.vpa,
       t.merchant_key, t.amount_paise, t.direction, t.balance_paise, t.category_id, c.name AS category,
       c.kind AS category_kind, c.color AS category_color, t.category_source, t.confidence, t.needs_review,
       t.rule_id, t.note, t.linked_txn_id
FROM transactions t
JOIN accounts a ON a.id = t.account_id
LEFT JOIN categories c ON c.id = t.category_id
"""


def _txn_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["amount"] = rupees(d.pop("amount_paise"))
    d["balance"] = rupees(d.pop("balance_paise"))
    d["needs_review"] = bool(d["needs_review"])
    return d


def _filters(f: dict) -> tuple[str, list]:
    where, params = [], []
    if f.get("account_id"):
        where.append("t.account_id = ?")
        params.append(int(f["account_id"]))
    if f.get("month"):
        where.append("substr(t.txn_date, 1, 7) = ?")
        params.append(f["month"])
    if f.get("start_date"):
        where.append("t.txn_date >= ?")
        params.append(f["start_date"])
    if f.get("end_date"):
        where.append("t.txn_date <= ?")
        params.append(f["end_date"])
    if f.get("category_id"):  # a main category includes its sub-categories
        where.append("(t.category_id = ? OR t.category_id IN (SELECT id FROM categories WHERE parent_id = ?))")
        params += [int(f["category_id"]), int(f["category_id"])]
    if f.get("category"):
        where.append("(lower(c.name) = lower(?) OR c.parent_id IN (SELECT id FROM categories WHERE lower(name) = lower(?)))")
        params += [f["category"], f["category"]]
    if f.get("source"):
        where.append("t.category_source = ?")
        params.append(f["source"])
    if f.get("direction"):
        where.append("t.direction = ?")
        params.append(f["direction"])
    if f.get("needs_review"):
        where.append("t.needs_review = 1")
    if f.get("merchant_key"):
        where.append("t.merchant_key = ?")
        params.append(f["merchant_key"])
    if f.get("min_amount") is not None:
        where.append("t.amount_paise >= ?")
        params.append(int(round(float(f["min_amount"]) * 100)))
    if f.get("max_amount") is not None:
        where.append("t.amount_paise <= ?")
        params.append(int(round(float(f["max_amount"]) * 100)))
    if f.get("q"):
        where.append("(t.narration_raw LIKE ? OR t.counterparty LIKE ? OR t.note LIKE ?)")
        like = f"%{f['q']}%"
        params += [like, like, like]
    return (" WHERE " + " AND ".join(where)) if where else "", params


def list_transactions(limit: int = 200, offset: int = 0, sort: str = "date_desc", **filters) -> dict:
    order = {
        "date_desc": "t.txn_date DESC, t.id DESC",
        "date_asc": "t.txn_date ASC, t.id ASC",
        "amount_desc": "t.amount_paise DESC",
        "amount_asc": "t.amount_paise ASC",
        "confidence": "COALESCE(t.confidence, 0) ASC, t.txn_date DESC",
    }.get(sort, "t.txn_date DESC, t.id DESC")
    where, params = _filters(filters)
    with db.session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM transactions t LEFT JOIN categories c ON c.id = t.category_id{where}", params
        ).fetchone()[0]
        rows = conn.execute(f"{_TXN_SELECT}{where} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"total": total, "items": [_txn_dict(r) for r in rows]}


def get_transaction(txn_id: int) -> dict:
    with db.session() as conn:
        row = conn.execute(f"{_TXN_SELECT} WHERE t.id = ?", (txn_id,)).fetchone()
        if not row:
            raise NotFound(f"Transaction {txn_id} not found")
        out = _txn_dict(row)
        out["similar_count"] = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE merchant_key = ? AND id != ?", (row["merchant_key"], txn_id)
        ).fetchone()[0]
        return out


def set_category(txn_id: int, category: int | str, scope: str = "merchant") -> dict:
    """Correct a category.

    scope="merchant": remember it for this payee. Saves a user rule for the merchant key and
    re-categorizes every past transaction with that key (except one-off manual choices);
    future imports use it too.
    scope="single": change just this transaction (e.g. a one-off payment to a person).
    """
    if scope not in ("merchant", "single"):
        raise ValueError("scope must be 'merchant' or 'single'")
    with db.session() as conn:
        txn = conn.execute("SELECT id, merchant_key, counterparty FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if not txn:
            raise NotFound(f"Transaction {txn_id} not found")
        category_id = _resolve_category(conn, category)
        category_name = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()["name"]

        if scope == "single":
            conn.execute(
                """UPDATE transactions SET category_id = ?, category_source = 'manual', rule_id = NULL, confidence = NULL,
                   needs_review = 0 WHERE id = ?""",
                (category_id, txn_id),
            )
            log.info("Transaction %s set to category %s (one-off)", txn_id, category_id)
            return {"scope": scope, "updated": 1, "category": category_name, "counterparty": txn["counterparty"]}

        rule_id = upsert_user_exact_rule(conn, txn["merchant_key"], category_id)
        # This transaction follows the rule from now on, even if it was a one-off before.
        conn.execute("UPDATE transactions SET category_source = 'fallback' WHERE id = ?", (txn_id,))
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM transactions WHERE merchant_key = ? AND category_source != 'manual'", (txn["merchant_key"],)
            )
        ]
        categorize(conn, ids, use_llm=False)
        log.info("Rule %s: payee of transaction %s now category %s; %d transactions updated", rule_id, txn_id, category_id, len(ids))
        return {
            "scope": scope,
            "updated": len(ids),
            "category": category_name,
            "counterparty": txn["counterparty"],
            "merchant_key": txn["merchant_key"],
            "rule_id": rule_id,
        }


def upsert_user_exact_rule(conn: sqlite3.Connection, merchant_key: str, category_id: int) -> int:
    conn.execute(
        """INSERT INTO merchant_rules (match_type, pattern, direction, category_id, source)
           VALUES ('exact', ?, 'any', ?, 'user')
           ON CONFLICT (match_type, pattern, direction) DO UPDATE SET
             category_id = excluded.category_id, source = 'user', confidence = NULL, updated_at = CURRENT_TIMESTAMP""",
        (merchant_key, category_id),
    )
    return conn.execute(
        "SELECT id FROM merchant_rules WHERE match_type = 'exact' AND pattern = ? AND direction = 'any'", (merchant_key,)
    ).fetchone()["id"]


def set_note(txn_id: int, note: str | None) -> dict:
    with db.session() as conn:
        if not conn.execute("UPDATE transactions SET note = ? WHERE id = ?", ((note or "").strip() or None, txn_id)).rowcount:
            raise NotFound(f"Transaction {txn_id} not found")
    return {"id": txn_id, "note": note}


def accept_categories(txn_ids: list[int]) -> dict:
    """Confirm Claude's picks: clears the review flag and turns each pick into your own rule."""
    accepted = 0
    with db.session() as conn:
        for txn_id in txn_ids:
            txn = conn.execute(
                "SELECT id, merchant_key, category_id, category_source FROM transactions WHERE id = ?", (txn_id,)
            ).fetchone()
            if not txn:
                continue
            if txn["category_source"] in ("llm", "local", "fallback", "seed"):
                upsert_user_exact_rule(conn, txn["merchant_key"], txn["category_id"])
                conn.execute(
                    """UPDATE transactions SET needs_review = 0, category_source = 'rule', confidence = 1.0,
                       rule_id = (SELECT id FROM merchant_rules WHERE match_type = 'exact' AND pattern = ? AND direction = 'any')
                       WHERE merchant_key = ? AND category_source IN ('llm', 'local', 'fallback', 'seed') AND category_id = ?""",
                    (txn["merchant_key"], txn["merchant_key"], txn["category_id"]),
                )
            conn.execute("UPDATE transactions SET needs_review = 0 WHERE id = ?", (txn_id,))
            accepted += 1
    return {"accepted": accepted}


def recategorize(scope: str = "uncategorized", classifier: Classifier | None = None) -> dict:
    """scope='uncategorized': retry Claude for Uncategorized rows and offline guesses. scope='all': re-apply rules everywhere."""
    with db.session() as conn:
        if scope == "refine":
            # Forget Claude's earlier (coarser) answers so every payee not covered by your own rules
            # is asked again against the current categories and sub-categories.
            conn.execute("DELETE FROM merchant_rules WHERE source = 'llm'")
            stats = categorize(conn, None, classifier=classifier, use_llm=True)
        elif scope == "uncategorized":
            ids = [r["id"] for r in conn.execute("SELECT id FROM transactions WHERE category_source IN ('fallback', 'local')")]
            stats = categorize(conn, ids, classifier=classifier, use_llm=True)
        else:
            stats = categorize(conn, None, classifier=classifier, use_llm=True)
        return stats.as_dict()


# ── Rules ─────────────────────────────────────────────────────────────────────


def list_rules(source: str | None = None, q: str | None = None) -> list[dict]:
    where, params = [], []
    if source:
        where.append("r.source = ?")
        params.append(source)
    if q:
        where.append("r.pattern LIKE ?")
        params.append(f"%{q.upper()}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.session() as conn:
        rows = conn.execute(
            f"""SELECT r.*, c.name AS category, c.color AS category_color,
                       (SELECT COUNT(*) FROM transactions t WHERE t.rule_id = r.id) AS txn_count
                FROM merchant_rules r JOIN categories c ON c.id = r.category_id{clause}
                ORDER BY CASE r.source WHEN 'user' THEN 0 WHEN 'llm' THEN 1 ELSE 2 END, r.updated_at DESC, r.id DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def _validate_rule(match_type: str, pattern: str, direction: str) -> str:
    if match_type not in ("exact", "contains", "regex"):
        raise ValueError("match_type must be exact, contains or regex")
    if direction not in ("any", "debit", "credit"):
        raise ValueError("direction must be any, debit or credit")
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("Pattern is required")
    if match_type != "regex":
        pattern = pattern.upper()
    try:
        compile_rule(match_type, pattern, "user")
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    return pattern


def create_rule(pattern: str, category: int | str, match_type: str = "contains", direction: str = "any") -> dict:
    pattern = _validate_rule(match_type, pattern, direction)
    with db.session() as conn:
        category_id = _resolve_category(conn, category)
        conn.execute(
            """INSERT INTO merchant_rules (match_type, pattern, direction, category_id, source) VALUES (?, ?, ?, ?, 'user')
               ON CONFLICT (match_type, pattern, direction) DO UPDATE SET category_id = excluded.category_id,
                 source = 'user', confidence = NULL, updated_at = CURRENT_TIMESTAMP""",
            (match_type, pattern, direction, category_id),
        )
        rule = conn.execute(
            "SELECT * FROM merchant_rules WHERE match_type = ? AND pattern = ? AND direction = ?",
            (match_type, pattern, direction),
        ).fetchone()
        stats = categorize(conn, None, use_llm=False)
        affected = conn.execute("SELECT COUNT(*) FROM transactions WHERE rule_id = ?", (rule["id"],)).fetchone()[0]
        return {"rule": dict(rule), "matched_transactions": affected, "recategorized": stats.total}


def update_rule(rule_id: int, category: int | str | None = None, pattern: str | None = None,
                match_type: str | None = None, direction: str | None = None) -> dict:
    with db.session() as conn:
        rule = conn.execute("SELECT * FROM merchant_rules WHERE id = ?", (rule_id,)).fetchone()
        if not rule:
            raise NotFound(f"Rule {rule_id} not found")
        mt = match_type or rule["match_type"]
        dr = direction or rule["direction"]
        pat = _validate_rule(mt, pattern if pattern is not None else rule["pattern"], dr)
        cid = _resolve_category(conn, category) if category is not None else rule["category_id"]
        conn.execute(
            """UPDATE merchant_rules SET match_type = ?, pattern = ?, direction = ?, category_id = ?, source = 'user',
               confidence = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (mt, pat, dr, cid, rule_id),
        )
        categorize(conn, None, use_llm=False)
        return dict(conn.execute("SELECT * FROM merchant_rules WHERE id = ?", (rule_id,)).fetchone())


def delete_rule(rule_id: int, classifier: Classifier | None = None) -> dict:
    """Delete a rule; the transactions it categorized fall through to the next rule (or Claude)."""
    with db.session() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM transactions WHERE rule_id = ? AND category_source != 'manual'", (rule_id,))]
        if not conn.execute("DELETE FROM merchant_rules WHERE id = ?", (rule_id,)).rowcount:
            raise NotFound(f"Rule {rule_id} not found")
        if ids:
            conn.execute(f"UPDATE transactions SET category_source = 'fallback' WHERE id IN ({','.join('?' * len(ids))})", ids)
        stats = categorize(conn, ids, classifier=classifier, use_llm=True)
        return {"recategorized": len(ids), "stats": stats.as_dict()}


# ── Reports ───────────────────────────────────────────────────────────────────


def _month_shift(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def spending_summary(start_date: str | None = None, end_date: str | None = None, group_by: str = "category",
                     account_id: int | None = None, kind: str | None = None) -> list[dict]:
    """Totals in rupees. Expense rows are net of refunds booked to the same category; transfers are excluded."""
    group_expr = {
        "category": "COALESCE(p.name, c.name)",  # sub-categories roll up into their main category
        "subcategory": "c.name",
        "merchant": "t.counterparty",
        "month": "substr(t.txn_date, 1, 7)",
        "account": "a.name",
    }.get(group_by)
    if not group_expr:
        raise ValueError("group_by must be category, subcategory, merchant, month or account")
    where = ["c.kind != 'transfer'"]
    params: list = []
    if start_date:
        where.append("t.txn_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("t.txn_date <= ?")
        params.append(end_date)
    if account_id:
        where.append("t.account_id = ?")
        params.append(account_id)
    if kind:
        where.append("c.kind = ?")
        params.append(kind)
    # Uncategorized money coming in is income-like; it must not reduce spending.
    kind_expr = "CASE WHEN c.is_system = 1 AND t.direction = 'credit' THEN 'income' ELSE c.kind END"
    if kind:
        where[-1] = f"{kind_expr} = ?"
    with db.session() as conn:
        rows = conn.execute(
            f"""SELECT {group_expr} AS label, {kind_expr} AS kind,
                       SUM(CASE WHEN t.direction = 'debit' THEN t.amount_paise ELSE 0 END) AS debit,
                       SUM(CASE WHEN t.direction = 'credit' THEN t.amount_paise ELSE 0 END) AS credit,
                       COUNT(*) AS n, MAX(COALESCE(p.color, c.color)) AS color
                FROM transactions t JOIN categories c ON c.id = t.category_id JOIN accounts a ON a.id = t.account_id
                LEFT JOIN categories p ON p.id = c.parent_id
                WHERE {" AND ".join(where)}
                GROUP BY {group_expr}, {kind_expr}""",
            params,
        ).fetchall()
    out = []
    for r in rows:
        spent = (r["debit"] or 0) - (r["credit"] or 0) if r["kind"] == "expense" else (r["credit"] or 0) - (r["debit"] or 0)
        out.append({"label": r["label"] or "(unknown)", "kind": r["kind"], "amount": rupees(spent), "debits": rupees(r["debit"]),
                    "credits": rupees(r["credit"]), "count": r["n"], "color": r["color"] if group_by == "category" else None})
    out.sort(key=lambda x: (x["kind"] != "expense", -(x["amount"] or 0)) if group_by != "month" else (x["label"], x["kind"]))
    return out


def available_months() -> list[str]:
    with db.session() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT substr(txn_date, 1, 7) FROM transactions ORDER BY 1 DESC")]


def dashboard(month: str | None = None, account_id: int | None = None) -> dict:
    months = available_months()
    month = month or (months[0] if months else date.today().strftime("%Y-%m"))
    start, end = f"{month}-01", f"{month}-31"
    by_cat = spending_summary(start, end, "category", account_id)
    expense = [r for r in by_cat if r["kind"] == "expense" and (r["amount"] or 0) > 0]
    income = [r for r in by_cat if r["kind"] == "income" and (r["amount"] or 0) > 0]
    first = _month_shift(month, -11)
    trend_rows = spending_summary(f"{first}-01", end, "month", account_id)
    trend = {}
    for i in range(12):
        m = _month_shift(first, i)
        trend[m] = {"month": m, "expense": 0.0, "income": 0.0}
    for r in trend_rows:
        if r["label"] in trend:
            trend[r["label"]][r["kind"]] = r["amount"] or 0.0
    merchants = [r for r in spending_summary(start, end, "merchant", account_id, kind="expense") if (r["amount"] or 0) > 0][:10]
    with db.session() as conn:
        review = conn.execute("SELECT COUNT(*) FROM transactions WHERE needs_review = 1").fetchone()[0]
        llm = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE error IS NULL").fetchone()
        transfers = conn.execute(
            """SELECT COALESCE(SUM(t.amount_paise), 0) FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE c.kind = 'transfer' AND t.direction = 'debit' AND t.txn_date BETWEEN ? AND ?""" +
            (" AND t.account_id = ?" if account_id else ""),
            (start, end, account_id) if account_id else (start, end),
        ).fetchone()[0]
    total_exp = round(sum(r["amount"] for r in expense), 2)
    total_inc = round(sum(r["amount"] for r in income), 2)
    return {
        "month": month,
        "months": months,
        "total_expense": total_exp,
        "total_income": total_inc,
        "net": round(total_inc - total_exp, 2),
        "transfers_out": rupees(transfers),
        "by_category": expense,
        "income_by_category": income,
        "trend": list(trend.values()),
        "top_merchants": merchants,
        "needs_review": review,
        "llm_calls": llm[0],
        "llm_cost_usd": round(llm[1], 4),
    }


def llm_usage(limit: int = 20) -> dict:
    with db.session() as conn:
        tot = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_items),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE error IS NULL"
        ).fetchone()
        sub = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_items),0) FROM llm_calls WHERE error IS NULL AND model LIKE 'claude-code/%'"
        ).fetchone()
        local = conn.execute("SELECT COUNT(*) FROM transactions WHERE category_source = 'local'").fetchone()[0]
        recent = [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,))]
    return {
        "model": config.MODEL_ID,
        "calls": tot[0], "merchants": tot[1], "input_tokens": tot[2], "output_tokens": tot[3],
        "cost_usd": round(tot[4], 6),
        "subscription_calls": sub[0], "subscription_merchants": sub[1],
        "api_calls": tot[0] - sub[0],
        "local_guesses": local,
        "recent": recent,
    }


# ── Categorizer settings ──────────────────────────────────────────────────────


def categorizer_settings(check_cli_login: bool = False) -> dict:
    from tracker import llm

    with db.session() as conn:
        mode = db.get_setting(conn, "categorizer", "auto")
    cli = llm.claude_code_status() if check_cli_login else {"installed": bool(llm.shutil.which("claude"))}
    clf = llm.build_classifier(mode)
    chain = [b.label for b in clf.backends] if clf else []
    return {
        "mode": mode,
        "modes": llm.MODES,
        "api_key_set": llm.api_key_set(),
        "claude_cli": cli,
        "chain": chain + ["Offline categorizer"],
        "model": config.MODEL_ID,
    }


def set_categorizer_mode(mode: str) -> dict:
    from tracker import llm

    if mode not in llm.MODES:
        raise ValueError(f"mode must be one of: {', '.join(llm.MODES)}")
    with db.session() as conn:
        db.set_setting(conn, "categorizer", mode)
    return categorizer_settings()
