"""Copy the harvested IW29 A:N block into the master dashboard Open_NINC sheet.

Columns O onwards on Open_NINC are formulas (FPSO, backlog, fluid codes, naming
checks). This step only replaces values in A2:N so those formulas keep working,
then fills them down to match the new row count.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .errors import ExportError
from .logging_setup import get_logger

log = get_logger("master_sync")

# Excel column letter for the last harvested field (N = 14).
_LAST_HARVEST_COL = 14
_LAST_HARVEST_LETTER = "N"


@dataclass
class SyncResult:
    source: Path
    master: Path
    rows_copied: int
    formulas_filled_to: int


def sync(
    config: Config,
    source_workbook: Optional[Path] = None,
) -> SyncResult:
    """Paste harvest A2:N into Open_NINC A2:N and extend helper formulas."""
    master_cfg = config.master_dashboard
    if not master_cfg.enabled:
        raise ExportError("master_dashboard.enabled is false; nothing to sync.")

    master = master_cfg.path
    if not master or not master.exists():
        raise ExportError(
            f"Master dashboard not found: {master}. Set master_dashboard.path."
        )

    source = source_workbook or _latest_export(config)
    if source is None or not source.exists():
        raise ExportError(
            f"No harvest workbook to sync from under {config.export.folder}."
        )

    return _copy_via_excel(
        source=source,
        master=master,
        source_sheet=master_cfg.source_sheet,
        dest_sheet=master_cfg.dest_sheet,
        formula_last_col=master_cfg.formula_last_col,
    )


def _latest_export(config: Config) -> Optional[Path]:
    folder = config.export.folder
    if not folder.exists():
        return None
    candidates = sorted(
        (
            path
            for path in folder.glob("IW29_*.xlsx")
            if path.is_file() and not path.name.startswith("~$")
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _copy_via_excel(
    source: Path,
    master: Path,
    source_sheet: str,
    dest_sheet: str,
    formula_last_col: int,
) -> SyncResult:
    try:
        import win32com.client.dynamic as dyn
    except ImportError as exc:
        raise ExportError(
            "Master dashboard sync needs Excel and pywin32 on this machine."
        ) from exc

    xl = dyn.Dispatch("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False
    xl.AskToUpdateLinks = False
    xl.ScreenUpdating = False

    src_wb = None
    dst_wb = None
    try:
        src_wb = xl.Workbooks.Open(str(source), UpdateLinks=0, ReadOnly=True)
        dst_wb = xl.Workbooks.Open(str(master), UpdateLinks=0, ReadOnly=False)

        src_ws = _sheet(src_wb, source_sheet)
        dst_ws = _sheet(dst_wb, dest_sheet)

        src_last = _last_data_row(src_ws, col=1)
        if src_last < 2:
            raise ExportError(f"{source.name}!{source_sheet} has no data rows under A2.")

        rows = src_last - 1
        dst_old_last = max(_last_data_row(dst_ws, col=1), 1)

        # Direct Value assignment avoids clipboard / multi-selection errors that
        # Copy+PasteSpecial hits on filtered sheets and Excel Tables.
        clear_to = max(dst_old_last, src_last)
        if clear_to >= 2:
            dst_ws.Range(f"A2:{_LAST_HARVEST_LETTER}{clear_to}").ClearContents()

        src_range = src_ws.Range(f"A2:{_LAST_HARVEST_LETTER}{src_last}")
        dst_ws.Range(f"A2:{_LAST_HARVEST_LETTER}{src_last}").Value = src_range.Value

        filled_to = _fill_formulas(dst_ws, src_last, formula_last_col, dst_old_last)

        dst_wb.Save()
        log.info(
            "Synced %d rows from %s!%s A2:%s into %s!%s",
            rows,
            source.name,
            source_sheet,
            _LAST_HARVEST_LETTER,
            master.name,
            dest_sheet,
        )
        return SyncResult(
            source=source,
            master=master,
            rows_copied=rows,
            formulas_filled_to=filled_to,
        )
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"Master dashboard sync failed: {exc}") from exc
    finally:
        try:
            xl.CutCopyMode = False
        except Exception:
            pass
        if src_wb is not None:
            try:
                src_wb.Close(False)
            except Exception:
                pass
        if dst_wb is not None:
            try:
                dst_wb.Close(True)
            except Exception:
                pass
        try:
            xl.ScreenUpdating = True
            xl.Quit()
        except Exception:
            pass


def _sheet(workbook: Any, name: str) -> Any:
    try:
        return workbook.Worksheets(name)
    except Exception as exc:
        known = [_safe_name(ws) for ws in workbook.Worksheets]
        raise ExportError(
            f"Workbook has no sheet named {name!r}. Available: {', '.join(known)}"
        ) from exc


def _safe_name(ws: Any) -> str:
    try:
        return str(ws.Name)
    except Exception:
        return "?"


def _last_data_row(ws: Any, col: int = 1) -> int:
    """Bottom-most non-empty cell in a column, ignoring the header-only case."""
    try:
        cell = ws.Cells(ws.Rows.Count, col).End(-4162)  # xlUp
        row = int(cell.Row)
        return row if cell.Value not in (None, "") else 1
    except Exception:
        used = ws.UsedRange
        return int(used.Row + used.Rows.Count - 1) if used else 1


def _col_letter(index: int) -> str:
    letters = []
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _fill_formulas(
    ws: Any, data_last_row: int, formula_last_col: int, old_last_row: int
) -> int:
    """Copy O2:{last}2 formulas down, and clear helpers below a shrunk list."""
    if formula_last_col <= _LAST_HARVEST_COL or data_last_row < 2:
        return data_last_row

    first_formula_col = _LAST_HARVEST_COL + 1
    first_letter = _col_letter(first_formula_col)
    last_letter = _col_letter(formula_last_col)
    template = ws.Range(f"{first_letter}2:{last_letter}2")

    formulas = []
    for cell in template:
        try:
            formula = str(cell.Formula or "")
        except Exception:
            formula = ""
        formulas.append(formula if formula.startswith("=") else "")

    if not any(formulas):
        log.warning(
            "No formulas found in %s2:%s2; left helper columns untouched.",
            first_letter,
            last_letter,
        )
        return data_last_row

    # Write FormulaR1C1-style by setting each column's Formula from the template
    # cell, then AutoFill. AutoFill respects relative references better than a
    # bulk Formula paste on some corporate Excel builds.
    if data_last_row > 2:
        try:
            template.AutoFill(
                Destination=ws.Range(f"{first_letter}2:{last_letter}{data_last_row}")
            )
        except Exception:
            # Fallback: assign formula text row by row for columns that have one.
            for offset, formula in enumerate(formulas):
                if not formula:
                    continue
                col = first_formula_col + offset
                letter = _col_letter(col)
                ws.Range(f"{letter}2").Formula = formula
                ws.Range(f"{letter}2").AutoFill(
                    Destination=ws.Range(f"{letter}2:{letter}{data_last_row}")
                )

    if old_last_row > data_last_row:
        ws.Range(
            f"{first_letter}{data_last_row + 1}:{last_letter}{old_last_row}"
        ).ClearContents()

    return data_last_row
