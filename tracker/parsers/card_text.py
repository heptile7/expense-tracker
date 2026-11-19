"""Credit card statements: the text-only PDF reader and the bill box (due date, amount due).

Card statements differ from bank statements: there is no running balance, purchases are
plain amounts, and payments and refunds are marked as credits ("Cr", a trailing "C", or a
leading "+"). Some issuers print the rupee sign as a "C" glyph in front of the amount,
add a time after the date ("18/09/2026| 13:45"), and print reward points in a column
just before the amount; all three are handled here.
"""

import re
from datetime import date
from decimal import Decimal

from tracker.parsers.base import (
    CARD,
    CardSummary,
    ParsedStatement,
    RawTxn,
    card_amount,
    detect_bank,
    detect_card_last4,
    parse_date,
)

_DATE = r"\d{1,2}[/\-.](?:\d{1,2}|[A-Za-z]{3})[/\-.]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}"
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?"
_AMOUNT = r"(?:[+\-]\s*)?(?:(?:CR|DR)\s+)?(?:C|₹|`|RS\.?|INR)?\s*\(?\d[\d,]*\.\d{2}\)?"
_LINE_RE = re.compile(
    rf"^(?P<date>{_DATE})(?:\s*\|?\s*{_TIME})?\s*\|?\s+(?P<desc>.+?)\s+(?P<amount>{_AMOUNT})"
    r"(?:\s*(?P<flag>CR|DR|C|D)\b)?(?:\s+(?P<tail>[A-Z]{1,3}))?\s*$",
    re.IGNORECASE,
)
_REWARDS_RE = re.compile(r"\s+[+\-]?\s*\d{1,6}$")  # reward points printed just before the amount
_NOT_TXN = (
    "OPENING BALANCE", "CLOSING BALANCE", "TOTAL DUE", "TOTAL AMOUNT DUE", "MINIMUM AMOUNT DUE", "MINIMUM DUE",
    "PAYMENT DUE DATE", "STATEMENT DATE", "CREDIT LIMIT", "PREVIOUS BALANCE",
)


def parse_card_text(text: str) -> ParsedStatement | None:
    out = ParsedStatement(bank=detect_bank(text), account_last4=detect_card_last4(text), layout="card-text", kind=CARD)
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        m = _LINE_RE.match(line)
        if not m:
            continue
        txn_date = parse_date(m.group("date"))
        desc = _REWARDS_RE.sub("", m.group("desc")).strip(" |")
        if txn_date is None or sum(ch.isalpha() for ch in desc) < 2 or any(k in desc.upper() for k in _NOT_TXN):
            continue
        found = card_amount(m.group("amount") + (m.group("flag") or ""))
        if not found:
            continue
        amount, direction = found
        out.rows.append(RawTxn(txn_date, desc, amount, direction))
    out.card = card_summary(text)
    return out.finalize() if out.rows else None


# ── The bill box ──────────────────────────────────────────────────────────────

_FIELDS: dict[str, tuple[str, ...]] = {
    "statement_date": ("STATEMENT DATE", "STATEMENT GENERATION DATE", "BILL DATE", "BILLING DATE"),
    "due_date": ("PAYMENT DUE DATE", "PAYMENT DUE BY", "DUE DATE", "PAY BY"),
    "total_due": ("TOTAL AMOUNT DUE", "TOTAL PAYMENT DUE", "TOTAL DUES", "TOTAL DUE", "AMOUNT PAYABLE"),
    "min_due": ("MINIMUM AMOUNT DUE", "MINIMUM PAYMENT DUE", "MIN. AMOUNT DUE", "MIN AMOUNT DUE", "MINIMUM DUE"),
}
_LABELS = sorted(((label, f) for f, labels in _FIELDS.items() for label in labels), key=lambda x: -len(x[0]))
_TOKEN_RE = re.compile(rf"(?P<date>{_DATE})|(?P<amount>(?:C|₹|`|RS\.?|INR)?\s*\d[\d,]*\.\d{{2}}(?:\s*(?:CR|DR)\b)?)", re.IGNORECASE)


def _labels_in(line: str) -> list[tuple[int, int, str]]:
    """(start, end, field) for each bill-box label in the line, longest label first, no overlaps."""
    upper, found, taken = line.upper(), [], []
    for label, field in _LABELS:
        start = 0
        while (i := upper.find(label, start)) >= 0:
            span = (i, i + len(label))
            start = span[1]
            if not any(a < span[1] and span[0] < b for a, b in taken):
                taken.append(span)
                found.append((span[0], span[1], field))
    return sorted(found)


def _value(field: str, kind: str, text: str):
    if field.endswith("_date"):
        return parse_date(text) if kind == "date" else None
    if kind != "amount":
        return None
    found = card_amount(re.sub(r"\s*(?:CR|DR)\s*$", "", text, flags=re.IGNORECASE))
    return found[0] if found else None


def card_summary(text: str) -> CardSummary:
    """Statement date, payment due date, total and minimum due.

    Handles "Label: value" on one line and a row of labels with the values on the next line.
    """
    values: dict[str, date | Decimal] = {}
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    for idx, line in enumerate(lines):
        labels = _labels_in(line)
        if not labels:
            continue
        waiting = []
        for n, (_start, end, field) in enumerate(labels):
            stop = labels[n + 1][0] if n + 1 < len(labels) else len(line)
            token = _TOKEN_RE.search(line[end:stop])
            if token:
                if field not in values and (v := _value(field, token.lastgroup, token.group(0))) is not None:
                    values[field] = v
            else:
                waiting.append(field)
        if waiting and idx + 1 < len(lines):
            tokens = list(_TOKEN_RE.finditer(lines[idx + 1]))
            if len(tokens) >= len(waiting):
                for field, token in zip(waiting, tokens, strict=False):
                    if field not in values and (v := _value(field, token.lastgroup, token.group(0))) is not None:
                        values[field] = v
    return CardSummary(**values)
