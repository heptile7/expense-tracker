"""Schema migration from v1 (before offline guesses) keeps existing data."""

import sqlite3

from tracker import db


def test_v1_database_is_migrated(tmp_path):
    path = tmp_path / "v1.db"
    v1_schema = db.SCHEMA.replace("'llm', 'local', 'transfer'", "'llm', 'transfer'").split("CREATE TABLE IF NOT EXISTS app_settings")[0]
    conn = sqlite3.connect(path)
    conn.executescript(v1_schema)
    conn.execute("INSERT INTO accounts (id, name, bank) VALUES (1, 'HDFC', 'HDFC')")
    conn.execute("INSERT INTO categories (id, name, kind) VALUES (1, 'Food & Dining', 'expense')")
    conn.execute(
        """INSERT INTO transactions (account_id, txn_date, narration_raw, merchant_key, amount_paise, direction, fingerprint,
           category_id, category_source) VALUES (1, '2026-04-01', 'UPI-SWIGGY', 'D:SWIGGY', 45000, 'debit', 'fp1', 1, 'seed')"""
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    db.set_db_path(path)
    db.init_db()
    with db.session() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        row = conn.execute("SELECT merchant_key, amount_paise FROM transactions").fetchone()
        assert (row["merchant_key"], row["amount_paise"]) == ("D:SWIGGY", 45000)
        conn.execute("UPDATE transactions SET category_source = 'local'")  # allowed after migration
        db.set_setting(conn, "categorizer", "local")
        assert db.get_setting(conn, "categorizer") == "local"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
