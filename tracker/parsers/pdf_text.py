"""Fallback for PDF statements whose tables have no ruling lines.

Reads text lines that start with a date and end with "<amount> <balance>". Debit vs
credit is inferred from how the running balance moves, which works for any bank.
"""

import re
from decimal import Decimal

from tracker.parsers.base import ParsedStatement, RawTxn, detect_account_last4, detect_bank, parse_amount, parse_date

_LINE_RE = re.compile(
    r"^(?P<date>\d{1,2}[/\-.](?:\d{1,2}|[A-Za-z]{3})[/\-.]\d{2,4})\s+(?P<rest>.*?)\s+"
    r"(?P<amount>-?[\d,]+\.\d{2})(?:\s*(?:CR|DR|Cr|Dr))?\s+(?P<balance>-?[\d,]+\.\d{2})(?:\s*(?P<bal_flag>CR|DR|Cr|Dr))?\s*$"
)
_TRAILING_REF_DATE = re.compile(r"\s+(?P<ref>[A-Z0-9]{10,22})\s+\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$")
_TRAILING_DATE = re.compile(r"\s+\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$")
_OPENING_RE = re.compile(r"OPENING\s+BALANCE\s*:?\s*(?:INR|RS\.?|₹)?\s*(-?[\d,]+\.\d{2})", re.IGNORECASE)
_FOOTER_HINTS = ("PAGE ", "STATEMENT OF ACCOUNT", "OPENING BALANCE", "CLOSING BALANCE", "GENERATED ON", "REGISTERED OFFICE", "*")
_CREDIT_HINTS = (" CR-", "/CR/", "NEFT CR", "RTGS CR", "DEPOSIT", "SALARY", "INT.PD", "REFUND", "REVERSAL")


def parse_text(text: str) -> ParsedStatement | None:
    out = ParsedStatement(bank=detect_bank(text), account_last4=detect_account_last4(text), layout="pdf-text")
    m = _OPENING_RE.search(text)
    prev_balance: Decimal | None = parse_amount(m.group(1)) if m else None
    last_was_txn = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        txn_date = parse_date(match.group("date")) if match else None
        if not match or txn_date is None:
            upper = line.upper()
            if last_was_txn and out.rows and not upper.startswith(_FOOTER_HINTS) and not re.search(r"\d+\.\d{2}\s*$", line):
                out.rows[-1].narration = f"{out.rows[-1].narration} {line}"
            else:
                last_was_txn = False
            continue

        rest = match.group("rest")
        ref = None
        ref_match = _TRAILING_REF_DATE.search(rest)
        if ref_match:
            ref = ref_match.group("ref")
            rest = rest[: ref_match.start()]
        else:
            rest = _TRAILING_DATE.sub("", rest)

        amount = abs(parse_amount(match.group("amount")) or Decimal(0))
        balance = parse_amount(match.group("balance"))
        if balance is not None and (match.group("bal_flag") or "").upper() == "DR":
            balance = -balance
        if amount == 0:
            continue

        if prev_balance is not None and balance is not None:
            direction = "credit" if balance > prev_balance else "debit"
        else:
            upper = rest.upper()
            direction = "credit" if any(h in upper for h in _CREDIT_HINTS) else "debit"
        prev_balance = balance
        if ref and set(ref) <= {"0"}:
            ref = None
        out.rows.append(RawTxn(txn_date, rest.strip(), amount, direction, balance, ref))
        last_was_txn = True

    return out.finalize() if out.rows else None
