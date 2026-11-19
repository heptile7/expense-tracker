"""Shared types and cell parsers for statement files."""

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation


class StatementError(Exception):
    """The file could not be read as a bank statement."""


class PasswordRequired(StatementError):
    """The PDF is encrypted and no (or a wrong) password was given."""


@dataclass
class RawTxn:
    txn_date: date
    narration: str
    amount: Decimal  # always positive
    direction: str  # debit | credit
    balance: Decimal | None = None
    ref_no: str | None = None


@dataclass
class CardSummary:
    """The bill box of a credit card statement. Any field may be missing."""

    statement_date: date | None = None
    due_date: date | None = None
    total_due: Decimal | None = None
    min_due: Decimal | None = None


BANK, CARD = "bank", "credit_card"


@dataclass
class ParsedStatement:
    rows: list[RawTxn] = field(default_factory=list)
    bank: str | None = None  # HDFC | CUB | None
    account_last4: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    layout: str = ""  # which reader produced it, for diagnostics
    kind: str = BANK  # bank | credit_card
    card: CardSummary | None = None

    def finalize(self) -> "ParsedStatement":
        if self.rows:
            dates = [r.txn_date for r in self.rows]
            self.period_start, self.period_end = min(dates), max(dates)
        return self


_DATE_FORMATS = (
    "%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y", "%d/%b/%Y", "%d/%b/%y", "%d %B %Y", "%d-%B-%Y",
    "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%d %b, %Y", "%d %B, %Y", "%B %d, %Y",
)
_EXCEL_EPOCH = datetime(1899, 12, 30)


def parse_date(cell) -> date | None:
    if cell is None:
        return None
    if isinstance(cell, datetime):
        return cell.date()
    if isinstance(cell, date):
        return cell
    if hasattr(cell, "to_pydatetime"):  # pandas Timestamp
        try:
            return cell.to_pydatetime().date()
        except (ValueError, OverflowError):
            return None
    if isinstance(cell, int | float):
        if isinstance(cell, float) and math.isnan(cell):
            return None
        if 20000 < cell < 80000:  # Excel serial date
            return (_EXCEL_EPOCH + timedelta(days=float(cell))).date()
        return None
    s = str(cell).strip()
    if not s or len(s) > 30:
        return None
    candidates = [s, s.split()[0]] if " " in s else [s]
    if "|" in s:  # card statements: "18/09/2026| 13:45"
        candidates.append(s.split("|")[0].strip())
    if "T" in s and s[:4].isdigit():
        candidates.append(s.split("T")[0])
    for cand in candidates:
        for fmt in _DATE_FORMATS:
            try:
                d = datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
            if 1990 <= d.year <= 2100:
                return d
    return None


_AMOUNT_JUNK = re.compile(r"[,₹\s]|INR|RS\.?", re.IGNORECASE)


def parse_amount(cell) -> Decimal | None:
    """Parse '1,234.50', '1234.5 Cr', '(250.00)', 250.0 -> Decimal (signed). Empty -> None."""
    if cell is None:
        return None
    if isinstance(cell, int | float):
        if isinstance(cell, float) and math.isnan(cell):
            return None
        return Decimal(str(cell))
    s = _AMOUNT_JUNK.sub("", str(cell)).upper()
    if not s or s in {"-", "--", "NA", "NIL"}:
        return None
    sign = 1
    if s.endswith("DR"):
        sign, s = -1, s[:-2]
    elif s.endswith("CR"):
        s = s[:-2]
    if s.startswith("(") and s.endswith(")"):
        sign, s = -1, s[1:-1]
    if s.startswith("-"):
        sign, s = -sign, s[1:]
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value * sign


def cell_text(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and math.isnan(cell):
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


_ACCOUNT_RE = re.compile(
    r"(?:A/?C|ACCOUNT|ACCT)\s*(?:NO|NUMBER|NUM|#)?\.?\s*[:\-]?\s*([0-9Xx*]{6,20})", re.IGNORECASE
)
_BANK_MARKERS = (
    ("CUB", ("CITY UNION BANK", "CIUB0")),
    ("HDFC", ("HDFC BANK", "HDFC0")),
)


def detect_bank(text: str) -> str | None:
    upper = text.upper()
    for bank, markers in _BANK_MARKERS:
        if any(m in upper for m in markers):
            return bank
    return None


def detect_account_last4(text: str) -> str | None:
    m = _ACCOUNT_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return digits[-4:] if len(digits) >= 4 else None


# ── Credit card statements ────────────────────────────────────────────────────

_CARD_MARKERS = (
    "PAYMENT DUE DATE", "MINIMUM AMOUNT DUE", "MINIMUM DUE", "MIN. AMOUNT DUE", "TOTAL DUES", "TOTAL AMOUNT DUE",
    "CREDIT LIMIT", "AVAILABLE CREDIT", "CASH LIMIT", "REWARD POINT",
)
# "Card No: 4386 XXXX XXXX 1234", "Card Number 438628XXXXXX1234", or any masked card number on its own.
_CARD_NO_RE = re.compile(r"CARD\s*(?:NO|NUMBER|NUM|#)?\.?\s*[:\-]?\s*((?:[0-9X*•]{2,6}[\s-]?){2,4}\d{4})(?!\d)", re.IGNORECASE)
_MASKED_CARD_RE = re.compile(r"(?<![0-9A-Z])(?:[0-9X*•]{4}[\s-]?){3}(\d{4})(?!\d)", re.IGNORECASE)


def detect_card_statement(text: str) -> bool:
    """A credit card statement, going by its bill box (due date, minimum due, credit limit)."""
    upper = text.upper()
    hits = sum(m in upper for m in _CARD_MARKERS)
    return hits >= 2 or (hits >= 1 and "CREDIT CARD" in upper)


def detect_card_last4(text: str) -> str | None:
    for m in _CARD_NO_RE.finditer(text):
        raw = m.group(1)
        if sum(ch in "Xx*•" for ch in raw) >= 4 or len(re.sub(r"\D", "", raw)) >= 15:
            return re.sub(r"\D", "", raw)[-4:]
    for m in _MASKED_CARD_RE.finditer(text):
        if sum(ch in "Xx*•" for ch in m.group(0)) >= 4:
            return m.group(1)
    return None


_CARD_AMOUNT_JUNK = re.compile(r"[,\s]|INR|RS\.?|₹|`", re.IGNORECASE)


def card_amount(cell) -> tuple[Decimal, str] | None:
    """A card statement amount -> (amount, direction).

    Purchases are plain amounts (debit). Payments and refunds are credits, marked by a
    trailing "Cr"/"C", a leading "+" or "-", or brackets. "C" in front of the number is
    how some PDFs render the rupee sign, not a credit marker.
    """
    if cell is None:
        return None
    if isinstance(cell, int | float):
        if isinstance(cell, float) and math.isnan(cell):
            return None
        value = Decimal(str(cell))
        return (abs(value), "credit" if value < 0 else "debit") if value else None
    s = _CARD_AMOUNT_JUNK.sub("", str(cell)).upper()
    if not s or s in {"-", "--", "NA", "NIL"}:
        return None
    direction = "debit"
    if s[0] in "+-":
        direction, s = "credit", s[1:]
    if s.startswith(("CR", "DR")):
        direction, s = ("credit" if s.startswith("CR") else "debit"), s[2:]
    if s.startswith("C") and s[1:2].isdigit():
        s = s[1:]  # rupee sign glyph
    if s.startswith("(") and s.endswith(")"):
        direction, s = "credit", s[1:-1]
    if s.endswith("CR") or s.endswith("C"):
        direction, s = "credit", s[: -2 if s.endswith("CR") else -1]
    elif s.endswith("DR") or s.endswith("D"):
        direction, s = "debit", s[: -2 if s.endswith("DR") else -1]
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return (abs(value), direction) if value else None
