"""Audit FPSO Power BI CSVs for filter-breaking data issues.

Symptom this targets: a slicer selection in FPSO_Inspection.pbix appears to
do nothing and the visual falls back to "All".

Usual data-side causes (all checkable without opening the PBIX):

- slicer and visual read different tables/columns with no relationship, so a
  selection on one never reaches the other;
- ``ItemClass`` / ``Site`` values differ by trailing spaces or casing between
  ``FPSO_wo_fact.csv`` and ``FPSO_item_class_lookup.csv`` (orphan keys);
- blank ``ItemClass`` rows that match no slicer item;
- a stale ``CLV_wo_fact.csv`` mirror while the PBIX still queries that name;
- ``FPSO_backlog_matrix.csv`` classes missing from the fact.

Run: ``python -m iw29_export filter-check`` (dataset dir comes from config).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .logging_setup import get_logger

log = get_logger("pbi_filter_check")

FACT_NAME = "FPSO_wo_fact.csv"
LOOKUP_NAME = "FPSO_item_class_lookup.csv"
MATRIX_NAME = "FPSO_backlog_matrix.csv"
CLV_MIRROR_NAME = "CLV_wo_fact.csv"

SLICER_COLUMNS = (
    "Site",
    "ItemClass",
    "StatusPrimary",
    "SECE",
    "BacklogBucket",
)


@dataclass
class FilterAudit:
    dataset_dir: Path
    fact_rows: int = 0
    issues: List[str] = field(default_factory=list)
    fixes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, issue: str, fix: str) -> None:
        self.issues.append(issue)
        if fix not in self.fixes:
            self.fixes.append(fix)


def audit_dataset(dataset_dir: Path) -> FilterAudit:
    """Check the CSVs Power BI reads for keys that break slicer propagation."""
    audit = FilterAudit(dataset_dir=dataset_dir)
    fact_path = dataset_dir / FACT_NAME
    if not fact_path.is_file():
        audit.add(
            f"fact missing: {FACT_NAME} not found under {dataset_dir}",
            "Run `python -m iw29_export iw38` first so the fact CSV exists.",
        )
        return audit

    fact_fields, fact_rows = _load_dicts(fact_path)
    audit.fact_rows = len(fact_rows)
    if not fact_rows:
        audit.add(f"{FACT_NAME} has no data rows.", "Re-run the IW38 harvest.")
        return audit

    _check_whitespace(audit, fact_fields, fact_rows)
    _check_case_variants(audit, fact_rows)
    _check_blanks(audit, fact_rows)
    _check_lookup_join(audit, dataset_dir, fact_rows)
    _check_matrix_join(audit, dataset_dir, fact_rows)
    _check_clv_mirror(audit, dataset_dir, fact_path, len(fact_rows))
    return audit


def format_report(audit: FilterAudit) -> str:
    lines = [
        "FPSO slicer filter check",
        f"  dataset: {audit.dataset_dir}",
        f"  fact rows: {audit.fact_rows}",
    ]
    if audit.ok:
        lines.append("  result: OK - no data-side filter breaks found")
        lines.append("  next: apply pbi/fpso_inspection_filter_fix.dax + "
                     "pbi/FPSO_Inspection_filter_fix.md in Power BI Desktop")
        return "\n".join(lines)
    lines.append(f"  result: {len(audit.issues)} issue(s)")
    for issue in audit.issues:
        lines.append(f"  ISSUE: {issue}")
    lines.append("  fixes:")
    for fix in audit.fixes:
        lines.append(f"    - {fix}")
    return "\n".join(lines)


def _load_dicts(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{key: (row.get(key) or "") for key in fields} for row in reader]
    return fields, rows


def _check_whitespace(
    audit: FilterAudit, fields: Sequence[str], rows: Sequence[Dict[str, str]]
) -> None:
    for column in SLICER_COLUMNS:
        if column not in fields:
            continue
        bad = sum(1 for row in rows if row[column] != row[column].strip())
        if bad:
            audit.add(
                f"{bad} row(s) have leading/trailing spaces in {column}; "
                "slicer keys will not match the visual",
                f"Trim {column} in Power Query (Text.Trim) or re-run harvest; "
                "see pbi/fpso_inspection_filter_fix.pq.m.",
            )


def _check_case_variants(audit: FilterAudit, rows: Sequence[Dict[str, str]]) -> None:
    for column in ("Site", "ItemClass"):
        variants: Dict[str, set] = {}
        for row in rows:
            display = row.get(column, "").strip()
            if display:
                variants.setdefault(display.lower(), set()).add(display)
        for key, forms in sorted(variants.items()):
            if len(forms) > 1:
                audit.add(
                    f"{column} has case variants for the same key: "
                    f"{sorted(forms)}; the slicer splits one item into several",
                    f"Normalise {column} display values (one casing) and join "
                    "on a lowercase key; see pbi/fpso_inspection_filter_fix.pq.m.",
                )


def _check_blanks(audit: FilterAudit, rows: Sequence[Dict[str, str]]) -> None:
    blank_class = sum(1 for row in rows if not row.get("ItemClass", "").strip())
    if blank_class:
        audit.add(
            f"{blank_class} fact row(s) have blank ItemClass; they match no "
            "slicer item and read as 'All'",
            "Backfill ItemClass via the lookup/XLOOKUP cache, or give blanks "
            "an explicit '(Unclassified)' dimension member wired to the fact.",
        )


def _check_lookup_join(
    audit: FilterAudit, dataset_dir: Path, fact_rows: Sequence[Dict[str, str]]
) -> None:
    lookup_path = dataset_dir / LOOKUP_NAME
    if not lookup_path.is_file():
        return
    _, lookup_rows = _load_dicts(lookup_path)
    lookup_classes = {
        str(row.get("ItemClass") or row.get("Item Class") or "").strip()
        for row in lookup_rows
    }
    lookup_classes.discard("")
    lookup_orders = set()
    duplicates: Dict[Tuple[str, str], set] = {}
    for row in lookup_rows:
        from . import convert

        site = str(row.get("Site") or "").strip().upper()
        order = convert.identifier_text(row.get("Order") or row.get("WorkOrder"))
        klass = str(row.get("ItemClass") or row.get("Item Class") or "").strip()
        if site and order:
            lookup_orders.add((site, order))
        if order:
            duplicates.setdefault((site, order), set()).add(klass)
    fact_classes = {
        str(row.get("ItemClass") or "").strip() for row in fact_rows
    }
    fact_classes.discard("")
    orphan = sorted(fact_classes - lookup_classes)
    if orphan:
        audit.add(
            "fact ItemClass missing from lookup: " + ", ".join(orphan[:10]),
            "Add the missing classes to FPSO_item_class_lookup.csv (or fix "
            "casing/spaces) so slicer and fact share one dimension.",
        )
    clashes = sorted(
        f"{site}:{order} -> {sorted(classes)}"
        for (site, order), classes in duplicates.items()
        if len({c for c in classes if c}) > 1
    )
    if clashes:
        audit.add(
            f"{len(clashes)} lookup (Site, Order) key(s) map to several "
            f"classes, e.g. {clashes[0]}; the slicer picks one, the fact "
            "carries another",
            "Dedupe FPSO_item_class_lookup.csv to one row per (Site, Order).",
        )
    _check_order_coverage(audit, fact_rows, lookup_orders)


def _check_order_coverage(
    audit: FilterAudit,
    fact_rows: Sequence[Dict[str, str]],
    lookup_orders: set,
) -> None:
    if not lookup_orders:
        return
    from . import convert

    missing = 0
    for row in fact_rows:
        site = str(row.get("Site") or "").strip().upper()
        order = convert.identifier_text(
            row.get("WorkOrder") or row.get("Order") or ""
        )
        if order and (site, order) not in lookup_orders and ("", order) not in {
            (s, o) for s, o in lookup_orders
        }:
            missing += 1
    if missing:
        audit.add(
            f"{missing} fact order(s) have no lookup row; "
            "ItemClass slicers cannot resolve them",
            "Re-run harvest so save_item_class_lookup persists them, or "
            "backfill the lookup from the newest enriched workbook.",
        )


def _check_matrix_join(
    audit: FilterAudit, dataset_dir: Path, fact_rows: Sequence[Dict[str, str]]
) -> None:
    matrix_path = dataset_dir / MATRIX_NAME
    if not matrix_path.is_file():
        return
    _, matrix_rows = _load_dicts(matrix_path)
    fact_classes = {
        str(row.get("ItemClass") or "").strip() for row in fact_rows
    }
    fact_classes.discard("")
    matrix_classes = {
        str(row.get("EquipmentClass") or "").strip() for row in matrix_rows
    }
    matrix_classes.discard("")
    orphan = sorted(matrix_classes - fact_classes)
    if orphan:
        audit.add(
            "matrix EquipmentClass missing from fact: " + ", ".join(orphan[:10]),
            "Rebuild FPSO_backlog_matrix.csv from the current fact "
            "(_rewrite_fpso_summary_and_matrix) after the ItemClass fix.",
        )


def _check_clv_mirror(
    audit: FilterAudit, dataset_dir: Path, fact_path: Path, fact_count: int
) -> None:
    mirror = dataset_dir / CLV_MIRROR_NAME
    if not mirror.is_file():
        return
    try:
        with mirror.open("r", encoding="utf-8-sig") as handle:
            mirror_count = sum(1 for _ in handle) - 1
    except OSError:
        return
    if mirror_count != fact_count:
        audit.add(
            f"{CLV_MIRROR_NAME} has {mirror_count} rows but {FACT_NAME} has "
            f"{fact_count}; the PBIX query name points at stale data, so "
            "slicers and visuals disagree",
            "Re-mirror after harvest (copy FPSO_wo_fact.csv over "
            "CLV_wo_fact.csv) and refresh the model.",
        )


def run(
    dataset_dir: Optional[Path] = None, folder: Optional[Path] = None
) -> Tuple[FilterAudit, int]:
    """Audit and return (audit, exit_code). 1 when issues were found."""
    if dataset_dir is None:
        base = folder or Path.home() / "IW38"
        dataset_dir = base if base.name.lower() == "dataset" else base / "dataset"
    audit = audit_dataset(dataset_dir)
    print(format_report(audit))
    for line in audit.issues:
        log.warning("%s", line)
    return audit, 0 if audit.ok else 1
