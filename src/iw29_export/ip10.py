"""Look up the next IP10 PlanDate for a list of maintenance plans.

The recorded planDate.vbs only opens /nIP10 and scrolls the scheduling table.
This driver enters each plan, reads PlanDate plus status, and keeps the earliest
call that is still on or after today and is not Complete.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import PROJECT_ROOT, Config
from .credentials import resolve
from .errors import ConfigError, ExportError, SapError
from .logging_setup import get_logger
from .sap import SapGui, SapSession, relative_id

log = get_logger("ip10")

DEFAULT_LIST = PROJECT_ROOT / "data" / "ip10_maintenance_plans.txt"
DEFAULT_OUT = PROJECT_ROOT / "data" / "ip10_plan_dates.csv"

_SAP_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_PLAN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,40}$")

_PLAN_FIELDS = (
    "wnd[0]/usr/ctxtRMIPM-WARPL",
    "wnd[0]/usr/ctxtWARPL",
    "wnd[0]/usr/txtRMIPM-WARPL",
)
# Live FR3 path. Screen numbers drift (8031 vs the recording's 0121), so the
# reader also falls back to the table name SAPLIWP3TCTRL_0121.
_TABLE_ID = (
    "wnd[0]/usr/subSUBSCREEN_SCHEDULING:SAPLIWP3:8003/"
    "tabsTABSTRIP_SCHEDULING/tabpT\\01/"
    "ssubSUBSCREEN_BODY:SAPLIWP3:8031/subSUBSCREEN_CALL:SAPLIWP3:0121/"
    "tblSAPLIWP3TCTRL_0121"
)
_TABLE_NAME = "SAPLIWP3TCTRL_0121"

CallRow = Tuple[str, str]


@dataclass
class PlanDateResult:
    plan: str
    plan_date: str
    detail: str = ""


def load_plans(path: Path) -> List[str]:
    """One maintenance plan per line. Comments and blanks are ignored."""
    if not path.is_file():
        raise ConfigError(f"Maintenance plan list not found: {path}")
    plans: List[str] = []
    seen = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        plan = re.split(r"[\s,;]+", text, maxsplit=1)[0].strip()
        if not _PLAN_RE.match(plan):
            raise ConfigError(f"Not a maintenance plan id: {plan!r} in {path.name}")
        key = plan.upper()
        if key in seen:
            continue
        seen.add(key)
        plans.append(plan)
    if not plans:
        raise ExportError(f"No maintenance plans found in {path.name}.")
    log.info("Loaded %d maintenance plan(s) from %s", len(plans), path.name)
    return plans


def parse_sap_date(text: str) -> Optional[date]:
    match = _SAP_DATE.match((text or "").strip())
    if not match:
        return None
    day, month, year = (int(match.group(i)) for i in (1, 2, 3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def format_sap_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def next_plan_date(calls: Sequence[CallRow], today: date) -> str:
    """Earliest PlanDate on or after today whose status is not Complete.

    ``calls`` are ``(plan_date_text, status_text)`` as shown on the IP10 list.
    The returned text stays in SAP's ``DD.MM.YYYY`` form. Empty when none qualify.
    """
    best: Optional[date] = None
    for raw_date, status in calls:
        if "complete" in (status or "").lower():
            continue
        parsed = parse_sap_date(raw_date)
        if parsed is None or parsed < today:
            continue
        if best is None or parsed < best:
            best = parsed
    return format_sap_date(best) if best else ""


def write_csv(path: Path, rows: Sequence[PlanDateResult]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Inspection Plan", "PlanDate"])
        for row in rows:
            writer.writerow([row.plan, row.plan_date])
    log.info("Wrote %d row(s) to %s", len(rows), path)
    return path


def run(
    config: Config,
    list_path: Path,
    out_path: Path,
    today: Optional[date] = None,
    password: Optional[str] = None,
) -> List[PlanDateResult]:
    plans = load_plans(list_path)
    as_of = today or date.today()
    if config.runtime.mock:
        rows = [_mock_row(plan, as_of) for plan in plans]
        write_csv(out_path, rows)
        return rows

    secret = password if password is not None else resolve(config)
    gui = SapGui.from_config(config, secret)
    rows: List[PlanDateResult] = []
    with gui.session() as session:
        for index, plan in enumerate(plans, start=1):
            log.info("[%d/%d] %s", index, len(plans), plan)
            try:
                calls = _read_plan(session, plan)
                chosen = next_plan_date(calls, as_of)
                detail = "" if chosen else "no upcoming PlanDate"
                log.info("%s -> %s (%d call(s))", plan, chosen or "<none>", len(calls))
            except Exception as exc:
                log.exception("Failed on plan %s", plan)
                chosen = ""
                detail = str(exc)
            rows.append(PlanDateResult(plan=plan, plan_date=chosen, detail=detail))
    write_csv(out_path, rows)
    return rows


def _mock_row(plan: str, today: date) -> PlanDateResult:
    """Stable demo calls so a list run can be checked without SAP."""
    year = today.year
    calls = [
        (f"14.02.{year - 4}", "New start Complete"),
        (f"15.08.{year + 3}", "New start Hold"),
        (f"15.02.{year + 7}", "Scheduled Hold"),
    ]
    if plan.upper().endswith("0"):
        calls = [(f"14.02.{year - 4}", "New start Complete")]
    return PlanDateResult(plan=plan, plan_date=next_plan_date(calls, today))


def _read_plan(session: SapSession, plan: str) -> List[CallRow]:
    session.start_transaction("IP10")
    session.dismiss_popups()
    field_id = _find_plan_field(session)
    session.set_text(field_id, plan)
    session.send_vkey(0)
    session.dismiss_popups()
    session.raise_on_error()
    table = _find_table(session)
    if table is None:
        status = session.status()
        raise SapError(
            f"IP10 did not open a scheduling list for {plan}. "
            f"Status: {status.text or session.window_title() or 'no table'}"
        )
    return _read_calls(table)


def _find_plan_field(session: SapSession) -> str:
    for element_id in _PLAN_FIELDS:
        if session.exists(element_id):
            return element_id
    user = session.optional("wnd[0]/usr")
    if user is not None:
        found = _walk_for_plan_field(user)
        if found:
            return found
    raise SapError(
        "IP10 maintenance-plan field was not on screen. "
        "Run inspect on IP10 and record the field id."
    )


def _walk_for_plan_field(node) -> str:
    try:
        count = int(node.Children.Count)
    except Exception:
        return ""
    for index in range(count):
        try:
            child = node.Children(index)
        except Exception:
            continue
        try:
            kind = str(child.Type)
        except Exception:
            kind = ""
        element_id = ""
        try:
            element_id = relative_id(str(child.Id))
        except Exception:
            element_id = ""
        blob = " ".join(
            _attr(child, name) for name in ("Name", "Tooltip", "Text")
        ).upper()
        if kind in {"GuiCTextField", "GuiTextField"} and (
            "WARPL" in element_id.upper() or "MAINT" in blob or "PLAN" in blob
        ):
            return element_id
        nested = _walk_for_plan_field(child)
        if nested:
            return nested
    return ""


def _find_table(session: SapSession):
    if session.wait_for_element(_TABLE_ID, timeout_s=4):
        table = session.find(_TABLE_ID)
        if _int_attr(table, "RowCount") > 0:
            return table
    user = session.optional("wnd[0]/usr")
    tables: List = []
    if user is not None:
        _collect_tables(user, tables)
    named = [table for table in tables if _attr(table, "Name") == _TABLE_NAME]
    pool = named or tables
    if not pool:
        return None
    pool.sort(key=lambda table: _int_attr(table, "RowCount"), reverse=True)
    best = pool[0]
    if _int_attr(best, "RowCount") <= 0:
        return None
    return best


def _collect_tables(node, found: List) -> None:
    try:
        count = int(node.Children.Count)
    except Exception:
        return
    for index in range(count):
        try:
            child = node.Children(index)
        except Exception:
            continue
        try:
            kind = str(child.Type)
        except Exception:
            kind = ""
        if kind == "GuiTableControl":
            found.append(child)
        _collect_tables(child, found)


def _read_calls(table) -> List[CallRow]:
    # Read the open page first. Scrolling the GuiTableControl can drop the COM
    # object, and the calls SAP shows on open are the upcoming ones.
    rows = _read_visible_pairs(table)
    if rows:
        return rows
    total = _int_attr(table, "RowCount")
    visible = _int_attr(table, "VisibleRowCount") or total
    try:
        return _read_visible_via_getcell(table, min(visible or total, total or visible))
    except Exception:
        return []


def _read_visible_pairs(table) -> List[CallRow]:
    """PlanDate is RMIPM-NPLDA; status is TERMFELD2. Ids look like name[col,row]."""
    dates = {}
    statuses = {}
    try:
        count = int(table.Children.Count)
    except Exception:
        return []
    for index in range(count):
        try:
            child = table.Children(index)
        except Exception:
            continue
        name = _attr(child, "Name")
        if name not in {"RMIPM-NPLDA", "TERMFELD2"}:
            continue
        row = _visible_row(child)
        if row is None:
            continue
        text = _attr(child, "Text")
        if name == "RMIPM-NPLDA":
            dates[row] = text
        else:
            statuses[row] = text
    pairs: List[CallRow] = []
    for row in sorted(set(dates) | set(statuses)):
        pairs.append((dates.get(row, ""), statuses.get(row, "")))
    return pairs


def _visible_row(node) -> Optional[int]:
    match = re.search(r"\[(\d+),(\d+)\]", _attr(node, "Id"))
    if not match:
        return None
    return int(match.group(2))


def _read_visible_via_getcell(table, count: int) -> List[CallRow]:
    date_col = _column_index(table, "plandate", "plan date")
    status_col = _column_index(table, "scheduling type", "status")
    if date_col is None:
        date_col = 1
    rows: List[CallRow] = []
    for offset in range(count):
        raw_date = _cell_text(table, offset, date_col)
        status = _cell_text(table, offset, status_col) if status_col is not None else ""
        if raw_date or status:
            rows.append((raw_date, status))
    return rows


def _column_index(table, *needles: str) -> Optional[int]:
    try:
        columns = getattr(table, "Columns", None)
    except Exception:
        return None
    if columns is None:
        return None
    try:
        count = int(columns.Count)
    except Exception:
        return None
    for index in range(count):
        try:
            column = columns(index)
        except Exception:
            try:
                column = columns.Item(index)
            except Exception:
                continue
        title = _attr(column, "Title").lower()
        if any(needle in title for needle in needles):
            return index
    return None


def _cell_text(table, row: int, column: int) -> str:
    try:
        cell = table.GetCell(row, column)
    except Exception:
        return ""
    return _attr(cell, "Text")


def _int_attr(node, name: str) -> int:
    try:
        return int(getattr(node, name))
    except Exception:
        return 0


def _attr(node, name: str) -> str:
    try:
        return str(getattr(node, name) or "").strip()
    except Exception:
        return ""
