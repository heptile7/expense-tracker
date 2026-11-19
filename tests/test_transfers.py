from helpers import cub_csv, hdfc_xlsx

from tracker import service
from tracker.importer import import_file


def test_debit_in_one_account_and_credit_in_another_is_a_self_transfer(tmp_path, fake_llm):
    service.create_account(name="HDFC", bank="HDFC", last4="1234")
    service.create_account(name="CUB", bank="CUB", last4="9876")
    import_file(
        hdfc_xlsx(tmp_path / "h.xlsx", [("10/04/24", "IMPS-412345678920-SOMEONE ELSE-CIUB-XXXX9876-SAVINGS", "412345678920", 10000.00, None, 40000.00)]),
        classifier=fake_llm,
    )
    import_file(
        cub_csv(tmp_path / "c.csv", [("11-04-2024", "IMPS/P2A/412345678920/HDFC TRANSFER/HDFC", "10,000.00", "Cr", "15,000.00")]),
        classifier=fake_llm,
    )
    items = service.list_transactions()["items"]
    assert len(items) == 2
    assert {t["category"] for t in items} == {"Self Transfer"}
    assert all(t["linked_txn_id"] for t in items)
    assert service.dashboard("2024-04")["total_expense"] == 0


def test_payment_to_holder_name_is_self_transfer(tmp_path, fake_llm):
    service.create_account(name="HDFC", bank="HDFC", last4="1234", holder_name="Test User")
    import_file(
        hdfc_xlsx(tmp_path / "h.xlsx", [("10/04/24", "UPI-TEST USER-TESTUSER@OKAXIS-UTIB0000001-412345678921-UPI", "412345678921", 5000.00, None, 45000.00)]),
        classifier=fake_llm,
    )
    assert service.list_transactions()["items"][0]["category"] == "Self Transfer"


def test_your_rule_beats_transfer_detection(tmp_path, fake_llm):
    service.create_account(name="HDFC", bank="HDFC", last4="1234", holder_name="Test User")
    import_file(
        hdfc_xlsx(tmp_path / "h.xlsx", [("10/04/24", "UPI-TEST USER-TESTUSER@OKAXIS-UTIB0000001-412345678921-UPI", "412345678921", 5000.00, None, 45000.00)]),
        classifier=fake_llm,
    )
    txn = service.list_transactions()["items"][0]
    service.set_category(txn["id"], "Investments")
    assert service.get_transaction(txn["id"])["category"] == "Investments"
