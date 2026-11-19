"""Find the transaction table inside a grid of cells, whatever bank layout it came from.

Bank exports put a preamble (name, address, account number) above the table and a
summary below it. We locate the header row by its column names, map the columns, and
read rows until the data stops. Works for HDFC (Date | Narration | Chq./Ref.No. |
Value Dt | Withdrawal Amt. | Deposit Amt. | Closing Balance), CUB, and most other
Indian bank layouts, including a single Amount column with a Dr/Cr indicator.
"""

import re
from dataclasses import dataclass

from tracker.parsers.base import (
    ParsedStatement,
    RawTxn,
    cell_text,
    detect_account_last4,
    detect_bank,
    parse_amount,
    parse_date,
)


def _norm_header(cell) -> str:
    s = cell_text(cell).upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^A-Z0-9/ ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_value_date(h: str) -> bool:
    return h.startswith("VALUE")


def _is_date(h: str) -> bool:
    return h in {"DATE", "DT", "TXN DT", "TRAN DT"} or h.endswith(" DATE") or h.startswith("DATE") or _is_value_date(h)


def _is_narration(h: str) -> bool:
    return any(k in h for k in ("NARRATION", "DESCRIPTION", "PARTICULAR", "REMARK", "DETAILS"))


def _is_debit(h: str) -> bool:
    return h.startswith(("WITHDRAW", "DEBIT")) or h in {"DR", "DR AMOUNT", "DR AMT", "PAID OUT"}


def _is_credit(h: str) -> bool:
    return h.startswith(("DEPOSIT", "CREDIT")) or h in {"CR", "CR AMOUNT", "CR AMT", "PAID IN"}


def _is_amount(h: str) -> bool:
    return h.startswith("AMOUNT") or h in {"TRANSACTION AMOUNT", "TXN AMOUNT", "AMT", "TXN AMT"}


def _is_drcr(h: str) -> bool:
    return h in {"DR/CR", "CR/DR", "DR CR", "CR DR", "TYPE", "TXN TYPE", "DEBIT/CREDIT", "D/C", "C/D"}


def _is_balance(h: str) -> bool:
    return "BALANCE" in h


def _is_ref(h: str) -> bool:
    return any(k in h for k in ("REF", "CHQ", "CHEQUE", "INSTRUMENT", "UTR"))


@dataclass
class Columns:
    date: int
    narration: int
    debit: int | None = None
    credit: int | None = None
    amount: int | None = None
    drcr: int | None = None
    balance: int | None = None
    ref: int | None = None

    @property
    def amount_cols(self) -> set[int]:
        return {c for c in (self.debit, self.credit, self.amount) if c is not None}


def detect_columns(row: list) -> Columns | None:
    headers = [_norm_header(c) for c in row]
    if sum(bool(h) for h in headers) < 3:
        return None
    date_col = value_col = narr = debit = credit = amount = drcr = balance = ref = None
    for i, h in enumerate(headers):
        if not h:
            continue
        if _is_narration(h) and narr is None:
            narr = i
        elif _is_drcr(h) and drcr is None:
            drcr = i
        elif _is_debit(h) and debit is None:
            debit = i
        elif _is_credit(h) and credit is None:
            credit = i
        elif _is_balance(h) and balance is None:
            balance = i
        elif _is_amount(h) and amount is None:
            amount = i
        elif _is_date(h):
            if _is_value_date(h):
                value_col = value_col if value_col is not None else i
            elif date_col is None:
                date_col = i
        elif _is_ref(h) and ref is None:
            ref = i
    date_col = date_col if date_col is not None else value_col
    if date_col is None or narr is None:
        return None
    if not ((debit is not None and credit is not None) or amount is not None):
        return None
    return Columns(date_col, narr, debit, credit, amount, drcr, balance, ref)


def _get(row: list, idx: int | None):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def parse_grid(grid: list[list], layout: str = "table") -> ParsedStatement | None:
    """Return a ParsedStatement if a transaction table is found in the grid, else None."""
    header_idx, cols = None, None
    for i, row in enumerate(grid[:200]):
        cols = detect_columns(row)
        if cols:
            header_idx = i
            break
    if cols is None or header_idx is None:
        return None

    preamble = " ".join(cell_text(c) for row in grid[:header_idx] for c in row)
    header_norm = [_norm_header(c) for c in grid[header_idx]]
    out = ParsedStatement(bank=detect_bank(preamble), account_last4=detect_account_last4(preamble), layout=layout)

    for row in grid[header_idx + 1 :]:
        if [_norm_header(c) for c in row] == header_norm:
            continue  # header repeated on a new PDF page
        txn_date = parse_date(_get(row, cols.date))
        narration = cell_text(_get(row, cols.narration))
        amounts = [parse_amount(_get(row, c)) for c in cols.amount_cols]
        has_amount = any(a not in (None, 0) for a in amounts)

        if txn_date is None:
            # Narration wrapped onto the next row: append to the previous transaction.
            if narration and not has_amount and out.rows and not cell_text(_get(row, cols.date)):
                out.rows[-1].narration = f"{out.rows[-1].narration} {narration}".strip()
            continue
        if not has_amount:
            continue  # opening-balance rows and separators

        if cols.debit is not None and cols.credit is not None:
            debit = parse_amount(_get(row, cols.debit)) or 0
            credit = parse_amount(_get(row, cols.credit)) or 0
            if debit and not credit:
                amount, direction = abs(debit), "debit"
            elif credit and not debit:
                amount, direction = abs(credit), "credit"
            else:
                net = credit - debit
                amount, direction = abs(net), ("credit" if net > 0 else "debit")
        else:
            raw = parse_amount(_get(row, cols.amount)) or 0
            flag = cell_text(_get(row, cols.drcr)).upper()
            if flag.startswith(("D", "W")):
                direction = "debit"
            elif flag.startswith(("C", "DEP")):
                direction = "credit"
            else:
                direction = "debit" if raw < 0 else "credit"
            amount = abs(raw)
        if amount == 0:
            continue

        ref = cell_text(_get(row, cols.ref)) or None
        if ref and set(ref) <= {"0"}:
            ref = None
        out.rows.append(
            RawTxn(
                txn_date=txn_date,
                narration=narration or "(no narration)",
                amount=amount,
                direction=direction,
                balance=parse_amount(_get(row, cols.balance)),
                ref_no=ref,
            )
        )
    return out.finalize() if out.rows else None
