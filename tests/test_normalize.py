import pytest

from tracker.normalize import extract_ref, mask_for_llm, normalize


@pytest.mark.parametrize(
    ("narration", "direction", "channel", "key"),
    [
        # HDFC dash-delimited
        ("UPI-SWIGGY-SWIGGY8.PAYU@HDFCBANK-HDFC0MERUPI-412345678901-PAYMENT FROM PHONE", "debit", "UPI", "D:SWIGGY"),
        ("UPI-RAMESH KUMAR-9876543210@YBL-SBIN0001234-412345678902-RENT", "debit", "UPI", "D:RAMESH KUMAR"),
        ("NEFT CR-SBIN0001234-ACME TECHNOLOGIES PVT LTD-TEST USER-SBINN52024041512345", "credit", "NEFT", "C:ACME TECHNOLOGIES"),
        ("IMPS-412345678901-RAMESH KUMAR-SBIN-XXXXXXXX1234-RENT", "debit", "IMPS", "D:RAMESH KUMAR"),
        ("POS 416021XXXXXX1234 SWIGGY BANGALORE POS DEBIT", "debit", "CARD", "D:SWIGGY"),
        ("ME DC SI 416021XXXXXX1234 NETFLIX", "debit", "CARD", "D:NETFLIX"),
        ("ATW-416021XXXXXX1234-S1ANCH12-CHENNAI", "debit", "ATM", "D:ATM CASH"),
        ("NWD-416021XXXXXX1234-SBI ATM-COIMBATORE", "debit", "ATM", "D:ATM CASH"),
        ("ACH D- BAJAJ FINANCE LTD-1234567", "debit", "ACH", "D:BAJAJ FINANCE"),
        ("FT - CR - 50100123456789 - JOHN DOE", "credit", "TRANSFER", "C:JOHN DOE"),
        ("BIL/ONL/000123456789/AIRTEL/AIRTEL PREPAID", "debit", "OTHER", "D:AIRTEL"),
        # HDFC PDF statements (2026 layout), numbers made up
        ("POS 416021XXXXXX1234 612345678901 05APR6 1 12:34:56 6123456789 WWW AMAZON IN", "debit", "CARD", "D:AMAZON"),
        ("POS 416021XXXXXX1234 612345678901 05MAY6 1 12:34:56 BANGALORE SWIGGY LIMITED", "debit", "CARD", "D:SWIGGY"),
        ("POS 416021XXXXXX1234 612345678901 05JUN6 1 12:34:56 BANGALORE INSTAMART HDFC BAN", "debit", "CARD", "D:INSTAMART"),
        ("CRV POS 416021******1234 WWW AMAZON", "credit", "CARD", "C:AMAZON"),
        ("ACH D- INDIAN CLEARING CORP-1234SZDZKOP5", "debit", "ACH", "D:INDIAN CLEARING CORP"),
        ("NEFT CR-BOFA0MM6012-ACME SOLUTIONS PVT LTD-TEST USER HDFC BANK LIMITED", "credit", "NEFT", "C:ACME SOLUTIONS"),
        ("DC 4160211234561234 AUTOPAY SI-TAD", "debit", "OTHER", "D:TAD"),
        # Slash-delimited (CUB and most other banks)
        ("UPI/DR/412345678901/SWIGGY/YESB/swiggy@yesbank/Payme", "debit", "UPI", "D:SWIGGY"),
        ("UPI/CR/412345678901/AMAZON PAY INDIA PRIVA/UTIB/amazonpay@axis/refund", "credit", "UPI", "C:AMAZON PAY INDIA"),
        ("IMPS/P2A/412345678901/RAMESH KUMAR/SBIN/XXXX1234", "debit", "IMPS", "D:RAMESH KUMAR"),
    ],
)
def test_merchant_keys(narration, direction, channel, key):
    n = normalize(narration, direction)
    assert n.channel == channel
    assert n.merchant_key == key


def test_same_merchant_across_vpas_and_channels_groups_together():
    keys = {
        normalize("UPI-SWIGGY-SWIGGY8.PAYU@HDFCBANK-HDFC0MERUPI-412345678901-X", "debit").merchant_key,
        normalize("UPI/DR/412345678999/SWIGGY/YESB/swiggy@yesbank/Pay", "debit").merchant_key,
        normalize("POS 416021XXXXXX1234 SWIGGY BANGALORE POS DEBIT", "debit").merchant_key,
    }
    assert keys == {"D:SWIGGY"}


def test_refund_is_kept_apart_from_purchase():
    assert normalize("UPI/CR/1/SWIGGY/YESB/swiggy@yesbank", "credit").merchant_key == "C:SWIGGY"


def test_gateway_name_falls_back_to_vpa():
    n = normalize("UPI-PAYU-PAYUSWIGGY@HDFCBANK-HDFC0000001-412345678901-UPI", "debit")
    assert n.merchant_key == "D:PAYUSWIGGY@HDFCBANK"
    assert n.vpa == "payuswiggy@hdfcbank"


def test_qr_merchant_without_name_uses_vpa():
    n = normalize("TO UPI/412345678901/PAYTMQR2810050501/paytmqr28100505010@paytm", "debit")
    assert n.merchant_key == "D:PAYTMQR28100505010@PAYTM"


def test_llm_hint_has_no_digits():
    hint = mask_for_llm("UPI-RAMESH KUMAR-9876543210@YBL-SBIN0001234-412345678902-RENT 50100123451234")
    assert not any(ch.isdigit() for ch in hint)
    assert "RAMESH KUMAR" in hint


def test_extract_ref():
    assert extract_ref("UPI-SWIGGY-X@Y-HDFC0MERUPI-412345678901-PAY") == "412345678901"
    assert extract_ref("NEFT CR-SBIN0001234-ACME-SBINN52024041512345") == "SBINN52024041512345"
    assert extract_ref("CASH DEPOSIT") is None
