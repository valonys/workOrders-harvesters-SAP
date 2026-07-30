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
    VKEY_CHOOSE,
    VKEY_ENTER,
    VKEY_EXECUTE,
    VKEY_F12_CANCEL,
    VKEY_GET_VARIANT,
    SapGui,
    SapSession,
    relative_id,
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

# Confirmed on FR3: tbar[1]/btn[17] is "Get Variant... (Shift+F5)", and the same
# action is on the menu at Goto > Variants > Get...
_GET_VARIANT_BUTTON = "wnd[0]/tbar[1]/btn[17]"
_GET_VARIANT_MENU = "wnd[0]/mbar/menu[2]/menu[0]/menu[0]"
_VARIANT_NAME_FIELD = "wnd[1]/usr/txtV-LOW"
_VARIANT_OWNER_FIELD = "wnd[1]/usr/txtENAME-LOW"

# The single-values grid of the multiple-selection dialog, and its "Copy" button.
_MULTI_TABLE_ROW = (
    "wnd[1]/usr/tabsTAB_STRIP/tabpSIVA/ssubSCREEN_HEADER:SAPLALDB:3010/"
    "tblSAPLALDBSINGLE/ctxtRSCSEL_255-SLOW_I[1,{row}]"
)
_MULTI_COPY_BUTTON = "wnd[1]/tbar[0]/btn[8]"

# Ways to reach "save the list to a local file", by menu label rather than index.
_EXPORT_MENU_PATHS = (
    # IW29's ALV list on FR3 puts it here; "Spreadsheet" next to it is the XXL
    # route that ends in Excel, so label matching has to be exact.
    ("List", "Save", "File"),
    ("List", "Save", "Local File"),
    ("System", "List", "Save", "Local File"),
    ("List", "Export", "Local File"),
    ("List", "Save/Send", "File"),
)
_EXPORT_TOOLTIP_WORDS = ("local file", "spreadsheet", "export", "download")

# Dialog text that means SAP is about to hand the data to Excel rather than
# write a file we can name. There is nothing scriptable past this point.
_EXCEL_ROUTE_MARKERS = ("XXL", "MHTML", "SAVE THE DATA IN THE SPREADSHEET")

# Format options of the "Save list in file" dialog, best first. Tab-delimited
# parses most cleanly; the fixed-width "unconverted" form also works.
_LOCAL_FILE_FORMATS = ("tab", "spreadsheet", "unconverted", "text")

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
        gui = SapGui.from_config(cfg, self._password)

        progress(f"Connecting to SAP system {cfg.sap.system}...")
        with gui.session() as session:
            self.open_transaction(session, progress)
            self.apply_selection(session, progress)
            grid = self.execute(session, progress)

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

    # ---------------------------------------------------------------- steps

    def open_transaction(self, session: SapSession, progress: ProgressFn) -> None:
        progress(f"Opening transaction {self.config.selection.transaction}.")
        session.start_transaction(self.config.selection.transaction)
        session.dismiss_popups()

    def apply_selection(self, session: SapSession, progress: ProgressFn) -> None:
        self._apply_variant(session, progress)
        self._apply_layout(session)
        # After the variant on purpose: the variant carries the dates it was saved
        # with, and those must not decide what this report covers.
        self._apply_notification_dates(session)
        self._apply_filters(session, progress)
        self._apply_checkboxes(session)
        self._apply_raw_steps(session)

    def execute(self, session: SapSession, progress: ProgressFn) -> Optional[Any]:
        """Run the report and return the ALV grid control, if there is one."""
        progress("Executing the report...")
        started = time.monotonic()
        session.send_vkey(VKEY_EXECUTE)
        session.raise_on_error()
        session.dismiss_popups()
        log.info("Report finished in %.1fs", time.monotonic() - started)

        grid = self._find_grid(session)
        self._guard_empty_result(session, grid)
        return grid

    # ---------------------------------------------------------------- selection

    def _apply_variant(self, session: SapSession, progress: ProgressFn) -> None:
        variant = self.config.selection.variant
        if not variant:
            return
        progress(f"Loading selection variant '{variant}'.")

        if not self._open_variant_dialog(session):
            raise SapError(
                f"Could not open the 'Get Variant' dialog, so selection.variant="
                f"'{variant}' cannot be applied. Tried the toolbar button, Shift+F5 "
                "and Goto > Variants > Get..."
            )

        if session.exists(_VARIANT_NAME_FIELD):
            session.set_text(_VARIANT_NAME_FIELD, variant)
        else:
            raise SapError(
                f"The variant dialog has no name field at {_VARIANT_NAME_FIELD} on "
                "this release. Run 'iw29-export inspect' with the dialog open to find "
                "the right id."
            )
        # Blank the "created by" filter, otherwise only your own variants match.
        if session.exists(_VARIANT_OWNER_FIELD):
            session.set_text(_VARIANT_OWNER_FIELD, "")

        session.send_vkey(VKEY_EXECUTE, "wnd[1]")

        # One match loads straight away; several leave a pick list open.
        if session.wait_for_window("wnd[1]", timeout_s=2):
            self._choose_from_variant_list(session, variant)

        session.raise_on_error()
        log.info("Variant '%s' loaded.", variant)

    def _open_variant_dialog(self, session: SapSession) -> bool:
        attempts = (
            (_GET_VARIANT_BUTTON, "toolbar button", session.press),
            (None, "Shift+F5", None),
            (_GET_VARIANT_MENU, "Goto > Variants > Get...", session.select),
        )
        for element_id, label, action in attempts:
            if element_id is None:
                session.send_vkey(VKEY_GET_VARIANT)
            elif session.exists(element_id) and action is not None:
                action(element_id)
            else:
                continue
            if session.wait_for_window("wnd[1]", timeout_s=6):
                log.info("Variant dialog opened via the %s.", label)
                return True
            log.info("The %s did not open a dialog; trying the next route.", label)
        return False

    def _choose_from_variant_list(self, session: SapSession, variant: str) -> None:
        grid = None
        for element_id in _GRID_CANDIDATES:
            grid = session.optional(element_id.replace("wnd[0]", "wnd[1]"))
            if grid is not None:
                break
        if grid is not None:
            try:
                grid.currentCellRow = 0
                grid.doubleClickCurrentCell()
                session.wait_ready()
                return
            except Exception:
                log.info("Could not double-click the variant list; pressing Choose.")
        session.send_vkey(VKEY_CHOOSE, "wnd[1]")
        if session.exists("wnd[1]"):
            raise SapError(
                f"A dialog stayed open after selecting variant '{variant}': "
                f"{session.popup_text() or '<no text>'}. Check the variant name."
            )

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

    def _apply_notification_dates(self, session: SapSession) -> None:
        window = self.config.selection.notification_date
        if not window.enabled:
            log.info("Leaving the notification date fields as the variant set them.")
            return

        written = []
        for field_name, value in (
            (window.field_low, self._expand(window.low)),
            (window.field_high, self._expand(window.high)),
        ):
            element_id = self._plain_field(session, field_name)
            if element_id is None:
                raise SapError(
                    f"The notification date field {field_name} is not on the "
                    f"{self.config.selection.transaction} selection screen. Run "
                    "'iw29-export inspect' to find its id, then correct "
                    "selection.notification_date.field_from/field_to."
                )
            session.set_text(element_id, value)
            written.append((field_name, element_id, value))

        # Read the fields back, so the log proves what SAP was actually asked for
        # rather than what we intended. This feeds a KPI, so it has to be checkable.
        for field_name, element_id, value in written:
            actual = session.text(element_id)
            if actual.strip() != value.strip():
                raise SapError(
                    f"Set {field_name} to {value or '<blank>'} but the screen shows "
                    f"{actual or '<blank>'}. The variant or a user exit may be "
                    "overwriting it."
                )
        log.info(
            "Notification date window: %s to %s (%s / %s)",
            written[0][2] or "<blank>",
            written[1][2] or "<blank>",
            window.field_low,
            window.field_high,
        )

    def _plain_field(self, session: SapSession, field_name: str) -> Optional[str]:
        for prefix in _FIELD_PREFIXES:
            element_id = f"wnd[0]/usr/{prefix}{field_name}"
            if session.exists(element_id):
                return element_id
        return None

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
        self._open_export_dialog(session, grid, "&PC")
        self._fill_save_dialog(session, target)

    def _export_native_xlsx(
        self,
        session: SapSession,
        grid: Optional[Any],
        target: Path,
        progress: ProgressFn,
    ) -> None:
        progress("Exporting through SAP's own spreadsheet dialog.")
        self._open_export_dialog(session, grid, "&XXL")
        self._fill_save_dialog(session, target)

    def _open_export_dialog(
        self, session: SapSession, grid: Optional[Any], context_item: str
    ) -> None:
        """Reach SAP's local-file save dialog, backing out of dead ends.

        Several buttons look like "export" but lead to the XXL/Excel route, which
        hands the file to Excel and offers nothing a script can fill in. So each
        route is followed only as far as needed to tell whether it produces the
        DY_PATH dialog; if it does not, the popups are cancelled and the next
        route is tried.
        """
        tried: List[str] = []
        for label, attempt in self._export_routes(session, grid, context_item):
            tried.append(label)
            try:
                attempt()
            except Exception as exc:
                log.info("Export via %s was not available: %s", label, _brief(exc))
                continue
            if not session.wait_for_window("wnd[1]", timeout_s=10):
                log.info("%s opened no dialog; trying the next route.", label)
                continue
            if self._reach_save_dialog(session):
                log.info("Save dialog reached via %s.", label)
                return
            log.info("%s leads to the Excel route, not a local file; backing out.", label)
            _cancel_popups(session)

        raise ExportError(
            "None of these export routes reached SAP's local-file save dialog: "
            + "; ".join(tried)
            + ". Run 'iw29-export inspect --execute' to dump the result screen, then "
            "set the working id under [[selection.raw]]."
        )

    def _reach_save_dialog(self, session: SapSession, hops: int = 4) -> bool:
        """Walk through format dialogs until DY_PATH appears, or give up."""
        for _ in range(hops):
            if session.exists(_SAVE_DIALOG_PATH):
                return True
            text = session.popup_text()
            if any(marker in text.upper() for marker in _EXCEL_ROUTE_MARKERS):
                return False
            options = _radio_buttons(session)
            if not options:
                return False
            chosen = _prefer_option(options, _LOCAL_FILE_FORMATS)
            log.info("Format dialog: choosing %r", chosen[1])
            session.set_checked(chosen[0], True)
            session.send_vkey(VKEY_ENTER, "wnd[1]")
            session.wait_for_window("wnd[1]", timeout_s=5)
        return session.exists(_SAVE_DIALOG_PATH)

    def _export_routes(
        self, session: SapSession, grid: Optional[Any], context_item: str
    ) -> List[tuple]:
        """Ordered best-first. For text output the local-file routes come first."""
        grid_routes: List[tuple] = []
        if grid is not None:
            grid_routes = [
                (
                    f"the ALV grid context item {context_item}",
                    lambda: _grid_select_context(grid, context_item),
                ),
                (
                    f"the ALV export menu then {context_item}",
                    lambda: _grid_context_export(grid, context_item),
                ),
            ]

        menu_routes: List[tuple] = []
        for labels in _EXPORT_MENU_PATHS:
            menu_id = session.menu_id(labels)
            if menu_id:
                menu_routes.append(
                    (f"the menu {' > '.join(labels)}", _menu_action(session, menu_id))
                )

        toolbar_routes: List[tuple] = []
        for element_id, tooltip in _toolbar_buttons(session):
            if any(word in tooltip.lower() for word in _EXPORT_TOOLTIP_WORDS):
                toolbar_routes.append(
                    (
                        f"the toolbar button {element_id} ({tooltip.strip()!r})",
                        _press_action(session, element_id),
                    )
                )

        ok_code_routes = [("the %PC command", _ok_code_action(session, "%PC"))]

        if context_item == "&XXL":
            return grid_routes + toolbar_routes + menu_routes + ok_code_routes
        return grid_routes + ok_code_routes + menu_routes + toolbar_routes

    def _fill_save_dialog(self, session: SapSession, target: Path) -> None:
        if not session.wait_for_element(_SAVE_DIALOG_PATH, timeout_s=10):
            session.dismiss_popups()
        if not session.wait_for_element(_SAVE_DIALOG_PATH, timeout_s=10):
            raise ExportError(
                "SAP never opened its save dialog. Run 'iw29-export inspect' after "
                "executing the report to see what the export button actually opens."
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


def _menu_action(session: SapSession, element_id: str):
    return lambda: session.select(element_id)


def _press_action(session: SapSession, element_id: str):
    return lambda: session.press(element_id)


def _ok_code_action(session: SapSession, code: str):
    def run() -> None:
        session.set_text("wnd[0]/tbar[0]/okcd", code)
        session.send_vkey(VKEY_ENTER)

    return run


def _toolbar_buttons(session: SapSession) -> List[tuple]:
    """Application-toolbar buttons as (id, tooltip), so they can be matched by name."""
    found: List[tuple] = []
    for container_id in ("wnd[0]/tbar[1]", "wnd[0]/tbar[0]"):
        container = session.optional(container_id)
        if container is None:
            continue
        try:
            count = int(container.Children.Count)
        except Exception:
            continue
        for index in range(count):
            try:
                child = container.Children(index)
                if str(child.Type) != "GuiButton":
                    continue
                found.append((relative_id(str(child.Id)), str(child.Tooltip or "")))
            except Exception:
                continue
    return found


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
    grid.pressToolbarContextButton("&MB_EXPORT")
    grid.selectContextMenuItem(context_item)


def _grid_select_context(grid: Any, context_item: str) -> None:
    """Fire a grid context function directly, without opening its export menu."""
    grid.selectContextMenuItem(context_item)


def _prefer_option(options: List[tuple], keywords: Sequence[str]) -> tuple:
    for keyword in keywords:
        for element_id, label in options:
            if keyword in label.lower():
                return element_id, label
    return options[0]


def _cancel_popups(session: SapSession, limit: int = 5) -> None:
    for _ in range(limit):
        if not session.exists("wnd[1]"):
            return
        try:
            session.send_vkey(VKEY_F12_CANCEL, "wnd[1]")
        except Exception:
            break
    if session.exists("wnd[1]"):
        log.warning(
            "Could not close a SAP dialog while backing out: %s",
            session.popup_text() or "<no text>",
        )


def _brief(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= 160 else text[:157] + "..."


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
                found.append((relative_id(str(child.Id)), str(child.Text or "")))
            except Exception:
                continue
        else:
            _collect_radio_buttons(child, found, depth + 1)


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
