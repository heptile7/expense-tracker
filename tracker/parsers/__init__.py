"""Read a statement file (Excel, CSV/TXT, PDF) into normalized rows.

All layouts go through ``table_sniff.parse_grid`` which finds the transaction table by
its header names, so HDFC and CUB exports (and most other banks) need no per-bank code.
Text-only PDFs fall back to ``pdf_text.parse_text`` (bank statements) or
``card_text.parse_card_text`` (credit card statements).

A statement is treated as a credit card statement when its text has a card bill box
(payment due date, minimum due, credit limit), or when the caller says so because the
chosen account is a credit card.
"""

import csv
import io
import logging
from pathlib import Path

from tracker.parsers.base import (
    BANK,
    CARD,
    CardSummary,
    ParsedStatement,
    PasswordRequired,
    RawTxn,
    StatementError,
    cell_text,
    detect_account_last4,
    detect_bank,
    detect_card_last4,
    detect_card_statement,
)
from tracker.parsers.card_text import card_summary, parse_card_text
from tracker.parsers.pdf_text import parse_text
from tracker.parsers.table_sniff import parse_grid

__all__ = [
    "BANK", "CARD", "CardSummary", "ParsedStatement", "PasswordRequired", "RawTxn", "StatementError", "parse_statement",
]

log = logging.getLogger(__name__)

SUPPORTED = {".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".pdf"}
# Max gap (points) between letters of the same word. pdfplumber's default of 3 glues words together
# in HDFC's small-font statements ("ACHD-INDIANCLEARINGCORP"); 1.5 keeps the spaces.
PDF_X_TOLERANCE = 1.5


def parse_statement(path: str | Path, password: str | None = None, kind: str | None = None) -> ParsedStatement:
    """kind: "bank" or "credit_card" to force how amounts are read; None detects it from the file."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise StatementError(f"Unsupported file type {ext}. Use Excel (.xls/.xlsx), CSV or PDF.")
    if ext == ".pdf":
        result = _parse_pdf(path, password, kind)
    elif ext in {".csv", ".txt"}:
        result = _first_match(_delimited_grids(path.read_bytes()), "csv", kind)
    else:
        result = _first_match(_excel_grids(path), "excel", kind)
    if result is None:
        raise StatementError(
            "Couldn't find a transaction table (looked for Date, Narration/Description and Debit/Credit or Amount columns)."
        )
    return result


def _grid_text(grid: list[list]) -> str:
    return "\n".join(" ".join(t for t in (cell_text(c) for c in row) if t) for row in grid)


def _first_match(grids, layout: str, kind: str | None) -> ParsedStatement | None:
    for grid in grids:
        text = _grid_text(grid)
        grid_kind = kind or (CARD if detect_card_statement(text) else None)
        parsed = parse_grid(grid, layout, grid_kind)
        if parsed:
            if parsed.kind == CARD:
                parsed.card = card_summary(text)
                parsed.account_last4 = detect_card_last4(text) or parsed.account_last4
            return parsed
    return None


def _df_to_grid(df) -> list[list]:
    return [[None if (isinstance(v, float) and v != v) else v for v in row] for row in df.itertuples(index=False, name=None)]


def _excel_grids(path: Path):
    import pandas as pd

    data = path.read_bytes()
    try:
        engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
        sheets = pd.read_excel(io.BytesIO(data), header=None, dtype=object, sheet_name=None, engine=engine)
        for df in sheets.values():
            yield _df_to_grid(df)
        return
    except Exception as exc:  # some banks' ".xls" is really HTML or tab-separated text
        log.info("Not a binary Excel file (%s); trying HTML/text", exc)
    head = data[:2048].lower()
    if b"<table" in head or b"<html" in head or b"<table" in data.lower():
        try:
            for df in pd.read_html(io.BytesIO(data), header=None):
                yield _df_to_grid(df.astype(object))
            return
        except Exception as exc:
            log.info("HTML parse failed: %s", exc)
    yield from _delimited_grids(data)


def _delimited_grids(data: bytes):
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiters = [dialect.delimiter]
    except csv.Error:
        delimiters = [",", "\t", "|", ";"]
    for delim in delimiters:
        yield [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


def _parse_pdf(path: Path, password: str | None, kind: str | None) -> ParsedStatement | None:
    import pdfplumber

    try:
        pdf = pdfplumber.open(path, password=password or "")
    except Exception as exc:
        if _is_password_error(exc):
            raise PasswordRequired("This PDF is password-protected." if not password else "Wrong PDF password.") from exc
        raise StatementError(f"Couldn't open the PDF: {exc}") from exc

    with pdf:
        rows: list[list] = []
        texts: list[str] = []
        for page in pdf.pages:
            texts.append(page.extract_text(x_tolerance=PDF_X_TOLERANCE) or "")
            for table in page.extract_tables({"text_x_tolerance": PDF_X_TOLERANCE}):
                rows.extend([[(c or "").replace("\n", " ") for c in r] for r in table])
    full_text = "\n".join(texts)
    kind = kind or (CARD if detect_card_statement(full_text) else BANK)
    parsed = parse_grid(rows, "pdf-table", kind) if rows else None
    if parsed:
        # Preamble (account number, bank) usually sits outside the table in PDFs.
        parsed.bank = parsed.bank or detect_bank(full_text)
        if kind == CARD:
            parsed.account_last4 = detect_card_last4(full_text) or parsed.account_last4
            parsed.card = card_summary(full_text)
        else:
            parsed.account_last4 = parsed.account_last4 or detect_account_last4(full_text)
        return parsed
    return parse_card_text(full_text) if kind == CARD else parse_text(full_text)


def _is_password_error(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        name = type(exc).__name__.lower()
        if "password" in name or "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            return True
        exc = exc.__cause__ or exc.__context__ or (exc.args[0] if exc.args and isinstance(exc.args[0], BaseException) else None)
    return False
