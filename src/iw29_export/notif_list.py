"""Load a notification-number list from txt, csv or Excel."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import List

from .errors import ConfigError, ExportError
from .logging_setup import get_logger

log = get_logger("notif_list")

_NOTIF_RE = re.compile(r"^\d{6,12}$")


def load_notification_numbers(
    path: Path,
    column: str = "",
    sheet: str = "",
) -> List[str]:
    """Return unique notification numbers in file order.

    - ``.txt``: one number per line (comments starting with # ignored)
    - ``.csv``: column named ``column`` (default first column / Notification)
    - ``.xlsx``: same, via Excel COM
    """
    if not path.exists():
        raise ConfigError(f"Notification list not found: {path}")

    suffix = path.suffix.lower()
    if suffix in {".txt", ".list"}:
        numbers = _from_text(path)
    elif suffix == ".csv":
        numbers = _from_csv(path, column)
    elif suffix in {".xlsx", ".xlsm"}:
        numbers = _from_xlsx(path, column, sheet)
    else:
        raise ConfigError(
            f"Unsupported list type '{suffix}'. Use .txt, .csv or .xlsx."
        )

    cleaned = _dedupe(_normalise(numbers))
    if not cleaned:
        raise ExportError(f"No notification numbers found in {path.name}.")
    log.info("Loaded %d notification number(s) from %s", len(cleaned), path.name)
    return cleaned


def _from_text(path: Path) -> List[str]:
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # allow "13073653, note" or tab-separated
        rows.append(re.split(r"[\s,;]+", text, maxsplit=1)[0])
    return rows


def _from_csv(path: Path, column: str) -> List[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise ExportError(f"{path.name} has no header row.")
        key = _pick_column(list(reader.fieldnames), column)
        return [str(row.get(key) or "").strip() for row in reader]


def _from_xlsx(path: Path, column: str, sheet: str) -> List[str]:
    try:
        import win32com.client.dynamic as dyn
    except ImportError as exc:
        raise ExportError(
            "Reading an Excel notification list needs Excel and pywin32."
        ) from exc

    xl = dyn.Dispatch("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False
    wb = None
    try:
        wb = xl.Workbooks.Open(str(path), UpdateLinks=0, ReadOnly=True)
        ws = wb.Worksheets(sheet) if sheet else wb.Worksheets(1)
        used = ws.UsedRange
        if used is None:
            return []
        rows = int(used.Rows.Count)
        cols = int(used.Columns.Count)
        headers = [str(ws.Cells(1, c).Value or "").strip() for c in range(1, cols + 1)]
        key = _pick_column(headers, column)
        index = headers.index(key) + 1
        return [
            str(ws.Cells(r, index).Value or "").strip()
            for r in range(2, rows + 1)
        ]
    finally:
        if wb is not None:
            try:
                wb.Close(False)
            except Exception:
                pass
        try:
            xl.Quit()
        except Exception:
            pass


def _pick_column(headers: List[str], wanted: str) -> str:
    if wanted:
        for header in headers:
            if header.lower() == wanted.lower():
                return header
        raise ConfigError(
            f"Column {wanted!r} not in list headers: {', '.join(headers)}"
        )
    for candidate in ("Notification", "QMNUM", "Message", "Notif", "notification"):
        for header in headers:
            if header.lower() == candidate.lower():
                return header
    return headers[0]


def _normalise(values: List[str]) -> List[str]:
    found = []
    for raw in values:
        text = str(raw).strip()
        if not text or text.lower() in {"notification", "qmnum", "message"}:
            continue
        # Excel may hand back 13073653.0
        if re.fullmatch(r"\d+\.0+", text):
            text = text.split(".", 1)[0]
        digits = text if _NOTIF_RE.match(text) else re.sub(r"\D", "", text)
        if digits:
            found.append(digits)
    return found


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered
