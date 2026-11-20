"""Synthetic statement builders. No real bank data is used anywhere in the tests."""

from pathlib import Path

from openpyxl import Workbook

OWNER_PIN = "123456"


def signed_in_client(app_module, **kwargs):
    """A TestClient on this computer, signed in as the owner (setting sign-in up on first use)."""
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, client=("127.0.0.1", 50000), **kwargs)
    if client.get("/api/auth/state").json()["setup_needed"]:
        client.post("/api/auth/setup", json={"name": "Test User", "pin": OWNER_PIN}).raise_for_status()
    else:
        owner = client.get("/api/auth/state").json()["people"][0]
        client.post("/api/auth/signin", json={"profile_id": owner["id"], "pin": OWNER_PIN}).raise_for_status()
    return client

HDFC_HEADER = ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]


def hdfc_xlsx(path: Path, rows: list[tuple], account_no: str = "50100123451234") -> Path:
    """rows: (dd/mm/yy, narration, ref, withdrawal or None, deposit or None, balance)."""
    wb = Workbook()
    ws = wb.active
    ws.append(["HDFC BANK Ltd.", None, None, None, None, None, None])
    ws.append(["MR TEST USER", None, None, None, "Account No :", account_no, None])
    ws.append(["Statement of accounts", None, None, None, None, None, None])
    ws.append([None] * 7)
    ws.append(HDFC_HEADER)
    ws.append(["********", "********", "********", "********", "********", "********", "********"])
    for d, narr, ref, wd, dep, bal in rows:
        ws.append([d, narr, ref, d, wd, dep, bal])
    ws.append([None] * 7)
    ws.append(["STATEMENT SUMMARY :-", None, None, None, None, None, None])
    ws.append(["Opening Balance", "Dr Count", "Cr Count", "Debits", "Credits", "Closing Bal", None])
    wb.save(path)
    return path


def cub_csv(path: Path, rows: list[tuple], account_no: str = "500101012349876") -> Path:
    """CUB-style export with a single Amount column and a Dr/Cr flag.

    rows: (dd-mm-yyyy, description, amount, 'Dr'|'Cr', balance)
    """
    lines = [
        "CITY UNION BANK LIMITED,,,,,",
        f"Account Number : {account_no},,,,,",
        ",,,,,",
        "Txn Date,Description,Ref No./Cheque No.,Amount,Dr/Cr,Balance",
    ]
    for d, desc, amt, flag, bal in rows:
        lines.append(f'{d},"{desc}",,"{amt}",{flag},"{bal}"')
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def card_csv(path: Path, rows: list[tuple], card_no: str = "4386 XXXX XXXX 5521",
             bill: tuple = ("18/11/2026", "08/12/2026", "12,345.00", "620.00")) -> Path:
    """HDFC-style credit card statement: a bill box, then Date | Transaction Description | Amount.

    rows: (dd/mm/yyyy, description, amount text). Credits carry "Cr" ("9,876.00 Cr"), purchases are plain.
    bill: (statement date, payment due date, total dues, minimum amount due).
    """
    lines = [
        "HDFC Bank Credit Card Statement,,",
        f"Card No,{card_no},",
        "Statement Date,Payment Due Date,Total Dues,Minimum Amount Due",
        ",".join(f'"{v}"' for v in bill),
        ",,",
        "Date,Transaction Description,Amount (in Rs.)",
    ]
    lines += [f'{d},"{desc}","{amt}"' for d, desc, amt in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


MONTH1 = [
    ("01/04/24", "UPI-SWIGGY-SWIGGY8.PAYU@HDFCBANK-HDFC0MERUPI-412345678901-PAYMENT FROM PHONE", "0000412345678901", 450.00, None, 49550.00),
    ("02/04/24", "UPI-KUMAR STORES-KUMARSTORES@OKSBI-SBIN0001234-412345678902-UPI", "0000412345678902", 820.50, None, 48729.50),
    ("03/04/24", "NEFT CR-SBIN0001234-ACME TECHNOLOGIES PVT LTD-TEST USER-SBINN52024040312345", "SBINN52024040312345", None, 85000.00, 133729.50),
    ("05/04/24", "UPI-RAMESH KUMAR-9876543210@YBL-SBIN0001234-412345678903-RENT", "0000412345678903", 15000.00, None, 118729.50),
    ("06/04/24", "UPI-SRI MURUGAN TRADERS-Q123456789@YBL-YESB0YBLUPI-412345678904-UPI", "0000412345678904", 300.00, None, 118429.50),
    ("07/04/24", "UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099-412345678905-UPI", "0000412345678905", 275.00, None, 118154.50),
    ("08/04/24", "ATW-416021XXXXXX1234-S1ANCH12-CHENNAI", "0000000000000000", 2000.00, None, 116154.50),
]

MONTH2 = [
    ("02/05/24", "UPI-KUMAR STORES-KUMARSTORES@OKSBI-SBIN0001234-412345678910-UPI", "0000412345678910", 640.00, None, 115514.50),
    ("03/05/24", "UPI-GREENLEAF ENTERPRISES-GREENLEAF@OKAXIS-UTIB0000001-412345678911-UPI", "0000412345678911", 380.00, None, 115134.50),
    ("04/05/24", "UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099-412345678912-UPI", "0000412345678912", 199.00, None, 114935.50),
    ("05/05/24", "UPI-RAMESH KUMAR-9876543210@YBL-SBIN0001234-412345678913-RENT", "0000412345678913", 15000.00, None, 99935.50),
]
