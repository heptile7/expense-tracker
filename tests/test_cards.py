"""Credit card statements: reading them, the bill box, bill-payment matching and the dashboard numbers."""

import logging
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from conftest import FakeClassifier
from helpers import card_csv, hdfc_xlsx

from tracker import db, insights, service
from tracker.categorizer import categorize
from tracker.fetch import netbanking
from tracker.importer import KindMismatch, import_file
from tracker.parsers.base import card_amount, detect_card_last4, detect_card_statement
from tracker.parsers.card_text import card_summary, parse_card_text

# Made-up statements in the two HDFC layouts (older "Cr" suffix; newer "C" rupee glyph, reward points, "+" credits).
OLD_LAYOUT = """HDFC Bank Credit Cards
Credit Card Statement
Card No: 4386 XXXX XXXX 5521
Statement Date Payment Due Date Total Dues Minimum Amount Due
18/11/2026 08/12/2026 12,345.00 620.00
Credit Limit Available Credit Limit Available Cash Limit
2,00,000.00 1,87,655.00 80,000.00
Domestic Transactions
Date Transaction Description Amount (in Rs.)
20/10/2026 SWIGGY BANGALORE 345.00
25/10/2026 NETBANKING TRANSFER (Ref# 00000012345678) 9,876.00 Cr
02/11/2026 PYU*SWIGGY FOOD BANGALORE 120.50
02/11/2026 PYU*SWIGGY FOOD BANGALORE 120.50
05/11/2026 AMAZON REFUND MUMBAI 499.00 Cr
International Transactions
10/11/2026 NETFLIX.COM LOS GATOS USD 9.99 799.00
"""

NEW_LAYOUT = """Credit Card Statement
HDFC BANK
Card Number 438628XXXXXX5521
Statement Date 18 Nov, 2026
Payment Due Date 08 Dec, 2026
TOTAL AMOUNT DUE C12,345.00 MINIMUM DUE C620.00
DATE & TIME TRANSACTION DESCRIPTION REWARDS AMOUNT PI
20/10/2026| 13:45 SWIGGY BANGALORE + 6 C 345.00 TU
25/10/2026| 09:02 BPPY CC PAYMENT DP016298 (Ref# ST000000123) + C 9,876.00 TU
05/11/2026| 18:20 AMAZON REFUND MUMBAI - 4 + C 499.00 TU
"""

CARD_ROWS = [
    ("20/10/2026", "SWIGGY BANGALORE", "345.00"),
    ("25/10/2026", "NETBANKING TRANSFER (Ref# 00000012345678)", "9,876.00 Cr"),
    ("02/11/2026", "PYU*SWIGGY FOOD BANGALORE", "120.50"),
    ("02/11/2026", "PYU*SWIGGY FOOD BANGALORE", "120.50"),
    ("05/11/2026", "AMAZON REFUND MUMBAI", "499.00 Cr"),
    ("06/11/2026", "FINANCE CHARGES", "210.00"),
]


@pytest.mark.parametrize("text, expected", [
    ("345.00", (Decimal("345.00"), "debit")),
    ("9,876.00 Cr", (Decimal("9876.00"), "credit")),
    ("C 1,234.00", (Decimal("1234.00"), "debit")),  # rupee sign rendered as "C"
    ("+ C 9,876.00", (Decimal("9876.00"), "credit")),
    ("5,000.00 C", (Decimal("5000.00"), "credit")),  # SBI style
    ("1,234.00 D", (Decimal("1234.00"), "debit")),
    ("-250.00", (Decimal("250.00"), "credit")),
    ("₹ 99.00", (Decimal("99.00"), "debit")),
    ("", None),
])
def test_card_amount_markers(text, expected):
    assert card_amount(text) == expected


def test_card_detection_and_number():
    assert detect_card_statement(OLD_LAYOUT) and detect_card_statement(NEW_LAYOUT)
    assert not detect_card_statement("HDFC BANK Statement of account\nOpening Balance 1,000.00\nCREDIT CARD PAYMENT 500.00")
    assert detect_card_last4(OLD_LAYOUT) == detect_card_last4(NEW_LAYOUT) == "5521"


@pytest.mark.parametrize("text", [OLD_LAYOUT, NEW_LAYOUT], ids=["old", "new"])
def test_card_text_layouts(text):
    parsed = parse_card_text(text)
    assert (parsed.kind, parsed.bank, parsed.account_last4) == ("credit_card", "HDFC", "5521")
    rows = {(r.txn_date.isoformat(), r.narration, r.amount, r.direction) for r in parsed.rows}
    assert ("2026-10-20", "SWIGGY BANGALORE", Decimal("345.00"), "debit") in rows  # reward points stripped
    assert any(r.direction == "credit" and r.amount == Decimal("9876.00") for r in parsed.rows)
    assert ("2026-11-05", "AMAZON REFUND MUMBAI", Decimal("499.00"), "credit") in rows
    bill = parsed.card
    assert (bill.statement_date, bill.due_date) == (date(2026, 11, 18), date(2026, 12, 8))
    assert (bill.total_due, bill.min_due) == (Decimal("12345.00"), Decimal("620.00"))


def test_card_text_keeps_identical_rows_and_skips_the_bill_box():
    parsed = parse_card_text(OLD_LAYOUT)
    assert sum(r.narration.startswith("PYU*SWIGGY") for r in parsed.rows) == 2
    assert len(parsed.rows) == 6  # the "18/11/2026 08/12/2026 12,345.00 620.00" line is not a transaction
    assert parsed.rows[-1].amount == Decimal("799.00")  # INR amount, not the USD one


def test_card_table_with_rewards_column_and_a_second_table():
    """PDF tables in the newer layout, followed by an international table with an extra column."""
    from tracker.parsers.table_sniff import parse_grid

    grid = [
        ["HDFC BANK Credit Card", "", "", "", ""],
        ["Card No 4386 XXXX XXXX 5521", "", "", "", ""],
        ["DATE & TIME", "TRANSACTION DESCRIPTION", "REWARDS", "AMOUNT", "PI"],
        ["20/10/2026| 13:45", "SWIGGY BANGALORE", "+ 6", "C 345.00", "TU"],
        ["25/10/2026| 09:02", "BPPY CC PAYMENT", "", "+ C 9,876.00", "TU"],
        ["Date", "Transaction Description", "Foreign Currency", "Amount (in Rs.)", ""],
        ["10/11/2026", "NETFLIX.COM LOS GATOS", "USD 9.99", "799.00", ""],
    ]
    parsed = parse_grid(grid, "pdf-table", kind="credit_card")
    assert [(r.narration, r.amount, r.direction) for r in parsed.rows] == [
        ("SWIGGY BANGALORE", Decimal("345.00"), "debit"),
        ("BPPY CC PAYMENT", Decimal("9876.00"), "credit"),
        ("NETFLIX.COM LOS GATOS", Decimal("799.00"), "debit"),
    ]
    assert parsed.account_last4 == "5521"


def test_bill_box_on_one_line():
    s = card_summary("Statement Date: 18/11/2026  Payment Due Date: 08/12/2026\nTotal Amount Due: Rs. 4,500.00 Minimum Amount Due: Rs. 225.00")
    assert (s.statement_date, s.due_date, s.total_due, s.min_due) == (date(2026, 11, 18), date(2026, 12, 8), Decimal("4500.00"), Decimal("225.00"))


def _bank_rows():
    return [
        ("01/10/26", "NEFT CR-SBIN0001234-ACME TECHNOLOGIES PVT LTD-TEST USER-SBINN52026100112345", "SBINN52026100112345", None, 90000.00, 190000.00),
        ("21/10/26", "UPI-ARUN PRAKASH-9876543210@YBL-SBIN0001234-612345678901-UPI", "0000612345678901", None, 345.00, 190345.00),
        ("26/10/26", "IB BILLPAY DR-HDFC4U-XXXXXXXXXX5521", "0000000000000000", 9876.00, None, 180469.00),
        ("06/11/26", "UPI-ARUN PRAKASH-9876543210@YBL-SBIN0001234-612345678902-UPI", "0000612345678902", 499.00, None, 179970.00),
    ]


def test_card_import_creates_account_and_reads_directions(tmp_path, fake_llm):
    service.create_account(name="HDFC Savings", bank="HDFC", last4="1234", holder_name="Test User")
    r = import_file(card_csv(tmp_path / "card.csv", CARD_ROWS), classifier=fake_llm)
    assert (r.kind, r.account_created, r.n_new) == ("credit_card", True, 6)  # both identical ₹120.50 rows kept
    assert r.card == {"statement_date": "2026-11-18", "due_date": "2026-12-08", "total_due": 12345.0, "min_due": 620.0}
    acct = next(a for a in service.list_accounts() if a["id"] == r.account_id)
    assert (acct["type"], acct["last4"], acct["name"]) == ("credit_card", "5521", "HDFC Credit Card")
    with db.session() as conn:
        rows = {(t["narration_raw"], t["direction"], t["channel"], t["category"], t["category_source"]) for t in conn.execute(
            "SELECT t.*, c.name AS category FROM transactions t JOIN categories c ON c.id = t.category_id WHERE account_id = ?",
            (r.account_id,))}
    assert ("SWIGGY BANGALORE", "debit", "CARD", "Food Delivery", "seed") in rows
    assert ("NETBANKING TRANSFER (Ref# 00000012345678)", "credit", "CARD", "Credit Card Payment", "transfer") in rows
    assert ("AMAZON REFUND MUMBAI", "credit", "CARD", "Refunds & Cashback", "seed") in rows
    assert ("FINANCE CHARGES", "debit", "CARD", "Bank Charges", "seed") in rows
    assert not any("NETBANKING" in it.payee for call in fake_llm.calls for it in call["items"])  # bill payments never go to Claude

    # The next month's statement overlaps by a few days: nothing is counted twice.
    again = import_file(card_csv(tmp_path / "card2.csv", CARD_ROWS[2:] + [("10/11/2026", "UBER INDIA", "250.00")],
                                 bill=("18/12/2026", "07/01/2027", "250.00", "100.00")), classifier=fake_llm)
    assert (again.account_id, again.account_created, again.n_new, again.n_duplicates) == (r.account_id, False, 1, 4)


def test_card_payment_pairs_with_the_bank_bill_payment(tmp_path, fake_llm):
    service.create_account(name="HDFC Savings", bank="HDFC", last4="1234", holder_name="Test User")
    import_file(hdfc_xlsx(tmp_path / "bank.xlsx", _bank_rows(), account_no="50100123451234"), classifier=fake_llm)
    import_file(card_csv(tmp_path / "card.csv", CARD_ROWS), classifier=fake_llm)
    with db.session() as conn:
        t = {r["narration_raw"]: dict(r) for r in conn.execute(
            "SELECT t.*, c.name AS category FROM transactions t JOIN categories c ON c.id = t.category_id")}
    bank, card = t["IB BILLPAY DR-HDFC4U-XXXXXXXXXX5521"], t["NETBANKING TRANSFER (Ref# 00000012345678)"]
    assert (bank["category"], bank["linked_txn_id"]) == ("Credit Card Payment", card["id"])
    assert (card["category"], card["linked_txn_id"]) == ("Credit Card Payment", bank["id"])
    # Same amounts a day apart, but a card purchase and a card refund are never transfers.
    assert t["SWIGGY BANGALORE"]["linked_txn_id"] is None and t["SWIGGY BANGALORE"]["category"] == "Food Delivery"
    assert t["AMAZON REFUND MUMBAI"]["linked_txn_id"] is None
    assert t["UPI-ARUN PRAKASH-9876543210@YBL-SBIN0001234-612345678902-UPI"]["category_source"] != "transfer"


def test_statement_kind_must_match_the_account(tmp_path, fake_llm):
    savings = service.create_account(name="HDFC Savings", bank="HDFC", last4="1234")
    card = service.create_account(name="My Card", bank="HDFC", type="credit_card")
    with pytest.raises(KindMismatch):
        import_file(card_csv(tmp_path / "card.csv", CARD_ROWS), account_id=savings["id"], classifier=fake_llm)
    with pytest.raises(KindMismatch):
        import_file(hdfc_xlsx(tmp_path / "bank.xlsx", _bank_rows()), account_id=card["id"], classifier=fake_llm)
    r = import_file(card_csv(tmp_path / "card.csv", CARD_ROWS), account_id=card["id"], classifier=fake_llm)
    assert (r.account_id, r.account_created) == (card["id"], False)
    assert next(a for a in service.list_accounts() if a["id"] == card["id"])["last4"] == "5521"  # learned from the statement


def test_card_bills_and_unitemised_bill_payments(tmp_path):
    fake_llm = FakeClassifier({"BILLPAY": ("Credit Card Payment", 0.9)})  # as Claude answers for HDFC's bill payments
    service.create_account(name="HDFC Savings", bank="HDFC", last4="1234", holder_name="Test User")
    import_file(hdfc_xlsx(tmp_path / "bank.xlsx", _bank_rows()), classifier=fake_llm)
    oct_ = insights.overview("month", "2026-10")
    assert oct_["card_bills_not_itemised"] == {"amount": 9876.0, "count": 1}  # no card statement yet
    assert any("credit card bills" in f["text"] for f in oct_["facts"])
    assert oct_["cards"] == []

    r = import_file(card_csv(tmp_path / "card.csv", CARD_ROWS), classifier=fake_llm)
    oct_ = insights.overview("month", "2026-10")
    assert oct_["card_bills_not_itemised"]["amount"] == 0
    assert oct_["card_spent"] == 345.0
    nov = insights.overview("month", "2026-11")
    assert nov["card_spent"] == 120.50 * 2 + 210.00  # the refund is income-side, not negative card spending

    with db.session() as conn:
        due = insights.card_bills(conn, today=date(2026, 11, 25))
        assert [(c["status"], c["total_due"], c["days_left"], c["paid"]) for c in due] == [("due", 12345.0, 13, 0)]
        late = insights.card_bills(conn, today=date(2026, 12, 10))[0]
        assert late["status"] == "overdue" and late["left_to_pay"] == 12345.0
        # A bill payment from the bank after the statement date, mentioning the card's last 4 digits.
        conn.execute(
            """INSERT INTO transactions (account_id, txn_date, narration_raw, merchant_key, amount_paise, direction, fingerprint,
               category_id, category_source) VALUES (1, '2026-12-01', 'IB BILLPAY DR-HDFC4U-XXXXXXXXXX5521', 'D:BILLPAY', 1234500,
               'debit', 'fp-pay', ?, 'llm')""",
            (db.category_id_by_name(conn, "Credit Card Payment"),),
        )
        paid = insights.card_bills(conn, today=date(2026, 12, 10))[0]
        assert (paid["status"], paid["paid"], paid["account_id"]) == ("paid", 12345.0, r.account_id)


def test_paid_via_cred_is_a_purchase_but_cred_bill_pay_is_not(fake_llm):
    acct = service.create_account(name="HDFC Savings", bank="HDFC", last4="1234")
    with db.session() as conn:
        ids = []
        for i, (narr, key) in enumerate([
            ("UPI-SWIGGY-UPISWIGGY@ICICI-ICIC0DC0099-612345678901-PAID VIA CRED", "D:SWIGGY"),
            ("UPI-CRED CLUB-CRED.CLUB@AXISB-UTIB0000000-612345678902-PAYMENT ON CRED", "D:CRED CLUB"),
        ]):
            ids.append(conn.execute(
                """INSERT INTO transactions (account_id, txn_date, narration_raw, merchant_key, amount_paise, direction, fingerprint)
                   VALUES (?, '2026-10-01', ?, ?, 10000, 'debit', ?)""", (acct["id"], narr, key, f"fp{i}")).lastrowid)
        categorize(conn, ids, classifier=fake_llm)
        cats = [r[0] for r in conn.execute(
            f"SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id IN ({ids[0]}, {ids[1]}) ORDER BY t.id")]
    assert cats == ["Food Delivery", "Credit Card Payment"]


def test_upgrade_reruns_changed_keyword_rules(tmp_path):
    """A v3 database: statements get the bill-box columns, and rows the old "CRED" rule caught are re-categorized."""
    path = tmp_path / "v3.db"
    db.set_db_path(path)
    db.init_db()
    with db.session() as conn:
        conn.execute("INSERT INTO accounts (id, name, bank) VALUES (1, 'HDFC', 'HDFC')")
        card_pay = db.category_id_by_name(conn, "Credit Card Payment")
        conn.execute(
            """INSERT INTO transactions (account_id, txn_date, narration_raw, merchant_key, amount_paise, direction, fingerprint,
               category_id, category_source) VALUES (1, '2026-10-01', 'UPI-ZOMATO-ZOMATO@HDFCBANK-HDFC0MERUPI-612345678901-PAID VIA CRED',
               'D:ZOMATO', 45000, 'debit', 'fp1', ?, 'seed')""", (card_pay,))
        db.set_setting(conn, "seeded:rules", "2")
    raw = sqlite3.connect(path)
    for col in ("kind", "statement_date", "due_date", "total_due_paise", "min_due_paise"):
        raw.execute(f"ALTER TABLE statements DROP COLUMN {col}")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    db.init_db()
    with db.session() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert {"kind", "due_date", "total_due_paise"} <= {r[1] for r in conn.execute("PRAGMA table_info(statements)")}
        row = conn.execute("SELECT c.name, t.category_source FROM transactions t JOIN categories c ON c.id = t.category_id").fetchone()
        assert tuple(row) == ("Food Delivery", "seed")
        assert db.get_setting(conn, "seeded:rules") == db.SEED_RULES_VERSION


def test_card_accounts_record_their_own_net_banking_steps():
    netbanking.save_route(netbanking.route_profile("HDFC"), [{"action": "click", "candidates": ["#stmt"], "label": "Statement", "frame": ""}])
    assert netbanking.get_route(netbanking.route_profile("HDFC", card=True)) is None
    info = next(b for b in netbanking.banks_info() if b["bank"] == "HDFC")
    assert info["route"] and info["card_route"] is None


def test_card_import_logs_counts_not_content(tmp_path, fake_llm, caplog):
    with caplog.at_level(logging.INFO):
        import_file(card_csv(tmp_path / "card.csv", CARD_ROWS), classifier=fake_llm)
    assert "credit_card" in caplog.text and "Created credit card account" in caplog.text
    for secret in ("SWIGGY", "NETBANKING", "5521", "9,876", "9876", "12345", "345.00"):
        assert secret not in caplog.text
