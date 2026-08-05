"""Feature-engineer IW38/IW39 harvests into Performance and Backlog KPIs.

Column mapping (CLV text export → business meaning):
  Order          → work order (user's col F in the wide SoW layout)
  UserStatus     → completion / lifecycle status (QCAP, EXDO, APPR, INIT, SWE…)
  SysStatus      → system status (secondary)
  Basic fin.     → planned due date (user's "col Q" due date in the SoW layout;
                   Actual end is often blank on open orders)
  UserStatus SCE → SECE flag

Performance = count(QCAP or EXDO) / count(work orders)
Backlog     = not complete AND (due_date + 28 days) < today
Age buckets from days overdue after the +28 grace period.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import convert, xlsx
from .config import Config
from .errors import ConfigError, ExportError
from .logging_setup import get_logger

log = get_logger("iw38_kpi")

COMPLETED_STATUSES = frozenset({"QCAP", "EXDO"})
GRACE_DAYS = 28

BACKLOG_BUCKETS = (
    ("< 6 Months", 0, 183),
    ("6 Months < x < 1 Yrs", 183, 365),
    ("1 Yrs < x < 2 Yrs", 365, 730),
    ("2 Yrs < x < 3 Yrs", 730, 1095),
    ("> 3 Yrs", 1095, 10_000_000),
)

# Item Class comes from column A (Excel XLOOKUP), never invented here.
_ITEM_CLASS_HEADERS = ("item class", "itemclass", "equipment class", "equipmentclass")


@dataclass
class KpiBuildResult:
    fact_csv: Path
    dashboard_xlsx: Path
    total_orders: int
    completed: int
    performance_pct: float
    backlog: int


def build_for_variant(
    config: Config,
    variant: str = "CLV-PG2026",
    as_of: Optional[date] = None,
    table: Optional[convert.Table] = None,
) -> KpiBuildResult:
    """Read the fixed harvest workbook for ``variant`` and publish KPI outputs."""
    cfg = config.iw38
    folder = cfg.folder or Path.home() / "IW38"
    if table is None:
        site = _site_code(variant)
        table = _load_harvest_table(folder, site=site, system=config.sap.system, variant=variant)
    return build_from_table(table, folder, variant, as_of=as_of)


def build_from_table(
    table: convert.Table,
    folder: Path,
    variant: str,
    as_of: Optional[date] = None,
    item_class_by_order: Optional[Dict[str, str]] = None,
) -> KpiBuildResult:
    as_of = as_of or date.today()
    lookup = item_class_by_order or load_item_class_lookup(folder, variant)
    table = ensure_item_class_column(table, lookup)
    # Persist the XLOOKUP values so a plain SAP re-harvest can restore col A.
    save_item_class_lookup(folder, variant, table)
    rows = [
        _enrich_row(
            headers=table.headers,
            row=row,
            as_of=as_of,
            item_class_by_order=lookup,
        )
        for row in table.rows
    ]
    if not rows:
        raise ExportError(f"{variant}: no rows to build KPIs from.")

    dataset_dir = folder / "dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    site = _site_code(variant)
    fact_csv = dataset_dir / f"{site}_wo_fact.csv"
    summary_csv = dataset_dir / f"{site}_kpi_summary.csv"
    matrix_csv = dataset_dir / f"{site}_backlog_matrix.csv"
    dashboard = folder / f"{site}_Inspection_Dashboard.xlsx"

    _write_fact_csv(fact_csv, rows)
    completed = sum(1 for r in rows if r["IsCompleted"])
    backlog = sum(1 for r in rows if r["IsBacklog"])
    total = len(rows)
    perf = (completed / total) if total else 0.0
    _write_summary_csv(
        summary_csv,
        variant=variant,
        as_of=as_of,
        total=total,
        completed=completed,
        backlog=backlog,
        performance_pct=perf,
    )
    _write_matrix_csv(matrix_csv, rows)
    _write_dashboard_xlsx(
        dashboard,
        rows,
        variant=variant,
        as_of=as_of,
        total=total,
        completed=completed,
        backlog=backlog,
        performance_pct=perf,
    )
    log.info(
        "%s KPIs: %d WOs, performance %.1f%%, backlog %d → %s",
        variant,
        total,
        perf * 100,
        backlog,
        dashboard.name,
    )
    return KpiBuildResult(
        fact_csv=fact_csv,
        dashboard_xlsx=dashboard,
        total_orders=total,
        completed=completed,
        performance_pct=perf,
        backlog=backlog,
    )


def _fixed_harvest_name(system: str, variant: str) -> str:
    return f"IW38_{system}_{variant}.xlsx"


def _site_code(variant: str) -> str:
    return variant.split("-", 1)[0].upper()


def _load_harvest_table(
    folder: Path, *, site: str, system: str, variant: str
) -> convert.Table:
    staging = Path(
        __import__("os").environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    ) / "sap-iw29-export" / "staging"
    texts = sorted(staging.glob("IW39_raw_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in texts:
        try:
            table = convert.read_sap_text(path)
        except Exception:
            continue
        if table.row_count and _looks_like_site(table, site):
            log.info("Loading KPI source from staging %s", path.name)
            return table

    harvest = folder / _fixed_harvest_name(system, variant)
    dated = sorted(folder.glob(f"IW38_{system}_{variant}_*.xlsx"), reverse=True)
    for candidate in (harvest, *dated):
        if not candidate.exists():
            continue
        try:
            return convert.read_xlsx(candidate)
        except ExportError as exc:
            log.info("Could not read %s: %s", candidate.name, exc)

    raise ConfigError(
        f"No harvest data for {variant}. Run `python -m iw29_export iw38` first "
        f"(expected {harvest})."
    )


def _looks_like_site(table: convert.Table, site: str) -> bool:
    idx = _col(table.headers, "Functional Location")
    if idx is None:
        return False
    sample = table.rows[: min(30, len(table.rows))]
    hits = sum(1 for row in sample if str(row[idx] or "").upper().startswith(site))
    return hits >= max(1, len(sample) // 2)


def _enrich_row(
    headers: Sequence[str],
    row: Sequence[Any],
    as_of: date,
    item_class_by_order: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    user_status = str(_val(headers, row, "UserStatus") or "")
    sys_status = str(_val(headers, row, "SysStatus") or "")
    tokens = set(user_status.split()) | set(sys_status.split())
    completed = bool(tokens & COMPLETED_STATUSES)
    due = _as_date(_val(headers, row, "Basic fin."))
    if due is None:
        due = _as_date(_val(headers, row, "Bsc start"))
    grace_deadline = due + timedelta(days=GRACE_DAYS) if due else None
    is_backlog = (not completed) and grace_deadline is not None and grace_deadline < as_of
    days_overdue = (as_of - grace_deadline).days if is_backlog and grace_deadline else None
    bucket = _bucket(days_overdue) if is_backlog else ""
    fl_desc = str(_val(headers, row, "Description of functional location") or "")
    desc = str(_val(headers, row, "Desc.") or "")
    fl = str(_val(headers, row, "Functional Location") or "")
    order = _val(headers, row, "Order")
    item_class = resolve_item_class(headers, row, order, item_class_by_order)
    sece = "SECE" if "SCE" in tokens else "NON SECE"
    return {
        "ItemClass": item_class,
        "WorkOrder": order,
        "Message": _val(headers, row, "Message"),
        "Description": desc,
        "UserStatus": user_status,
        "SysStatus": sys_status,
        "FunctionalLocation": fl,
        "FunctionalLocationDesc": fl_desc,
        "DueDate": due.isoformat() if due else "",
        "GraceDeadline": grace_deadline.isoformat() if grace_deadline else "",
        "IsCompleted": completed,
        "IsBacklog": is_backlog,
        "DaysOverdue": days_overdue if days_overdue is not None else "",
        "BacklogBucket": bucket,
        "SECE": sece,
        "BscStart": _iso(_val(headers, row, "Bsc start")),
        "ActualEnd": _iso(_val(headers, row, "Actual end")),
        "MnWkCtr": _val(headers, row, "Mn.wk.ctr"),
        "AsOf": as_of.isoformat(),
    }


def resolve_item_class(
    headers: Sequence[str],
    row: Sequence[Any],
    order: Any,
    item_class_by_order: Optional[Dict[str, str]] = None,
) -> str:
    """Keep Item Class exactly as column A / XLOOKUP provided it."""
    direct = _item_class_from_headers(headers, row)
    if direct:
        return direct
    key = _order_key(order)
    if item_class_by_order and key in item_class_by_order:
        return item_class_by_order[key]
    return ""


def _item_class_from_headers(headers: Sequence[str], row: Sequence[Any]) -> str:
    for index, header in enumerate(headers):
        name = str(header or "").strip().lower()
        if name in _ITEM_CLASS_HEADERS and index < len(row):
            text = str(row[index] or "").strip()
            if text:
                return text
    # Column A convention when the harvest was enriched in Excel.
    if headers and str(headers[0] or "").strip().lower() in _ITEM_CLASS_HEADERS:
        return str(row[0] or "").strip() if row else ""
    return ""


def _order_key(order: Any) -> str:
    if order is None:
        return ""
    text = str(order).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def load_item_class_lookup(folder: Path, variant: str) -> Dict[str, str]:
    """Load Order → Item Class from cache and any enriched workbooks on disk."""
    mapping: Dict[str, str] = {}
    site = _site_code(variant)
    cache = folder / "dataset" / f"{site}_item_class_lookup.csv"
    if cache.exists():
        mapping.update(_read_lookup_csv(cache))
    # Prefer the newest enriched workbook that already has col A = Item Class.
    candidates = sorted(
        list(folder.glob(f"IW38_*_{variant}*.xlsx"))
        + list(folder.glob(f"IW38_*{_site_code(variant)}*.xlsx")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if path.name.startswith("CLV_Inspection"):
            continue
        try:
            extracted = _extract_item_class_map_from_xlsx(path)
        except Exception as exc:
            log.info("Could not read Item Class from %s: %s", path.name, exc)
            continue
        if extracted:
            mapping.update(extracted)
            log.info(
                "Loaded %d Item Class values from %s (XLOOKUP col A)",
                len(extracted),
                path.name,
            )
            break
    return mapping


def save_item_class_lookup(folder: Path, variant: str, table: convert.Table) -> Path:
    dataset_dir = folder / "dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / f"{_site_code(variant)}_item_class_lookup.csv"
    order_idx = _col(table.headers, "Order")
    class_idx = None
    for index, header in enumerate(table.headers):
        if str(header or "").strip().lower() in _ITEM_CLASS_HEADERS:
            class_idx = index
            break
    if order_idx is None or class_idx is None:
        return path
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Order", "ItemClass"])
        seen = set()
        for row in table.rows:
            key = _order_key(row[order_idx] if order_idx < len(row) else "")
            klass = str(row[class_idx] if class_idx < len(row) else "").strip()
            if not key or not klass or key in seen:
                continue
            seen.add(key)
            writer.writerow([key, klass])
    return path


def ensure_item_class_column(
    table: convert.Table, lookup: Dict[str, str]
) -> convert.Table:
    """Guarantee headers start with Item Class, filled from col A or Order lookup."""
    headers = list(table.headers)
    has_class = any(str(h or "").strip().lower() in _ITEM_CLASS_HEADERS for h in headers)
    order_idx = _col(headers, "Order")
    new_rows: List[List[Any]] = []
    if has_class:
        class_idx = next(
            i
            for i, h in enumerate(headers)
            if str(h or "").strip().lower() in _ITEM_CLASS_HEADERS
        )
        for row in table.rows:
            values = list(row)
            while len(values) < len(headers):
                values.append(None)
            current = str(values[class_idx] or "").strip()
            if not current and order_idx is not None:
                values[class_idx] = lookup.get(_order_key(values[order_idx]), "")
            new_rows.append(values)
        return convert.Table(headers=headers, rows=new_rows)

    # Prepend Item Class (column A), resolved only from the XLOOKUP cache.
    new_headers = ["Item Class", *headers]
    for row in table.rows:
        values = list(row)
        order = values[order_idx] if order_idx is not None and order_idx < len(values) else ""
        klass = lookup.get(_order_key(order), "")
        new_rows.append([klass, *values])
    return convert.Table(headers=new_headers, rows=new_rows)


def _read_lookup_csv(path: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            key = _order_key(row.get("Order") or row.get("order"))
            klass = str(row.get("ItemClass") or row.get("Item Class") or "").strip()
            if key and klass:
                mapping[key] = klass
    return mapping


def _extract_item_class_map_from_xlsx(path: Path) -> Dict[str, str]:
    """Read Order → Item Class from an enriched workbook (stdlib ZIP/XML)."""
    import zipfile
    from xml.etree import ElementTree as ET

    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    mapping: Dict[str, str] = {}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "xl/sharedStrings.xml" not in names:
            return mapping
        strings: List[str] = []
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", ns):
            texts = [
                node.text or ""
                for node in si.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
                )
            ]
            strings.append("".join(texts))
        sheet_name = next(
            (n for n in names if n.startswith("xl/worksheets/sheet")), None
        )
        if not sheet_name:
            return mapping
        sheet = ET.fromstring(archive.read(sheet_name))

        def cell_val(cell: Any) -> Any:
            kind = cell.attrib.get("t")
            node = cell.find("m:v", ns)
            if node is None:
                return None
            if kind == "s":
                return strings[int(node.text)]
            return node.text

        rows = sheet.findall("m:sheetData/m:row", ns)
        if not rows:
            return mapping
        header = [cell_val(c) for c in rows[0].findall("m:c", ns)]
        # Drop blank header slots from Excel quirks.
        class_idx = next(
            (
                i
                for i, h in enumerate(header)
                if str(h or "").strip().lower() in _ITEM_CLASS_HEADERS
            ),
            None,
        )
        order_idx = next(
            (i for i, h in enumerate(header) if str(h or "").strip().lower() == "order"),
            None,
        )
        if class_idx is None or order_idx is None:
            return mapping
        for row in rows[1:]:
            vals = [cell_val(c) for c in row.findall("m:c", ns)]
            if max(class_idx, order_idx) >= len(vals):
                continue
            key = _order_key(vals[order_idx])
            klass = str(vals[class_idx] or "").strip()
            if key and klass:
                mapping[key] = klass
    return mapping


def _bucket(days_overdue: Optional[int]) -> str:
    if days_overdue is None or days_overdue < 0:
        return ""
    for label, low, high in BACKLOG_BUCKETS:
        if low <= days_overdue < high:
            return label
    return BACKLOG_BUCKETS[-1][0]


def _write_fact_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    fieldnames = list(rows[0].keys())
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    _replace_file(tmp, path)


def _write_summary_csv(
    path: Path,
    *,
    variant: str,
    as_of: date,
    total: int,
    completed: int,
    backlog: int,
    performance_pct: float,
) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Metric", "Value", "Variant", "AsOf"])
        writer.writerow(["Global Plan (WOs)", total, variant, as_of.isoformat()])
        writer.writerow(["Perf (QCAP or EXDO)", completed, variant, as_of.isoformat()])
        writer.writerow(
            ["Performance %", round(performance_pct, 4), variant, as_of.isoformat()]
        )
        writer.writerow(
            ["Backlog (open & due+28 < today)", backlog, variant, as_of.isoformat()]
        )
    _replace_file(tmp, path)


def _write_matrix_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    """Long-form matrix for Power BI matrix visual (Equipment Class × bucket × SECE)."""
    counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        if not row["IsBacklog"]:
            continue
        counts[(row["ItemClass"], row["BacklogBucket"], row["SECE"])] += 1
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["EquipmentClass", "BacklogBucket", "SECE", "WO_Count", "BucketSort"]
        )
        sort_map = {label: i for i, (label, _, _) in enumerate(BACKLOG_BUCKETS)}
        for (item_class, bucket, sece), count in sorted(
            counts.items(),
            key=lambda item: (
                item[0][0],
                sort_map.get(item[0][1], 99),
                item[0][2],
            ),
        ):
            writer.writerow(
                [item_class, bucket, sece, count, sort_map.get(bucket, 99)]
            )
    _replace_file(tmp, path)


def _replace_file(tmp: Path, path: Path) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(str(tmp), str(path))
    except PermissionError:
        # File open in Excel/Power BI — write a sibling refresh copy instead.
        alt = path.with_name(path.stem + "_refresh" + path.suffix)
        os.replace(str(tmp), str(alt))
        log.warning(
            "Could not overwrite %s (file in use). Wrote %s instead.",
            path.name,
            alt.name,
        )


def _write_dashboard_xlsx(
    path: Path,
    rows: List[Dict[str, Any]],
    *,
    variant: str,
    as_of: date,
    total: int,
    completed: int,
    backlog: int,
    performance_pct: float,
) -> None:
    """Single overwrite workbook Power BI (or Excel) can point at."""
    try:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, Reference
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        # Stdlib fallback: KPI strip + fact in one sheet (Power BI uses the CSVs).
        headers = [
            "Metric",
            "Value",
            "",
            *list(rows[0].keys()),
        ]
        summary = [
            ["Global Plan (WOs)", total, "", *([""] * len(rows[0]))],
            ["Perf (QCAP or EXDO)", completed, "", *([""] * len(rows[0]))],
            ["Performance %", round(performance_pct, 4), "", *([""] * len(rows[0]))],
            ["Backlog", backlog, "", *([""] * len(rows[0]))],
            [],
        ]
        # Simpler: write fact only; summary/matrix live as CSVs beside it.
        fact_headers = list(rows[0].keys())
        values = [[row.get(h, "") for h in fact_headers] for row in rows]
        xlsx.write(fact_headers, values, path, sheet_name="Fact")
        del headers, summary
        return

    wb = Workbook()

    # --- KPI strip ---
    kpi = wb.active
    kpi.title = "KPI"
    kpi["A1"] = f"{_site_code(variant)} Topside SoW Progress — smoke test"
    kpi["A1"].font = Font(bold=True, size=14)
    kpi["A2"] = f"Variant {variant} · as of {as_of.isoformat()} · source IW39 harvest"
    kpi["A4"] = "Global Plan (WOs)"
    kpi["B4"] = total
    kpi["A5"] = "Perf (QCAP ∪ EXDO)"
    kpi["B5"] = completed
    kpi["A6"] = "Performance %"
    kpi["B6"] = performance_pct
    kpi["B6"].number_format = "0.0%"
    kpi["A7"] = "Backlog (open & due+28 < today)"
    kpi["B7"] = backlog
    kpi["A8"] = "Target (YTD) note"
    kpi["B8"] = "Set in Power BI once confirmed"
    for cell in ("B4", "B5", "B7"):
        kpi[cell].font = Font(bold=True, size=16, color="1F4E79")
    kpi["B5"].font = Font(bold=True, size=16, color="548235")
    kpi["B6"].font = Font(bold=True, size=16, color="548235")

    # --- Backlog matrix (Equipment Class × age bucket × SECE) ---
    matrix = wb.create_sheet("BacklogMatrix")
    bucket_labels = [b[0] for b in BACKLOG_BUCKETS]
    classes = sorted({r["ItemClass"] for r in rows if r["IsBacklog"]} or {"Other"})
    # Header
    matrix["A1"] = f"{_site_code(variant)} 2026 Backlog Status Per Equipment"
    matrix["A1"].font = Font(bold=True, size=12)
    matrix["A3"] = "Equipment Class"
    col = 2
    fills = {
        "< 6 Months": "548235",
        "6 Months < x < 1 Yrs": "A9D08E",
        "1 Yrs < x < 2 Yrs": "FFD966",
        "2 Yrs < x < 3 Yrs": "F4B183",
        "> 3 Yrs": "C00000",
    }
    sece_fill = PatternFill("solid", fgColor="F8CBAD")
    non_fill = PatternFill("solid", fgColor="BDD7EE")
    for bucket in bucket_labels:
        matrix.cell(3, col, bucket)
        matrix.cell(3, col).fill = PatternFill("solid", fgColor=fills[bucket])
        matrix.cell(3, col).font = Font(bold=True, color="FFFFFF" if bucket == "> 3 Yrs" else "000000")
        matrix.merge_cells(start_row=3, start_column=col, end_row=3, end_column=col + 1)
        matrix.cell(4, col, "SECE")
        matrix.cell(4, col + 1, "NON SECE")
        matrix.cell(4, col).fill = sece_fill
        matrix.cell(4, col + 1).fill = non_fill
        col += 2
    matrix.cell(3, col, "Grand Total")
    matrix.cell(3, col).font = Font(bold=True)

    counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        if not row["IsBacklog"]:
            continue
        counts[(row["ItemClass"], row["BacklogBucket"], row["SECE"])] += 1

    r = 5
    for item_class in classes:
        matrix.cell(r, 1, item_class)
        c = 2
        row_total = 0
        for bucket in bucket_labels:
            sece_n = counts[(item_class, bucket, "SECE")]
            non_n = counts[(item_class, bucket, "NON SECE")]
            matrix.cell(r, c, sece_n or None)
            matrix.cell(r, c + 1, non_n or None)
            row_total += sece_n + non_n
            c += 2
        matrix.cell(r, c, row_total or None)
        r += 1

    # Totals
    matrix.cell(r, 1, "Total")
    matrix.cell(r, 1).font = Font(bold=True)
    c = 2
    grand = 0
    for bucket in bucket_labels:
        sece_n = sum(counts[(cls, bucket, "SECE")] for cls in classes)
        non_n = sum(counts[(cls, bucket, "NON SECE")] for cls in classes)
        matrix.cell(r, c, sece_n or None)
        matrix.cell(r, c + 1, non_n or None)
        matrix.cell(r + 1, c, (sece_n + non_n) or None)
        grand += sece_n + non_n
        c += 2
    matrix.cell(r + 1, 1, "Grand Total")
    matrix.cell(r + 1, 1).font = Font(bold=True, color="C00000")
    matrix.cell(r + 1, c, grand)
    matrix.cell(r + 1, c).font = Font(bold=True, color="C00000")

    # --- Backlog bucket chart data ---
    chart_sheet = wb.create_sheet("BacklogByAge")
    chart_sheet["A1"] = "BacklogBucket"
    chart_sheet["B1"] = "SECE"
    chart_sheet["C1"] = "NON SECE"
    chart_sheet["D1"] = "Total"
    for i, bucket in enumerate(bucket_labels, start=2):
        sece_n = sum(counts[(cls, bucket, "SECE")] for cls in classes)
        non_n = sum(counts[(cls, bucket, "NON SECE")] for cls in classes)
        chart_sheet.cell(i, 1, bucket)
        chart_sheet.cell(i, 2, sece_n)
        chart_sheet.cell(i, 3, non_n)
        chart_sheet.cell(i, 4, sece_n + non_n)
    bar = BarChart()
    bar.type = "col"
    bar.grouping = "stacked"
    bar.title = f"{_site_code(variant)} Backlog by age bucket"
    bar.y_axis.title = "Nb of WO"
    bar.x_axis.title = "Age bucket"
    data = Reference(chart_sheet, min_col=2, min_row=1, max_col=3, max_row=1 + len(bucket_labels))
    cats = Reference(chart_sheet, min_col=1, min_row=2, max_row=1 + len(bucket_labels))
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    bar.shape = 4
    chart_sheet.add_chart(bar, "F2")

    # --- Fact ---
    fact = wb.create_sheet("Fact")
    headers = list(rows[0].keys())
    for c, header in enumerate(headers, start=1):
        cell = fact.cell(1, c, header)
        cell.font = Font(bold=True)
    for r_i, row in enumerate(rows, start=2):
        for c, header in enumerate(headers, start=1):
            fact.cell(r_i, c, row.get(header, ""))

    # --- Power BI measure crib ---
    dax = wb.create_sheet("PowerBI_Measures")
    dax["A1"] = "Paste into Power BI (or connect to dataset\\*_wo_fact.csv)"
    dax["A1"].font = Font(bold=True)
    measures = [
        ("Total WOs", "COUNTROWS( Fact )"),
        (
            "Completed",
            'CALCULATE( COUNTROWS( Fact ), Fact[IsCompleted] = TRUE() )',
        ),
        (
            "Performance %",
            "DIVIDE( [Completed], [Total WOs] )",
        ),
        (
            "Backlog",
            'CALCULATE( COUNTROWS( Fact ), Fact[IsBacklog] = TRUE() )',
        ),
        (
            "Completed rule",
            "UserStatus/SysStatus contains QCAP or EXDO",
        ),
        (
            "Backlog rule",
            "NOT completed AND (Basic fin. + 28 days) < TODAY()",
        ),
        (
            "SECE rule",
            "UserStatus contains SCE",
        ),
    ]
    dax["A3"] = "Measure"
    dax["B3"] = "DAX / rule"
    for i, (name, expr) in enumerate(measures, start=4):
        dax.cell(i, 1, name)
        dax.cell(i, 2, expr)
    dax.column_dimensions["A"].width = 18
    dax.column_dimensions["B"].width = 70

    for sheet in wb.worksheets:
        for column_cells in sheet.columns:
            letter = get_column_letter(column_cells[0].column)
            width = min(40, max(10, max(len(str(c.value or "")) for c in column_cells[:50]) + 2))
            sheet.column_dimensions[letter].width = width

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _col(headers: Sequence[str], name: str) -> Optional[int]:
    try:
        return list(headers).index(name)
    except ValueError:
        return None


def _val(headers: Sequence[str], row: Sequence[Any], name: str) -> Any:
    idx = _col(headers, name)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _iso(value: Any) -> str:
    d = _as_date(value)
    return d.isoformat() if d else ""
