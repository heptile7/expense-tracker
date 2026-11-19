from decimal import Decimal

import pytest
from helpers import MONTH1, MONTH2, cub_csv, hdfc_xlsx

from tracker import service
from tracker.importer import NeedsAccount, import_file
from tracker.parsers import parse_statement
from tracker.parsers.base import parse_amount, parse_date
from tracker.parsers.pdf_text import parse_text


def test_hdfc_xlsx_layout_is_detected(tmp_path):
    parsed = parse_statement(hdfc_xlsx(tmp_path / "s.xlsx", MONTH1))
    assert parsed.bank == "HDFC"
    assert parsed.account_last4 == "1234"
    assert len(parsed.rows) == len(MONTH1)
    first = parsed.rows[0]
    assert (first.direction, first.amount, first.balance) == ("debit", Decimal("450.0"), Decimal("49550.0"))
    salary = parsed.rows[2]
    assert (salary.direction, salary.amount) == ("credit", Decimal("85000.0"))
    assert parsed.rows[6].ref_no is None  # all-zero ref treated as missing


def test_cub_amount_plus_drcr_layout(tmp_path):
    path = cub_csv(
        tmp_path / "cub.csv",
        [
            ("01-04-2024", "UPI/DR/412345678901/SWIGGY/YESB/swiggy@yesbank/Pay", "1,250.00", "Dr", "20,000.00 Cr"),
            ("02-04-2024", "NEFT/N123456789/ACME TECH/SALARY", "50,000.00", "Cr", "70,000.00 Cr"),
        ],
    )
    parsed = parse_statement(path)
    assert parsed.bank == "CUB"
    assert parsed.account_last4 == "9876"
    assert [(r.direction, r.amount) for r in parsed.rows] == [("debit", Decimal("1250.00")), ("credit", Decimal("50000.00"))]


def test_account_auto_matched_by_last4(tmp_path):
    service.create_account(name="CUB", bank="CUB", last4="9876")
    hdfc = service.create_account(name="HDFC", bank="HDFC", last4="1234")
    result = import_file(hdfc_xlsx(tmp_path / "s.xlsx", MONTH1), use_llm=False)
    assert result.account_id == hdfc["id"]


def test_unknown_account_asks(tmp_path):
    service.create_account(name="A", bank="HDFC", last4="1111")
    service.create_account(name="B", bank="HDFC", last4="2222")
    with pytest.raises(NeedsAccount):
        import_file(hdfc_xlsx(tmp_path / "s.xlsx", MONTH1), use_llm=False)


def test_same_file_twice_is_rejected(tmp_path):
    service.create_account(name="HDFC", bank="HDFC", last4="1234")
    path = hdfc_xlsx(tmp_path / "s.xlsx", MONTH1)
    first = import_file(path, use_llm=False)
    second = import_file(path, use_llm=False)
    assert first.n_new == len(MONTH1)
    assert second.duplicate_file
    assert service.list_transactions()["total"] == len(MONTH1)


def test_overlapping_statements_dedupe_rows(tmp_path):
    service.create_account(name="HDFC", bank="HDFC", last4="1234")
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), use_llm=False)
    overlap = import_file(hdfc_xlsx(tmp_path / "apr_may.xlsx", MONTH1[-2:] + MONTH2), use_llm=False)
    assert overlap.n_new == len(MONTH2)
    assert overlap.n_duplicates == 2
    assert service.list_transactions()["total"] == len(MONTH1) + len(MONTH2)


def test_delete_statement_undoes_import(tmp_path):
    service.create_account(name="HDFC", bank="HDFC", last4="1234")
    result = import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), use_llm=False)
    service.delete_statement(result.statement_id)
    assert service.list_transactions()["total"] == 0


def test_parse_amount_and_date():
    assert parse_amount("1,234.50") == Decimal("1234.50")
    assert parse_amount("250.00 Dr") == Decimal("-250.00")
    assert parse_amount("(99.00)") == Decimal("-99.00")
    assert parse_amount("") is None and parse_amount("-") is None
    assert parse_date("01/04/24").isoformat() == "2024-04-01"
    assert parse_date("01-Apr-2024").isoformat() == "2024-04-01"
    assert parse_date("2024-04-01 10:15:00").isoformat() == "2024-04-01"
    assert parse_date("Opening Balance") is None


def test_pdf_text_fallback_infers_direction_from_balance():
    text = """HDFC BANK Ltd.
Account No : 50100123451234
Opening Balance : 10,000.00
Date Narration Chq./Ref.No. Value Dt Withdrawal Amt. Deposit Amt. Closing Balance
01/04/24 UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099- 0000412345678901 01/04/24 450.00 9,550.00
412345678901-UPI
03/04/24 NEFT CR-SBIN0001234-ACME TECHNOLOGIES SBINN52024040312345 03/04/24 85,000.00 94,550.00
Page 1 of 2
"""
    parsed = parse_text(text)
    assert parsed.bank == "HDFC" and parsed.account_last4 == "1234"
    assert [(r.direction, r.amount) for r in parsed.rows] == [("debit", Decimal("450.00")), ("credit", Decimal("85000.00"))]
    assert parsed.rows[0].narration.endswith("412345678901-UPI")
    assert parsed.rows[0].ref_no == "0000412345678901"
