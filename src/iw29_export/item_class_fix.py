"""Idempotent Item Class corrections for FPSO lookup + fact CSVs.

Some Campaign work orders were stored as Pressure Vessel (VII). The mapping is
explicit (Order + current class -> correct class) so re-runs are safe and the
IW38 harvest cannot silently restore the wrong label.
"""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .logging_setup import get_logger

log = get_logger("item_class_fix")

FROM_CLASS = "Pressure Vessel (VII)"
TO_CLASS = "Campaign"
MAPPING_NAME = "item_class_corrections.csv"
FACT_NAME = "FPSO_wo_fact.csv"
LOOKUP_NAME = "FPSO_item_class_lookup.csv"
MATRIX_NAME = "FPSO_backlog_matrix.csv"

_REPO_MAPPING = Path(__file__).resolve().parents[2] / "data" / MAPPING_NAME


@dataclass(frozen=True)
class CorrectionRule:
    """One explicit remap. Order is the item code (work order)."""

    order: str
    from_class: str
    to_class: str
    site: str = ""

    def matches(self, order: str, current: str, site: str = "") -> bool:
        if self.order != order:
            return False
        if self.site and site and self.site != site.upper():
            return False
        return current == self.from_class


@dataclass
class CorrectionReport:
    lookup_path: Optional[Path]
    fact_path: Optional[Path]
    lookup_changed: int = 0
    fact_changed: int = 0
    backups: List[Path] = None  # type: ignore[assignment]
    missing_from_lookup: List[str] = None  # type: ignore[assignment]
    order_mismatches: List[str] = None  # type: ignore[assignment]
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.backups is None:
            self.backups = []
        if self.missing_from_lookup is None:
            self.missing_from_lookup = []
        if self.order_mismatches is None:
            self.order_mismatches = []


def _order_key(value: object) -> str:
    from . import convert

    return convert.identifier_text(value)


def _norm_class(value: object) -> str:
    return str(value or "").strip()


def mapping_paths(folder: Optional[Path] = None) -> List[Path]:
    """Live dataset copy wins over the repo template."""
    found: List[Path] = []
    seen = set()
    candidates: List[Path] = []
    if folder:
        dataset = folder if folder.name.lower() == "dataset" else folder / "dataset"
        candidates.append(dataset / MAPPING_NAME)
        candidates.append(folder / MAPPING_NAME)
    candidates.append(_REPO_MAPPING)
    for path in candidates:
        key = str(path.resolve()).lower() if path.exists() else str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file() and path.stat().st_size > 0:
            found.append(path)
    return found


def _read_mapping_rows(path: Path) -> List[Dict[str, str]]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return []
    return list(csv.DictReader(lines))


def load_rules(
    folder: Optional[Path] = None,
    extra_paths: Optional[Sequence[Path]] = None,
) -> List[CorrectionRule]:
    paths: List[Path] = []
    if extra_paths:
        paths.extend(extra_paths)
    paths.extend(mapping_paths(folder))
    rules: Dict[Tuple[str, str, str], CorrectionRule] = {}
    for path in paths:
        if not path.is_file():
            continue
        rows = _read_mapping_rows(path)
        for row in rows:
            order = _order_key(
                row.get("Order") or row.get("WorkOrder") or row.get("ItemCode") or ""
            )
            from_class = _norm_class(
                row.get("FromClass") or row.get("CurrentClass") or row.get("from")
            )
            to_class = _norm_class(
                row.get("ToClass") or row.get("CorrectClass") or row.get("to")
            )
            site = str(row.get("Site") or "").strip().upper()
            if not order or not from_class or not to_class:
                continue
            # First file wins (dataset overlay before repo template).
            key = (site, order, from_class)
            if key not in rules:
                rules[key] = CorrectionRule(
                    order=order, from_class=from_class, to_class=to_class, site=site
                )
    return list(rules.values())


def write_rules(path: Path, rules: Sequence[CorrectionRule]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("# Order + FromClass -> ToClass. Idempotent; harvest re-applies this file.\r\n")
        writer = csv.writer(handle)
        writer.writerow(["Site", "Order", "FromClass", "ToClass"])
        for rule in rules:
            writer.writerow([rule.site, rule.order, rule.from_class, rule.to_class])
    tmp.replace(path)
    return path


def merge_rules(
    existing: Sequence[CorrectionRule], added: Sequence[CorrectionRule]
) -> List[CorrectionRule]:
    by_key: Dict[Tuple[str, str, str], CorrectionRule] = {
        (r.site, r.order, r.from_class): r for r in existing
    }
    for rule in added:
        by_key[(rule.site, rule.order, rule.from_class)] = rule
    return list(by_key.values())


def correct_item_class(
    order: object,
    current: object,
    site: str = "",
    folder: Optional[Path] = None,
    rules: Optional[Sequence[CorrectionRule]] = None,
) -> str:
    """Return the corrected class; unchanged when no rule matches."""
    key = _order_key(order)
    text = _norm_class(current)
    if not key or not text:
        return text
    site_key = str(site or "").strip().upper()
    for rule in rules if rules is not None else load_rules(folder):
        if rule.matches(key, text, site_key):
            return rule.to_class
    return text


def apply_to_order_map(
    mapping: Dict[str, str],
    *,
    folder: Optional[Path] = None,
    site: str = "",
    rules: Optional[Sequence[CorrectionRule]] = None,
) -> Dict[str, str]:
    resolved = rules if rules is not None else load_rules(folder)
    if not resolved:
        return mapping
    out = dict(mapping)
    for order, klass in mapping.items():
        out[order] = correct_item_class(
            order, klass, site=site, folder=folder, rules=resolved
        )
    return out


def _backup_path(path: Path, stamp: str) -> Path:
    candidate = path.with_name(f"{path.stem}_backup_{stamp}{path.suffix}")
    if not candidate.exists():
        return candidate
    timed = datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.stem}_backup_{timed}{path.suffix}")


def _backup(path: Path, stamp: str) -> Optional[Path]:
    if not path.is_file():
        return None
    dest = _backup_path(path, stamp)
    shutil.copy2(path, dest)
    return dest


def _dataset_dir(folder: Path) -> Path:
    if folder.name.lower() == "dataset":
        return folder
    return folder / "dataset"


def _rewrite_csv(path: Path, fieldnames: Sequence[str], rows: Sequence[Dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _load_dicts(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{key: (row.get(key) or "") for key in fields} for row in reader]
    return fields, rows


def apply_to_dataset(
    folder: Path,
    *,
    dry_run: bool = False,
    extra_rules: Optional[Sequence[CorrectionRule]] = None,
    persist_rules: bool = True,
) -> CorrectionReport:
    """Patch lookup + fact, backup originals, then check join integrity."""
    dataset = _dataset_dir(folder)
    lookup_path = dataset / LOOKUP_NAME
    fact_path = dataset / FACT_NAME
    rules = merge_rules(load_rules(folder), extra_rules or [])
    report = CorrectionReport(
        lookup_path=lookup_path if lookup_path.is_file() else None,
        fact_path=fact_path if fact_path.is_file() else None,
        dry_run=dry_run,
    )
    if not rules:
        log.info("No Item Class correction rules — nothing to apply.")
        if lookup_path.is_file() and fact_path.is_file():
            report.missing_from_lookup, report.order_mismatches = _join_problems(
                lookup_path, fact_path
            )
        return report

    stamp = datetime.now().strftime("%Y%m%d")
    lookup_rows: List[Dict[str, str]] = []
    lookup_fields: List[str] = []
    if lookup_path.is_file():
        lookup_fields, lookup_rows = _load_dicts(lookup_path)
        for row in lookup_rows:
            current = _norm_class(row.get("ItemClass") or row.get("Item Class"))
            site = str(row.get("Site") or "").strip().upper()
            order = _order_key(row.get("Order") or row.get("WorkOrder"))
            updated = correct_item_class(
                order, current, site=site, folder=folder, rules=rules
            )
            if updated != current:
                report.lookup_changed += 1
                class_key = "ItemClass" if "ItemClass" in row else (
                    "Item Class" if "Item Class" in row else "ItemClass"
                )
                row[class_key] = updated

    fact_rows: List[Dict[str, str]] = []
    fact_fields: List[str] = []
    if fact_path.is_file():
        fact_fields, fact_rows = _load_dicts(fact_path)
        for row in fact_rows:
            current = _norm_class(row.get("ItemClass") or row.get("Item Class"))
            site = str(row.get("Site") or "").strip().upper()
            order = _order_key(row.get("WorkOrder") or row.get("Order"))
            updated = correct_item_class(
                order, current, site=site, folder=folder, rules=rules
            )
            if updated != current:
                report.fact_changed += 1
                class_key = "ItemClass" if "ItemClass" in row else (
                    "Item Class" if "Item Class" in row else "ItemClass"
                )
                row[class_key] = updated

    if dry_run:
        log.info(
            "Dry run: would change %d lookup row(s) and %d fact row(s)",
            report.lookup_changed,
            report.fact_changed,
        )
        if lookup_path.is_file() and fact_path.is_file():
            # Validate as if applied, using in-memory rows.
            report.missing_from_lookup, report.order_mismatches = _join_problems_rows(
                lookup_rows, fact_rows
            )
        return report

    if persist_rules:
        dest = dataset / MAPPING_NAME
        write_rules(dest, rules)
        if _REPO_MAPPING.parent.is_dir():
            write_rules(_REPO_MAPPING, rules)

    if report.lookup_changed and lookup_path.is_file():
        backup = _backup(lookup_path, stamp)
        if backup:
            report.backups.append(backup)
        _rewrite_csv(lookup_path, lookup_fields, lookup_rows)
    if report.fact_changed and fact_path.is_file():
        backup = _backup(fact_path, stamp)
        if backup:
            report.backups.append(backup)
        _rewrite_csv(fact_path, fact_fields, fact_rows)
        try:
            from . import iw38_kpi

            iw38_kpi._rewrite_fpso_summary_and_matrix(dataset, fact_rows)
            iw38_kpi._mirror_clv_wo_fact(dataset, fact_path)
        except Exception as exc:
            log.warning("Could not rebuild FPSO summary/matrix after correction: %s", exc)

    if lookup_path.is_file() and fact_path.is_file():
        report.missing_from_lookup, report.order_mismatches = _join_problems(
            lookup_path, fact_path
        )
    log.info(
        "Item Class correction: lookup %d, fact %d, backups %s",
        report.lookup_changed,
        report.fact_changed,
        ", ".join(p.name for p in report.backups) or "none",
    )
    return report


def _join_problems(
    lookup_path: Path, fact_path: Path
) -> Tuple[List[str], List[str]]:
    _, lookup_rows = _load_dicts(lookup_path)
    _, fact_rows = _load_dicts(fact_path)
    return _join_problems_rows(lookup_rows, fact_rows)


def _join_problems_rows(
    lookup_rows: Sequence[Dict[str, str]], fact_rows: Sequence[Dict[str, str]]
) -> Tuple[List[str], List[str]]:
    lookup_by_order: Dict[Tuple[str, str], str] = {}
    lookup_classes = set()
    for row in lookup_rows:
        site = str(row.get("Site") or "").strip().upper()
        order = _order_key(row.get("Order") or row.get("WorkOrder"))
        klass = _norm_class(row.get("ItemClass") or row.get("Item Class"))
        if order and klass:
            lookup_by_order[(site, order)] = klass
            lookup_classes.add(klass)
    missing_class: List[str] = []
    order_mismatch: List[str] = []
    seen_missing = set()
    for row in fact_rows:
        site = str(row.get("Site") or "").strip().upper()
        order = _order_key(row.get("WorkOrder") or row.get("Order"))
        klass = _norm_class(row.get("ItemClass") or row.get("Item Class"))
        if klass and klass not in lookup_classes and klass not in seen_missing:
            missing_class.append(klass)
            seen_missing.add(klass)
        looked = lookup_by_order.get((site, order)) or lookup_by_order.get(("", order))
        if order and looked is None:
            order_mismatch.append(f"{site}:{order} (not in lookup)")
        elif order and looked is not None and klass and looked != klass:
            order_mismatch.append(f"{site}:{order} fact={klass} lookup={looked}")
    return missing_class, order_mismatch


def format_report(report: CorrectionReport) -> str:
    prefix = "DRY-RUN " if report.dry_run else ""
    lines = [
        f"{prefix}Item Class correction",
        f"  lookup: {report.lookup_path}  rows changed={report.lookup_changed}",
        f"  fact:   {report.fact_path}  rows changed={report.fact_changed}",
    ]
    if report.backups:
        lines.append("  backups:")
        for path in report.backups:
            lines.append(f"    {path}")
    if report.missing_from_lookup:
        lines.append(
            "  JOIN FAIL: fact Item Class missing from lookup: "
            + ", ".join(report.missing_from_lookup)
        )
    elif report.fact_path:
        lines.append("  join check: no fact Item Class missing from lookup")
    if report.order_mismatches:
        lines.append(
            f"  JOIN FAIL: {len(report.order_mismatches)} order/class mismatch(es)"
        )
        for item in report.order_mismatches[:20]:
            lines.append(f"    {item}")
    else:
        lines.append("  join check: every fact order matches lookup Item Class")
    return "\n".join(lines)


def rules_from_orders(
    orders: Iterable[str],
    *,
    from_class: str = FROM_CLASS,
    to_class: str = TO_CLASS,
    site: str = "",
) -> List[CorrectionRule]:
    out: List[CorrectionRule] = []
    for raw in orders:
        order = _order_key(raw)
        if order:
            out.append(
                CorrectionRule(
                    order=order,
                    from_class=from_class,
                    to_class=to_class,
                    site=str(site or "").strip().upper(),
                )
            )
    return out
