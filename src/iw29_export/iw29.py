"""Drives the IW29 selection screen and gets the result list out to a file."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .config import Config
from .errors import EmptyResultError, ExportError, SapError
from .logging_setup import get_logger
from .sap import (
    VKEY_ENTER,
    VKEY_EXECUTE,
    SapGui,
    SapSession,
    set_clipboard_text,
)
from .source import Extract, ProgressFn, ReportSource

log = get_logger("iw29")

# ALV grid containers differ between releases, so try the known ones in order.
_GRID_CANDIDATES = (
    "wnd[0]/usr/cntlGRID1/shellcont/shell",
    "wnd[0]/usr/cntlCONTAINER/shellcont/shell",
    "wnd[0]/usr/cntlALV_CONTAINER_1/shellcont/shell",
    "wnd[0]/usr/cntlCUSTOM_CONTROL/shellcont/shell",
    "wnd[0]/usr/cntlCC_ALV/shellcont/shell",
)

# Prefixes SAP uses for select-option input fields.
_FIELD_PREFIXES = ("ctxt", "txt")

_LAYOUT_FIELD_CANDIDATES = (
    "wnd[0]/usr/ctxtVARIANT",
    "wnd[0]/usr/ctxtP_LVARI",
    "wnd[0]/usr/ctxtALV_DEF",
    "wnd[0]/usr/ctxtDY_LAYOUT",
)

_GET_VARIANT_BUTTON = "wnd[0]/tbar[1]/btn[17]"

# The single-values grid of the multiple-selection dialog, and its "Copy" button.
_MULTI_TABLE_ROW = (
    "wnd[1]/usr/tabsTAB_STRIP/tabpSIVA/ssubSCREEN_HEADER:SAPLALDB:3010/"
    "tblSAPLALDBSINGLE/ctxtRSCSEL_255-SLOW_I[1,{row}]"
)
_MULTI_COPY_BUTTON = "wnd[1]/tbar[0]/btn[8]"

# Save-list dialog. btn[11] is "Generate"; btn[0] is the fallback on older kernels.
_SAVE_DIALOG_PATH = "wnd[1]/usr/ctxtDY_PATH"
_SAVE_DIALOG_FILENAME = "wnd[1]/usr/ctxtDY_FILENAME"
_SAVE_DIALOG_ENCODING = "wnd[1]/usr/ctxtDY_FILE_ENCODING"
_SAVE_DIALOG_BUTTONS = ("wnd[1]/tbar[0]/btn[11]", "wnd[1]/tbar[0]/btn[0]")
_UTF8_CODEPAGE = "4110"

_EMPTY_RESULT_HINTS = (
    "no notifications",
    "no objects",
    "no data",
    "list contains no data",
    "selection includes no data",
    "keine meldungen",
)


class SapIw29Source(ReportSource):
    """Runs the transaction end to end and returns the file SAP wrote."""

    name = "sap"

    def __init__(self, config: Config, password: str = ""):
        self.config = config
        self._password = password

    def extract(self, staging_dir: Path, progress: ProgressFn) -> Extract:
        cfg = self.config
        staging_dir.mkdir(parents=True, exist_ok=True)
        gui = SapGui(
            system=cfg.sap.system,
            client=cfg.sap.client,
            user=cfg.sap.user,
            password=self._password,
            language=cfg.sap.language,
            logon_path=cfg.sap.logon_path,
            attach_timeout_s=cfg.sap.attach_timeout_s,
            step_timeout_s=cfg.sap.step_timeout_s,
            close_connection=cfg.sap.close_connection,
            reuse_existing_connection=cfg.sap.reuse_existing_connection,
        )

        progress(f"Connecting to SAP system {cfg.sap.system}...")
        with gui.session() as session:
            progress(f"Opening transaction {cfg.selection.transaction}.")
            session.start_transaction(cfg.selection.transaction)
            session.dismiss_popups()

            self._apply_variant(session, progress)
            self._apply_layout(session)
            self._apply_filters(session, progress)
            self._apply_checkboxes(session)
            self._apply_raw_steps(session)

            progress("Executing the report...")
            started = time.monotonic()
            session.send_vkey(VKEY_EXECUTE)
            session.raise_on_error()
            session.dismiss_popups()
            log.info("Report finished in %.1fs", time.monotonic() - started)

            grid = self._find_grid(session)
            self._guard_empty_result(session, grid)

            reported_rows = _grid_row_count(grid)
            if reported_rows is not None:
                progress(f"SAP returned {reported_rows} rows.")

            target = staging_dir / _staging_filename(cfg)
            if cfg.export.mode == "native_xlsx":
                self._export_native_xlsx(session, grid, target, progress)
                kind = "xlsx"
            else:
                self._export_text(session, grid, target, progress)
                kind = "text"

            _await_file(target, timeout_s=60)
            progress(f"SAP wrote {target.name} ({target.stat().st_size:,} bytes).")
            return Extract(
                path=target, kind=kind, reported_by_sap=reported_rows
            )

    # ---------------------------------------------------------------- selection

    def _apply_variant(self, session: SapSession, progress: ProgressFn) -> None:
        variant = self.config.selection.variant
        if not variant:
            return
        progress(f"Loading selection variant '{variant}'.")
        if not session.exists(_GET_VARIANT_BUTTON):
            raise SapError(
                "The 'Get Variant' toolbar button is not on this screen, so "
                f"selection.variant='{variant}' cannot be applied."
            )
        session.press(_GET_VARIANT_BUTTON)
        for element_id in ("wnd[1]/usr/txtV-LOW", "wnd[1]/usr/txtENAME-LOW"):
            if session.exists(element_id):
                session.set_text(
                    element_id, variant if element_id.endswith("V-LOW") else ""
                )
        session.send_vkey(VKEY_EXECUTE, "wnd[1]")

        # A single hit loads straight away; several hits leave a pick list open.
        if session.exists("wnd[1]"):
            row = session.optional(
                "wnd[1]/usr/lbl[1,2]"
            ) or session.optional("wnd[1]/usr/tblSAPLALDBSINGLE")
            if row is not None:
                try:
                    row.setFocus()
                    session.send_vkey(VKEY_ENTER, "wnd[1]")
                except Exception:
                    session.dismiss_popups()
            else:
                session.dismiss_popups()
        session.raise_on_error()

    def _apply_layout(self, session: SapSession) -> None:
        layout = self.config.selection.layout
        if not layout:
            return
        for element_id in _LAYOUT_FIELD_CANDIDATES:
            if session.exists(element_id):
                session.set_text(element_id, layout)
                log.info("ALV layout '%s' set via %s", layout, element_id)
                return
        log.warning(
            "No layout field found on the selection screen; ignoring "
            "selection.layout='%s'. Add it under [[selection.raw]] if it matters.",
            layout,
        )

    def _apply_filters(self, session: SapSession, progress: ProgressFn) -> None:
        for item in self.config.selection.filters:
            values = [v for v in item.values if v]
            if not values:
                continue
            if len(values) == 1:
                element_id = self._selection_field(session, item, "-LOW")
                session.set_text(element_id, values[0])
                log.info("Filter %s = %s", item.field_name, values[0])
                continue

            element_id = self._selection_field(session, item, "-LOW")
            session.set_text(element_id, values[0])
            self._fill_multiple_selection(session, item.field_name, values, progress)

        for item in self.config.selection.ranges:
            low, high = self._expand(item.low), self._expand(item.high)
            if low:
                session.set_text(self._selection_field(session, item, "-LOW"), low)
            if high:
                session.set_text(self._selection_field(session, item, "-HIGH"), high)
            if low or high:
                log.info("Range %s = %s .. %s", item.field_name, low or "*", high or "*")

    def _expand(self, template: str) -> str:
        """Substitute {date_from}, {date_to} and {today} in a configured value."""
        if not template or "{" not in template:
            return template
        date_from, date_to = self.config.selection.resolved_dates()
        return template.format(
            date_from=date_from,
            date_to=date_to,
            today=datetime.now().strftime("%d.%m.%Y"),
        )

    def _selection_field(self, session: SapSession, item: Any, suffix: str) -> str:
        explicit = getattr(item, "kind", "") or ""
        prefixes: Sequence[str] = (
            (explicit,) + _FIELD_PREFIXES if explicit else _FIELD_PREFIXES
        )
        tried = []
        for prefix in dict.fromkeys(prefixes):
            candidate = f"wnd[0]/usr/{prefix}{item.field_name}{suffix}"
            tried.append(candidate)
            if session.exists(candidate):
                return candidate
        raise SapError(
            f"Field '{item.field_name}{suffix}' is not on the "
            f"{self.config.selection.transaction} selection screen. Tried: "
            + ", ".join(tried)
            + ". Record the screen with Alt+F12 to get the exact id."
        )

    def _fill_multiple_selection(
        self,
        session: SapSession,
        field_name: str,
        values: List[str],
        progress: ProgressFn,
    ) -> None:
        """Load many values through the clipboard rather than typing row by row."""
        button = f"wnd[0]/usr/btn%_{field_name}_%_APP_%-VALU_PUSH"
        if not session.exists(button):
            raise SapError(
                f"{len(values)} values were given for {field_name} but its multiple "
                f"selection button ({button}) is missing. Use a saved variant instead."
            )
        progress(f"Loading {len(values)} values into {field_name}.")
        session.press(button)

        paste_button = "wnd[1]/tbar[0]/btn[24]"
        if session.exists(paste_button):
            set_clipboard_text("\r\n".join(values))
            session.press(paste_button)
        else:
            log.info("No clipboard upload button; typing the values instead.")
            _type_multiple_selection(session, field_name, values)

        session.press(_MULTI_COPY_BUTTON)
        session.raise_on_error()

    def _apply_checkboxes(self, session: SapSession) -> None:
        for name, checked in self.config.selection.checkboxes.items():
            element_id = f"wnd[0]/usr/chk{name}"
            if not session.exists(element_id):
                log.warning("Checkbox %s is not on this screen; skipping.", name)
                continue
            session.set_checked(element_id, checked)
            log.info("Checkbox %s = %s", name, checked)

    def _apply_raw_steps(self, session: SapSession) -> None:
        for step in self.config.selection.raw:
            log.info("Raw step %s on %s", step.action, step.element_id)
            if step.action == "set_text":
                session.set_text(step.element_id, self._expand(step.value))
            elif step.action == "set_checked":
                session.set_checked(
                    step.element_id, str(step.value).strip().lower() in {"", "true", "1", "yes"}
                )
            elif step.action == "press":
                session.press(step.element_id)
            elif step.action == "select":
                session.select(step.element_id)
            elif step.action == "send_vkey":
                session.send_vkey(int(step.value or 0), step.element_id or "wnd[0]")

    # ------------------------------------------------------------------- output

    def _find_grid(self, session: SapSession) -> Optional[Any]:
        for element_id in _GRID_CANDIDATES:
            grid = session.optional(element_id)
            if grid is not None:
                log.info("ALV grid found at %s", element_id)
                return grid
        log.info("No ALV grid control found; treating the output as a classic list.")
        return None

    def _guard_empty_result(self, session: SapSession, grid: Optional[Any]) -> None:
        message = session.status()
        text = (message.text or "").lower()
        if any(hint in text for hint in _EMPTY_RESULT_HINTS):
            raise EmptyResultError(
                f"The selection returned no rows (SAP said: {message.text})."
            )
        rows = _grid_row_count(grid)
        if rows == 0:
            raise EmptyResultError("The ALV grid came back with zero rows.")
        still_on_selection = session.exists(_GET_VARIANT_BUTTON) and grid is None
        if still_on_selection:
            raise EmptyResultError(
                "SAP stayed on the selection screen after execute, which normally "
                f"means nothing matched. Status bar: {message.text or '<empty>'}"
            )

    def _export_text(
        self,
        session: SapSession,
        grid: Optional[Any],
        target: Path,
        progress: ProgressFn,
    ) -> None:
        progress("Exporting the result list as tab-delimited text.")
        if grid is not None:
            _grid_context_export(grid, "&PC")
            session.wait_ready()
        else:
            session.set_text("wnd[0]/tbar[0]/okcd", "%PC")
            session.send_vkey(VKEY_ENTER)

        self._choose_format_if_asked(session, prefer=("spreadsheet", "tab"))
        self._fill_save_dialog(session, target)

    def _export_native_xlsx(
        self,
        session: SapSession,
        grid: Optional[Any],
        target: Path,
        progress: ProgressFn,
    ) -> None:
        if grid is None:
            raise ExportError(
                "export.mode='native_xlsx' needs an ALV grid, but this output is a "
                "classic list. Use export.mode='text_then_convert'."
            )
        progress("Exporting through SAP's own spreadsheet dialog.")
        _grid_context_export(grid, "&XXL")
        session.wait_ready()
        self._choose_format_if_asked(session, prefer=("xlsx", "excel"))
        self._fill_save_dialog(session, target)

    def _choose_format_if_asked(
        self, session: SapSession, prefer: Sequence[str]
    ) -> None:
        """Pick a radio button by its label instead of guessing an index."""
        if not session.exists("wnd[1]") or session.exists(_SAVE_DIALOG_PATH):
            return
        options = _radio_buttons(session)
        if not options:
            session.send_vkey(VKEY_ENTER, "wnd[1]")
            return
        chosen = None
        for keyword in prefer:
            for element_id, label in options:
                if keyword in label.lower():
                    chosen = (element_id, label)
                    break
            if chosen:
                break
        if chosen is None:
            chosen = options[0]
        log.info("Export format dialog: choosing '%s'", chosen[1])
        session.set_checked(chosen[0], True)
        session.send_vkey(VKEY_ENTER, "wnd[1]")

    def _fill_save_dialog(self, session: SapSession, target: Path) -> None:
        if not session.exists(_SAVE_DIALOG_PATH):
            session.dismiss_popups()
        if not session.exists(_SAVE_DIALOG_PATH):
            raise ExportError(
                "SAP never opened its save dialog. Record the export once with "
                "Alt+F12 and paste the ids into the config."
            )

        session.set_text(_SAVE_DIALOG_PATH, str(target.parent) + "\\")
        session.set_text(_SAVE_DIALOG_FILENAME, target.name)
        if session.exists(_SAVE_DIALOG_ENCODING):
            session.set_text(_SAVE_DIALOG_ENCODING, _UTF8_CODEPAGE)

        for button in _SAVE_DIALOG_BUTTONS:
            if session.exists(button):
                session.press(button)
                break
        else:
            raise ExportError("The save dialog had no button we recognise.")

        session.dismiss_popups()
        session.raise_on_error()


def _type_multiple_selection(
    session: SapSession, field_name: str, values: List[str]
) -> None:
    """Fallback for releases without the clipboard button: fill the grid rows."""
    for index, value in enumerate(values):
        element_id = _MULTI_TABLE_ROW.format(row=index)
        if not session.exists(element_id):
            raise SapError(
                f"Only {index} of {len(values)} values for {field_name} fit in the "
                "visible rows of the multiple-selection dialog. Put these values in a "
                "saved variant instead, and set selection.variant."
            )
        session.set_text(element_id, value)


def _grid_context_export(grid: Any, context_item: str) -> None:
    try:
        grid.pressToolbarContextButton("&MB_EXPORT")
        grid.selectContextMenuItem(context_item)
    except Exception as exc:
        raise ExportError(
            "The ALV export button did not respond. This grid may not offer "
            f"'{context_item}'; record the export with Alt+F12 to see what your "
            "system uses."
        ) from exc


def _grid_row_count(grid: Optional[Any]) -> Optional[int]:
    if grid is None:
        return None
    for attr in ("RowCount", "VisibleRowCount"):
        try:
            value = int(getattr(grid, attr))
        except Exception:
            continue
        if value >= 0:
            return value
    return None


def _radio_buttons(session: SapSession) -> List[tuple]:
    """Enumerate the radio buttons of the active popup as (id, label) pairs.

    The "Select Spreadsheet" dialog nests its options inside
    subSUBSCREEN_STEPLOOP:SAPLSPO5:0150, so this has to walk the tree rather
    than look at the direct children of wnd[1]/usr.
    """
    container = session.optional("wnd[1]/usr")
    if container is None:
        return []
    found: List[tuple] = []
    _collect_radio_buttons(container, found, depth=0)
    return found


def _collect_radio_buttons(node: Any, found: List[tuple], depth: int) -> None:
    if depth > 8:
        return
    try:
        count = int(node.Children.Count)
    except Exception:
        return
    for index in range(count):
        try:
            child = node.Children(index)
            kind = str(child.Type)
        except Exception:
            continue
        if kind == "GuiRadioButton":
            try:
                found.append((_relative_id(str(child.Id)), str(child.Text or "")))
            except Exception:
                continue
        else:
            _collect_radio_buttons(child, found, depth + 1)


def _relative_id(element_id: str) -> str:
    marker = "/ses[0]/"
    if marker in element_id:
        return element_id.split(marker, 1)[1]
    index = element_id.find("wnd[")
    return element_id[index:] if index >= 0 else element_id


def _staging_filename(config: Config) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "xlsx" if config.export.mode == "native_xlsx" else "txt"
    return f"{config.selection.transaction}_raw_{stamp}.{suffix}"


def _await_file(path: Path, timeout_s: int = 60) -> None:
    """SAP writes asynchronously, so wait for the size to stop changing."""
    deadline = time.monotonic() + timeout_s
    last_size = -1
    stable_polls = 0
    while time.monotonic() < deadline:
        if path.exists():
            size = path.stat().st_size
            stable_polls = stable_polls + 1 if size > 0 and size == last_size else 0
            last_size = size
            if stable_polls >= 3:
                return
        time.sleep(0.25)
    if not path.exists():
        raise ExportError(
            f"SAP reported success but {path} was never created. Check that the "
            "account running this has write access to the staging folder."
        )
    if path.stat().st_size == 0:
        raise ExportError(f"SAP created {path.name} but it is empty.")
