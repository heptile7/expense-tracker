"""SQLite schema, connection handling and seeding. Amounts are integer paise."""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import config

SCHEMA_VERSION = 4  # 2: 'local' category_source, app_settings; 3: sub-categories, groups, budgets, plans;
                    # 4: credit card statements (bill box on statements)
SEED_RULES_VERSION = "3"  # built-in keyword rules. 2: sub-categories; 3: "PAID VIA CRED" is a purchase, card fees

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    bank         TEXT NOT NULL,                       -- HDFC | CUB | OTHER
    type         TEXT NOT NULL DEFAULT 'savings',     -- savings | current | credit_card
    last4        TEXT,
    holder_name  TEXT,                                -- used to spot transfers to yourself
    gmail_query  TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'transfer')),
    color        TEXT,
    description  TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    is_system    INTEGER NOT NULL DEFAULT 0,
    parent_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,  -- sub-category of
    grp          TEXT                                 -- needs | wants | savings | income | transfer
);

CREATE TABLE IF NOT EXISTS budgets (
    category_id   INTEGER PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
    monthly_paise INTEGER NOT NULL CHECK (monthly_paise >= 0),
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS money_plans (
    id           INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    backend      TEXT,
    plan_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS statements (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source       TEXT NOT NULL DEFAULT 'upload',      -- upload | gmail
    filename     TEXT,
    file_sha256  TEXT UNIQUE,
    period_start TEXT,
    period_end   TEXT,
    n_rows       INTEGER,
    n_new        INTEGER,
    imported_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    kind            TEXT NOT NULL DEFAULT 'bank',     -- bank | credit_card
    statement_date  TEXT,                             -- credit cards: the bill box
    due_date        TEXT,
    total_due_paise INTEGER,
    min_due_paise   INTEGER
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    id           INTEGER PRIMARY KEY,
    match_type   TEXT NOT NULL CHECK (match_type IN ('exact', 'contains', 'regex')),
    pattern      TEXT NOT NULL,
    direction    TEXT NOT NULL DEFAULT 'any' CHECK (direction IN ('debit', 'credit', 'any')),
    category_id  INTEGER NOT NULL REFERENCES categories(id),
    source       TEXT NOT NULL CHECK (source IN ('user', 'llm', 'seed')),
    confidence   REAL,
    hit_count    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (match_type, pattern, direction)
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    statement_id    INTEGER REFERENCES statements(id) ON DELETE SET NULL,
    txn_date        TEXT NOT NULL,                    -- ISO yyyy-mm-dd
    narration_raw   TEXT NOT NULL,
    channel         TEXT,
    counterparty    TEXT,
    vpa             TEXT,
    merchant_key    TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL CHECK (amount_paise >= 0),
    direction       TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    balance_paise   INTEGER,
    ref_no          TEXT,
    fingerprint     TEXT NOT NULL UNIQUE,
    category_id     INTEGER REFERENCES categories(id),
    category_source TEXT NOT NULL DEFAULT 'fallback'
                    CHECK (category_source IN ('manual', 'rule', 'seed', 'llm', 'local', 'transfer', 'fallback')),
    rule_id         INTEGER REFERENCES merchant_rules(id) ON DELETE SET NULL,
    confidence      REAL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    linked_txn_id   INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_txn_merchant ON transactions(merchant_key);
CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_category ON transactions(category_id);
CREATE INDEX IF NOT EXISTS ix_txn_account_date ON transactions(account_id, txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_rule ON transactions(rule_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model          TEXT NOT NULL,
    n_items        INTEGER NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    stop_reason    TEXT,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS fetched_messages (
    message_id    TEXT PRIMARY KEY,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                      -- statement
    status        TEXT NOT NULL,                      -- imported | duplicate | no_attachment | error
    detail        TEXT,
    processed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

_db_path: Path = config.DB_PATH


def set_db_path(path: str | Path) -> None:
    """Point the app at a different database file (used by tests)."""
    global _db_path
    _db_path = Path(path)


def get_db_path() -> Path:
    return _db_path


def connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Connection that commits on success and rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        _migrate(conn)
    finally:
        conn.close()
    with session() as conn:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        reseeded = _seed(conn)
        if reseeded:
            _reapply_seed_rules(conn)


def _reapply_seed_rules(conn: sqlite3.Connection) -> None:
    """After the keyword rules change, re-run them on what they categorized before. No Claude calls."""
    from tracker.categorizer import categorize  # categorizer imports this module

    ids = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE category_source = 'seed'")]
    if ids:
        categorize(conn, ids, use_llm=False, use_local=True)


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_txns = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()
    if not has_txns:
        return
    if version < 2:
        _migrate_v2(conn)
    # v2 -> v3: sub-categories and needs/wants/savings groups are plain new columns.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
    for col, ddl in (("parent_id", "INTEGER REFERENCES categories(id) ON DELETE SET NULL"), ("grp", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE categories ADD COLUMN {col} {ddl}")
    # v3 -> v4: credit card statements keep their bill box.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(statements)")}
    for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'bank'"), ("statement_date", "TEXT"), ("due_date", "TEXT"),
                     ("total_due_paise", "INTEGER"), ("min_due_paise", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE statements ADD COLUMN {col} {ddl}")
    conn.commit()


def _migrate_v2(conn: sqlite3.Connection) -> None:
    # v1 -> v2: widen the category_source CHECK. SQLite can't alter a CHECK, so rebuild the table
    # (create new, copy, drop old, rename), as the SQLite docs prescribe.
    ddl = re.search(r"CREATE TABLE IF NOT EXISTS transactions \(.*?\n\);", SCHEMA, re.DOTALL).group(0)
    ddl = ddl.replace("CREATE TABLE IF NOT EXISTS transactions (", "CREATE TABLE transactions_v2 (")
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    try:
        conn.execute(ddl)
        conn.execute("INSERT INTO transactions_v2 SELECT * FROM transactions")
        conn.execute("DROP TABLE transactions")
        conn.execute("ALTER TABLE transactions_v2 RENAME TO transactions")
        conn.execute("PRAGMA user_version = 2")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = ""


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone() if _has_settings(conn) else None
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _has_settings(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_settings'").fetchone())


def _seed(conn: sqlite3.Connection) -> bool:
    """Insert default categories and seed rules once; never overwrite user edits.

    Returns True when the built-in keyword rules were replaced on an existing database.
    """
    has_categories = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    if not has_categories:
        conn.executemany(
            "INSERT INTO categories (name, kind, color, description, is_system) VALUES (?, ?, ?, ?, ?)",
            [(n, k, c, d, 1 if n == config.UNCATEGORIZED else 0) for n, k, c, d in config.DEFAULT_CATEGORIES],
        )
    # Sub-categories (added once; you can rename, hide or delete them afterwards).
    if get_setting(conn, "seeded:subcategories") != "1":
        ids = {r["name"]: r for r in conn.execute("SELECT id, name, kind, color FROM categories")}
        for parent, subs in config.SUBCATEGORIES.items():
            p = ids.get(parent)
            if not p:
                continue
            for name, desc in subs:
                if name not in ids:
                    conn.execute(
                        "INSERT INTO categories (name, kind, color, description, parent_id) VALUES (?, ?, ?, ?, ?)",
                        (name, p["kind"], p["color"], desc, p["id"]),
                    )
        set_setting(conn, "seeded:subcategories", "1")

    # Needs / wants / savings groups for anything that has none yet.
    for grp, names in config.CATEGORY_GROUPS.items():
        conn.executemany("UPDATE categories SET grp = ? WHERE name = ? AND grp IS NULL AND parent_id IS NULL", [(grp, n) for n in names])
    conn.execute(
        "UPDATE categories SET grp = (SELECT p.grp FROM categories p WHERE p.id = categories.parent_id) "
        "WHERE grp IS NULL AND parent_id IS NOT NULL"
    )
    conn.execute(
        "UPDATE categories SET grp = CASE kind WHEN 'income' THEN 'income' WHEN 'transfer' THEN 'transfer' ELSE 'wants' END "
        "WHERE grp IS NULL"
    )

    # Built-in keyword rules: replace older versions (see SEED_RULES_VERSION).
    previous = get_setting(conn, "seeded:rules")
    if previous != SEED_RULES_VERSION:
        conn.execute("DELETE FROM merchant_rules WHERE source = 'seed'")
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
        rows = []
        for category, direction, keywords in config.SEED_RULES:
            if category not in ids:
                continue
            for kw in keywords:
                rows.append(("contains", kw.upper(), direction or "any", ids[category], "seed"))
        conn.executemany(
            "INSERT OR IGNORE INTO merchant_rules (match_type, pattern, direction, category_id, source) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        set_setting(conn, "seeded:rules", SEED_RULES_VERSION)
        return previous is not None
    return False


def category_tree(conn: sqlite3.Connection) -> dict[int, dict]:
    """id -> {name, kind, grp, parent_id, top_id, top_name}: every category with its top-level parent."""
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT id, name, kind, grp, parent_id, color FROM categories")}
    for r in rows.values():
        top = rows.get(r["parent_id"]) if r["parent_id"] else None
        r["top_id"] = top["id"] if top else r["id"]
        r["top_name"] = top["name"] if top else r["name"]
        r["grp"] = r["grp"] or (top["grp"] if top else None)
    return rows


def category_id_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM categories WHERE lower(name) = lower(?)", (name.strip(),)).fetchone()
    return row["id"] if row else None


def uncategorized_id(conn: sqlite3.Connection) -> int:
    cid = category_id_by_name(conn, config.UNCATEGORIZED)
    if cid is None:
        cur = conn.execute(
            "INSERT INTO categories (name, kind, color, is_system) VALUES (?, 'expense', '#ced4da', 1)",
            (config.UNCATEGORIZED,),
        )
        cid = cur.lastrowid
    return cid
