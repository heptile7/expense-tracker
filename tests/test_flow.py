"""Where the money went: every rupee in and out, one-offs, and what isn't explained yet."""

from datetime import date

import pytest
from conftest import FakeClassifier
from helpers import card_csv, hdfc_xlsx, signed_in_client

from tracker import flow, service
from tracker.importer import import_file

CLAUDE = FakeClassifier({
    "BILLPAY": ("Credit Card Payment", 0.9),
    "RAMESH KUMAR": ("Rent", 0.9),
    "PRIYA S": ("Payments to People", 0.9),
    "ARUN PRAKASH": ("Payments to People", 0.9),
    "GOLDSMITH": ("Shopping", 0.9),
    "HDFC BANK DIGITAL": ("Bank Charges", 0.8),
})


def _month(y: int, m: int, k: int) -> list[tuple]:
    """One synthetic month on the savings account: salary, rent, food, a SIP, a card bill, a payment to a friend."""
    d = lambda day: date(y, m, day).strftime("%d/%m/%y")  # noqa: E731
    r = lambda n: f"6{m:02d}{n:09d}"  # noqa: E731
    return [
        (d(1), f"NEFT CR-SBIN0001234-ACME TECHNOLOGIES PVT LTD-TEST USER-SBINN5{r(1)}", r(1), None, 100000.00),
        (d(3), f"UPI-RAMESH KUMAR-9876543210@YBL-SBIN0001234-{r(2)}-RENT", r(2), 20000.00, None),
        (d(5), f"UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099-{r(3)}-UPI", r(3), 500.00 + k, None),
        (d(6), f"UPI-MUTUAL FUNDS NCL-GROWW.NCL.BRK@VALIDHDFC-HDFC0MERUPI-{r(4)}-SIP", r(4), 5000.00, None),
        (d(10), "IB BILLPAY DR-HDFC4U-XXXXXXXXXX5521", f"0000{r(5)}", 3000.00, None),
        (d(12), f"UPI-PRIYA S-9123456780@YBL-SBIN0001234-{r(6)}-UPI", r(6), 700.00, None),
    ]


def _with_balance(rows: list[tuple], opening: float = 50000.0) -> list[tuple]:
    bal, out = opening, []
    for dt, narr, ref, wd, dep in rows:
        bal += (dep or 0) - (wd or 0)
        out.append((dt, narr, ref, wd, dep, round(bal, 2)))
    return out


@pytest.fixture
def three_months(tmp_path):
    service.create_account(name="HDFC Savings", bank="HDFC", last4="1234", holder_name="Test User")
    rows = _month(2026, 7, 0) + _month(2026, 8, 10) + _month(2026, 9, 20)
    rows += [
        ("15/09/26", "UPI-SURESH GOLDSMITH-GOLDSMITH@OKAXIS-UTIB0000001-609000000001-UPI", "609000000001", 45000.00, None),
        ("16/09/26", "UPI-RAMESH KUMAR-9876543210@YBL-SBIN0001234-609000000002-RENT PLUS PARKING", "609000000002", 1500.00, None),
        ("17/09/26", "UPI-HDFC BANK DIGITAL TO-HDFCBANKDIGITALTOKEN.1@HDFCBANK-HDFC0MERUPI-609000000003-UPI", "609000000003", 8000.00, None),
        ("18/09/26", "UPI-ARUN PRAKASH-9988776655@YBL-SBIN0001234-609000000004-LOAN", "609000000004", 12000.00, None),
        ("20/09/26", "UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099-609000000005-REFUND", "609000000005", None, 200.00),
    ]
    rows.sort(key=lambda x: (x[0][6:8], x[0][3:5], x[0][0:2]))
    import_file(hdfc_xlsx(tmp_path / "bank.xlsx", _with_balance(rows)), classifier=CLAUDE)


def test_every_rupee_is_accounted_for(three_months):
    w = flow.where_it_went("3m", "2026-09")
    assert w["period"]["months"] == 3
    assert w["money_in"] == 300000 + 200  # salary and the refund
    assert w["balance"]["change"] == w["left"] and w["check"]["matches"]
    rows = {r["name"]: r for r in w["rows"]}
    assert rows["Rent"]["amount"] == 60000 and rows["Rent"]["per_month"] == 20000
    assert rows["Investments"]["amount"] == 15000
    card = rows["Credit card bills (purchases not imported)"]
    assert card["amount"] == 9000 and card["unexplained"]
    assert rows["Payments to People"]["unexplained"]
    assert sum(r["amount"] for r in w["rows"]) == pytest.approx(w["money_out"])
    assert [r["amount"] for r in w["rows"]] == sorted((r["amount"] for r in w["rows"]), reverse=True)
    assert {c["name"] for c in w["came_from"]} >= {"Salary"}


def test_one_offs_are_set_apart_from_regular_payments(three_months):
    w = flow.where_it_went("3m", "2026-09")
    one_offs = {o["payee"]: o for o in w["one_offs"]}
    assert "Suresh Goldsmith" in one_offs and one_offs["Suresh Goldsmith"]["why"] == "a payee you don't pay regularly"
    assert "Arun Prakash" in one_offs  # ₹12,000 to someone paid once
    assert "Ramesh Kumar" not in one_offs  # rent: same payee every month
    assert w["one_off_total"] == 45000 + 12000
    assert w["regular_out"] == pytest.approx(w["money_out"] - w["one_off_total"])


def test_not_explained_and_likely_mixups(three_months):
    u = flow.where_it_went("3m", "2026-09")["unexplained"]
    items = {i["kind"]: i for i in u["items"]}
    assert items["card_bills"]["amount"] == 9000
    people = items["people"]
    assert people["count"] == 3 and people["big_count"] == 0
    mixups = {m["payee"]: m for m in items["mixups"]["items"]}
    assert mixups["Ramesh Kumar"]["category"] == "Parking & Tolls" and "PARKING" in mixups["Ramesh Kumar"]["why"]
    assert mixups["Arun Prakash"]["category"] == "EMI & Loans"  # money lent to a friend, caught by the word LOAN
    assert "bank charge" in mixups["Hdfc Bank Digital To"]["why"]
    assert "Swiggy" not in mixups  # the keyword is in Swiggy's own name: not a mix-up
    assert u["amount"] == pytest.approx(sum(i["amount"] for i in u["items"]))
    assert 0 < u["share"] < 1


def test_card_statement_turns_bills_into_purchases(three_months, tmp_path):
    rows = [("05/09/2026", "AMAZON PAY INDIA PRIVA MUMBAI", "2,500.00"), ("10/09/2026", "NETBANKING TRANSFER (Ref# 000000000001)", "3,000.00 Cr")]
    import_file(card_csv(tmp_path / "card.csv", rows, bill=("18/09/2026", "08/10/2026", "2,500.00", "125.00")), classifier=CLAUDE)
    w = flow.where_it_went("month", "2026-09")
    names = {r["name"]: r for r in w["rows"]}
    assert "Credit card bills (purchases not imported)" not in names  # September's bill is matched on the card
    assert names["Shopping"]["top_children"][0]["name"] == "Online Shopping"
    savings = next(a for a in service.list_accounts() if a["type"] != "credit_card")
    one = flow.where_it_went("month", "2026-09", account_id=savings["id"])
    assert any(r["kind"] == "to_cards" and r["amount"] == 3000 for r in one["rows"])
    assert one["check"]["matches"]


def test_periods_with_partial_data_average_over_months_with_data(three_months):
    w = flow.where_it_went("12m", "2026-09")
    assert w["period"]["months"] == 3
    assert next(r for r in w["rows"] if r["name"] == "Rent")["per_month"] == 20000


def test_where_endpoint_and_transaction_range(three_months):
    import app as app_module

    client = signed_in_client(app_module)
    w = client.get("/api/where", params={"period": "3m", "month": "2026-09"}).json()
    assert w["check"]["matches"]
    rent = next(r for r in w["rows"] if r["name"] == "Rent")
    t = client.get("/api/transactions", params={"start_date": "2026-08-01", "end_date": "2026-09-30",
                                                "category_id": rent["category_id"], "sort": "amount_desc"}).json()
    assert t["total"] == 2 and [i["amount"] for i in t["items"]] == [20000, 20000]  # August and September rent
