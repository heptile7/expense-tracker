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
class ParsedStatement:
    rows: list[RawTxn] = field(default_factory=list)
    bank: str | None = None  # HDFC | CUB | None
    account_last4: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    layout: str = ""  # which reader produced it, for diagnostics

    def finalize(self) -> "ParsedStatement":
        if self.rows:
            dates = [r.txn_date for r in self.rows]
            self.period_start, self.period_end = min(dates), max(dates)
        return self


_DATE_FORMATS = (
    "%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y", "%d/%b/%Y", "%d/%b/%y", "%d %B %Y", "%d-%B-%Y",
    "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y",
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
