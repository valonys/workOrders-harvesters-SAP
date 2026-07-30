"""Dumps the real ids on a SAP screen, so nothing has to be guessed.

Element ids differ between releases and customising. Running this against your
own system is faster than reading a recording, because it also shows the
tooltips and menu texts that tell you what each id actually does.
"""

from __future__ import annotations

from typing import Any, List, Optional

from .config import Config
from .credentials import resolve
from .logging_setup import get_logger
from .sap import SapGui, SapSession

log = get_logger("inspect")

_GRID_IDS = (
    "wnd[0]/usr/cntlGRID1/shellcont/shell",
    "wnd[0]/usr/cntlCONTAINER/shellcont/shell",
    "wnd[0]/usr/cntlALV_CONTAINER/shellcont/shell",
)

_INTERESTING = {
    "GuiTextField",
    "GuiCTextField",
    "GuiPasswordField",
    "GuiCheckBox",
    "GuiRadioButton",
    "GuiButton",
    "GuiComboBox",
    "GuiLabel",
    "GuiTableControl",
    "GuiShell",
    "GuiCustomControl",
}


def dump(
    config: Config, password: Optional[str] = None, execute: bool = False
) -> str:
    secret = password if password is not None else resolve(config)
    gui = SapGui.from_config(config, secret)

    lines: List[str] = []
    with gui.session() as session:
        from .iw29 import SapIw29Source

        source = SapIw29Source(config, secret)
        source.open_transaction(session, log.info)
        if execute:
            source.apply_selection(session, log.info)
            source.execute(session, log.info)

        lines.append(f"Stage       : {'result list' if execute else 'selection screen'}")
        lines.append(f"System      : {session.system} client {session.client}")
        lines.append(f"User        : {session.user}")
        lines.append(f"Transaction : {session.transaction}")
        lines.append(f"Window title: {_text_of(session.optional('wnd[0]'))}")
        lines.append("")

        lines.append("=== Application toolbar (tbar[1]) ===")
        lines.extend(_toolbar(session, "wnd[0]/tbar[1]"))
        lines.append("")
        lines.append("=== Standard toolbar (tbar[0]) ===")
        lines.extend(_toolbar(session, "wnd[0]/tbar[0]"))
        lines.append("")
        lines.append("=== Menus ===")
        lines.extend(_menus(session))
        lines.append("")
        lines.append("=== Screen contents (wnd[0]/usr) ===")
        container = session.optional("wnd[0]/usr")
        if container is None:
            lines.append("  (no user area)")
        else:
            _walk(container, lines, depth=0)

        if execute:
            lines.append("")
            lines.append("=== ALV grid toolbar (export function codes) ===")
            lines.extend(_grid_toolbar(session))

    return "\n".join(lines)


def _grid_toolbar(session: SapSession) -> List[str]:
    """The grid's own buttons, whose function codes are what selectContextMenuItem
    and pressToolbarContextButton expect."""
    grid = None
    for element_id in _GRID_IDS:
        grid = session.optional(element_id)
        if grid is not None:
            break
    if grid is None:
        return ["  (no ALV grid on this screen)"]

    rows = [f"  grid at {_short_id(grid)}"]
    try:
        count = int(grid.ToolbarButtonCount)
    except Exception as exc:
        return rows + [f"  (grid exposes no toolbar: {exc})"]

    for index in range(count):
        parts = []
        for attr in ("GetToolbarButtonId", "GetToolbarButtonText", "GetToolbarButtonTooltip"):
            try:
                parts.append(str(getattr(grid, attr)(index) or ""))
            except Exception:
                parts.append("?")
        rows.append(f"  [{index:2d}] code={parts[0]!r} text={parts[1]!r} tooltip={parts[2]!r}")
    return rows


def _toolbar(session: SapSession, container_id: str) -> List[str]:
    container = session.optional(container_id)
    if container is None:
        return ["  (not present)"]
    rows: List[str] = []
    for child in _children(container):
        try:
            kind = str(child.Type)
            if kind not in {"GuiButton", "GuiToolbarControl"}:
                continue
            rows.append(
                f"  {_short_id(child)}  {_text_of(child)!r}"
                f"  tooltip={_tooltip_of(child)!r}"
            )
        except Exception:
            continue
    return rows or ["  (no buttons)"]


def _menus(session: SapSession) -> List[str]:
    bar = session.optional("wnd[0]/mbar")
    if bar is None:
        return ["  (no menu bar)"]
    rows: List[str] = []
    for menu in _children(bar):
        _menu_branch(menu, rows, prefix="")
    return rows or ["  (empty menu bar)"]


def _menu_branch(node: Any, rows: List[str], prefix: str, depth: int = 0) -> None:
    if depth > 3:
        return
    try:
        label = str(node.Text or "").strip()
    except Exception:
        return
    path = f"{prefix} > {label}" if prefix else label
    children = _children(node)
    if children:
        for child in children:
            _menu_branch(child, rows, path, depth + 1)
    else:
        rows.append(f"  {_short_id(node)}  {path}")


def _walk(node: Any, lines: List[str], depth: int) -> None:
    if depth > 8:
        return
    for child in _children(node):
        try:
            kind = str(child.Type)
        except Exception:
            continue
        if kind in _INTERESTING:
            indent = "  " * (depth + 1)
            detail = f"{indent}{_short_id(child)}  [{kind}]"
            text = _text_of(child)
            if text:
                detail += f"  text={text!r}"
            tooltip = _tooltip_of(child)
            if tooltip and tooltip != text:
                detail += f"  tooltip={tooltip!r}"
            lines.append(detail)
        _walk(child, lines, depth + 1)


def _children(node: Any) -> List[Any]:
    try:
        count = int(node.Children.Count)
    except Exception:
        return []
    found = []
    for index in range(count):
        try:
            found.append(node.Children(index))
        except Exception:
            continue
    return found


def _short_id(node: Any) -> str:
    try:
        raw = str(node.Id)
    except Exception:
        return "<no id>"
    marker = "/ses[0]/"
    if marker in raw:
        return raw.split(marker, 1)[1]
    index = raw.find("wnd[")
    return raw[index:] if index >= 0 else raw


def _text_of(node: Any) -> str:
    if node is None:
        return ""
    try:
        return str(node.Text or "").strip()
    except Exception:
        return ""


def _tooltip_of(node: Any) -> str:
    for attr in ("Tooltip", "IconName", "Name"):
        try:
            value = str(getattr(node, attr) or "").strip()
        except Exception:
            continue
        if value:
            return value
    return ""
