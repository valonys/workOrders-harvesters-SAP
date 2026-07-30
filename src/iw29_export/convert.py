"""Turn whatever SAP wrote into a clean, typed .xlsx and tidy in-memory rows.

Letting SAP produce text and building the workbook here avoids the two classic
problems with SAP's own Excel export: it needs Excel installed and visible, and
it hands back strings where you wanted dates and numbers.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from . import xlsx
from .errors import ExportError
from .logging_setup import get_logger

log = get_logger("convert")

_ENCODING_CANDIDATES = ("utf-8-sig", "utf-16", "cp1252", "latin-1")
_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y%m%d")
_SEPARATOR_LINE = re.compile(r"^[\s|+\-=_]*$")
_NUMBER_LIKE = re.compile(r"^-?[\d.,\s]+-?$")
_SEPARATORS = ("\t", "|", ";")
_PREAMBLE_LIMIT = 30


@dataclass
class Table:
    headers: List[str]
    rows: List[List[Any]]

    @property
    def row_count(self) -> int:
        return len(self.rows)


def read_sap_text(path: Path) -> Table:
    """Read a SAP list export, whether it is tab delimited or pipe framed."""
    text, encoding = _read_text(path)
    log.info("Read %s as %s (%d characters)", path.name, encoding, len(text))

    lines = [line for line in text.splitlines() if not _SEPARATOR_LINE.match(line)]
    if not lines:
        raise ExportError(f"{path.name} contains no usable rows.")

    separator, header_index = _find_header(lines, path.name)
    if header_index:
        log.info(
            "Skipped %d preamble line(s) before the header, e.g. %r",
            header_index,
            lines[0][:60].strip(),
        )

    raw_rows = [_split(line, separator) for line in lines[header_index:]]
    header_cells = raw_rows[0]
    drop_first = _has_empty_lead_column(raw_rows)
    if drop_first:
        raw_rows = [row[1:] for row in raw_rows]
        header_cells = raw_rows[0]

    headers = _clean_headers(header_cells)
    width = len(headers)
    header_signature = [cell.strip() for cell in header_cells]

    rows: List[List[Any]] = []
    for raw in raw_rows[1:]:
        cells = [str(cell).strip() for cell in raw[:width]]
        cells += [""] * (width - len(cells))
        if not any(cells):
            continue
        # Long lists repeat the page title and header at each page break.
        if cells == header_signature:
            continue
        rows.append([coerce(cell) for cell in cells])

    if not rows:
        raise ExportError(f"{path.name} has a header but no data rows.")
    log.info("Parsed %d data rows across %d columns", len(rows), width)
    return Table(headers=headers, rows=rows)


def _find_header(lines: Sequence[str], name: str) -> Tuple[str, int]:
    """Locate the header row and its separator.

    SAP puts a page title and blank lines above the data, so the separator
    cannot be read off the first line. The header is the first line that is
    actually split into several columns.
    """
    window = lines[:_PREAMBLE_LIMIT]
    totals = {sep: sum(line.count(sep) for line in window) for sep in _SEPARATORS}
    separator = max(_SEPARATORS, key=lambda candidate: totals[candidate])
    if totals[separator]:
        for index, line in enumerate(window):
            if separator in line:
                return separator, index
    raise ExportError(
        f"Could not work out the column separator in {name}. Expected tab, pipe or "
        f"semicolon in the first {_PREAMBLE_LIMIT} lines. If SAP wrote a fixed-width "
        "list, re-run so the 'Text with Tabs' format is chosen."
    )


def _split(line: str, separator: str) -> List[str]:
    if separator == "|":
        return [cell.strip() for cell in line.strip().strip("|").split("|")]
    return next(csv.reader([line], delimiter=separator))


def _has_empty_lead_column(raw_rows: Sequence[Sequence[str]]) -> bool:
    """SAP's tab export starts every line with a tab, giving a blank first column."""
    sample = raw_rows[: min(len(raw_rows), 50)]
    return len(sample) > 1 and all(
        row and not str(row[0]).strip() for row in sample
    )


def read_xlsx(path: Path) -> Table:
    """Read back a workbook SAP produced itself, so the dataset step still works."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ExportError(
            "Reading a workbook SAP wrote needs openpyxl (pip install openpyxl). "
            "Switch export.mode to 'text_then_convert' to avoid the dependency."
        ) from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        iterator = sheet.iter_rows(values_only=True)
        header_row = next(iterator, None)
        if header_row is None:
            raise ExportError(f"{path.name} has no rows.")
        headers = _clean_headers([str(c) if c is not None else "" for c in header_row])
        width = len(headers)
        rows = []
        for raw in iterator:
            cells = list(raw[:width]) + [None] * max(0, width - len(raw))
            if all(cell in (None, "") for cell in cells):
                continue
            rows.append(
                [coerce(cell) if isinstance(cell, str) else cell for cell in cells]
            )
    finally:
        workbook.close()

    if not rows:
        raise ExportError(f"{path.name} has a header but no data rows.")
    return Table(headers=headers, rows=rows)


def write_xlsx(table: Table, destination: Path, sheet_name: str = "Sheet1") -> Path:
    """Write a styled single-sheet workbook using only the standard library."""
    xlsx.write(
        headers=table.headers,
        rows=table.rows,
        destination=destination,
        sheet_name=sheet_name,
        column_widths=_column_widths(table),
    )
    log.info(
        "Workbook written: %s (%d rows, %d columns)",
        destination,
        table.row_count,
        len(table.headers),
    )
    return destination


def coerce(value: str) -> Any:
    """Best-effort conversion of a SAP cell into a date, number or trimmed string."""
    text = (value or "").strip()
    if not text or text in {"-", "--"}:
        return None

    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.year > 1900:
            return parsed.date()

    # Leading zeros are meaningful in SAP keys, so those stay text.
    if _NUMBER_LIKE.match(text) and not (len(text) > 1 and text.startswith("0")):
        number = _parse_number(text)
        if number is not None:
            return number
    return text


def _parse_number(text: str) -> Optional[Any]:
    cleaned = text.replace("\u00a0", "").replace(" ", "")
    negative = cleaned.endswith("-")
    cleaned = cleaned.rstrip("-")
    if not cleaned or not any(ch.isdigit() for ch in cleaned):
        return None

    # SAP formats numbers per user settings: 1.234,56 or 1,234.56.
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        decimals = cleaned.rsplit(",", 1)[1]
        cleaned = cleaned.replace(",", "." if len(decimals) != 3 else "")
    elif cleaned.count(".") == 1 and len(cleaned.rsplit(".", 1)[1]) == 3:
        cleaned = cleaned.replace(".", "")

    try:
        number: Any = float(cleaned)
    except ValueError:
        return None
    if negative:
        number = -number
    if number.is_integer() and "." not in cleaned:
        return int(number)
    return number


def _read_text(path: Path) -> Tuple[str, str]:
    data = path.read_bytes()
    if not data:
        raise ExportError(f"{path} is empty.")
    for encoding in _ENCODING_CANDIDATES:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ExportError(f"Could not decode {path} with any of {_ENCODING_CANDIDATES}.")


def _clean_headers(raw: Sequence[str]) -> List[str]:
    headers: List[str] = []
    seen: dict = {}
    for index, cell in enumerate(raw, start=1):
        name = re.sub(r"\s+", " ", str(cell).strip()) or f"Column {index}"
        count = seen.get(name.lower(), 0) + 1
        seen[name.lower()] = count
        headers.append(name if count == 1 else f"{name} ({count})")
    return headers


def _column_widths(table: Table, minimum: int = 10, maximum: int = 55) -> List[int]:
    widths = []
    for index, header in enumerate(table.headers):
        longest = len(str(header))
        for row in table.rows[:500]:
            cell = row[index] if index < len(row) else None
            longest = max(longest, len(_display_length(cell)))
        widths.append(max(minimum, min(maximum, longest + 2)))
    return widths


def _display_length(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return "0000-00-00"
    return str(value)
