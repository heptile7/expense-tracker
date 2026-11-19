"""Read a statement file (Excel, CSV/TXT, PDF) into normalized rows.

All layouts go through ``table_sniff.parse_grid`` which finds the transaction table by
its header names, so HDFC and CUB exports (and most other banks) need no per-bank code.
Text-only PDFs fall back to ``pdf_text.parse_text``.
"""

import csv
import io
import logging
from pathlib import Path

from tracker.parsers.base import (
    ParsedStatement,
    PasswordRequired,
    RawTxn,
    StatementError,
    detect_account_last4,
    detect_bank,
)
from tracker.parsers.pdf_text import parse_text
from tracker.parsers.table_sniff import parse_grid

__all__ = ["ParsedStatement", "PasswordRequired", "RawTxn", "StatementError", "parse_statement"]

log = logging.getLogger(__name__)

SUPPORTED = {".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".pdf"}
# Max gap (points) between letters of the same word. pdfplumber's default of 3 glues words together
# in HDFC's small-font statements ("ACHD-INDIANCLEARINGCORP"); 1.5 keeps the spaces.
PDF_X_TOLERANCE = 1.5


def parse_statement(path: str | Path, password: str | None = None) -> ParsedStatement:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise StatementError(f"Unsupported file type {ext}. Use Excel (.xls/.xlsx), CSV or PDF.")
    if ext == ".pdf":
        result = _parse_pdf(path, password)
    elif ext in {".csv", ".txt"}:
        result = _first_match(_delimited_grids(path.read_bytes()), "csv")
    else:
        result = _first_match(_excel_grids(path), "excel")
    if result is None:
        raise StatementError(
            "Couldn't find a transaction table (looked for Date, Narration/Description and Debit/Credit or Amount columns)."
        )
    return result


def _first_match(grids, layout: str) -> ParsedStatement | None:
    for grid in grids:
        parsed = parse_grid(grid, layout)
        if parsed:
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


def _parse_pdf(path: Path, password: str | None) -> ParsedStatement | None:
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
    parsed = parse_grid(rows, "pdf-table") if rows else None
    if parsed:
        # Preamble (account number, bank) usually sits outside the table in PDFs.
        parsed.bank = parsed.bank or detect_bank(full_text)
        parsed.account_last4 = parsed.account_last4 or detect_account_last4(full_text)
        return parsed
    return parse_text(full_text)


def _is_password_error(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        name = type(exc).__name__.lower()
        if "password" in name or "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            return True
        exc = exc.__cause__ or exc.__context__ or (exc.args[0] if exc.args and isinstance(exc.args[0], BaseException) else None)
    return False
