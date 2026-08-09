"""Build Excel lookup lists of harvested IW22 notification PDFs for XLOOKUP."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from . import iw22_scenario, xlsx
from .logging_setup import get_logger

log = get_logger("iw22_lookup")

_CANONICAL_PDF = re.compile(r"^(\d+)\.pdf$", re.IGNORECASE)
_PART_PDF = re.compile(r"^(\d+)\((\d+)\)\.pdf$", re.IGNORECASE)

_HEADERS = ["Notification", "FPSO", "FileName", "FullPath", "Modified", "Scenario"]
_WIDTHS = (14, 8, 22, 70, 20, 72)


@dataclass
class LookupBuildResult:
    path: Path
    row_count: int
    paths: List[Path] = field(default_factory=list)


def build_lookup_xlsx(
    batch_folders: Sequence[Tuple[str, Path]],
    destination: Path,
    *,
    sheet_name: str = "Lookup",
    fill_scenario: bool = True,
    scenario_max_pages: int = 6,
    split_by_fpso: bool = True,
    write_combined: bool = True,
) -> LookupBuildResult:
    """Write notification lookup workbook(s) with an optional Scenario column.

    Always includes columns:
      Notification | FPSO | FileName | FullPath | Modified | Scenario

    When ``split_by_fpso`` is true, also writes
    ``iw22_notification_lookup_{FPSO}.xlsx`` beside ``destination``.
    """
    rows_by_fpso: Dict[str, List[List[object]]] = {}
    all_rows: List[List[object]] = []
    seen = set()

    for fpso, folder in batch_folders:
        rows_by_fpso.setdefault(fpso, [])
        if not folder.is_dir():
            continue
        for number, path in _iter_notification_pdfs(folder):
            key = (fpso, number)
            if key in seen:
                continue
            seen.add(key)
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except OSError:
                modified = ""
            scenario = ""
            if fill_scenario:
                try:
                    scenario = iw22_scenario.summarize_pdf(
                        path, max_pages=scenario_max_pages
                    )
                except Exception as exc:
                    log.warning("Scenario failed for %s: %s", path.name, exc)
                    scenario = ""
            row: List[object] = [
                number,
                fpso,
                path.name,
                str(path),
                modified,
                scenario,
            ]
            all_rows.append(row)
            rows_by_fpso.setdefault(fpso, []).append(row)

    all_rows.sort(key=lambda row: (str(row[1]), str(row[0])))
    for fpso in rows_by_fpso:
        rows_by_fpso[fpso].sort(key=lambda row: str(row[0]))

    written: List[Path] = []
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Per-FPSO files first so a locked combined workbook cannot block the split lists.
    if split_by_fpso:
        for fpso, rows in sorted(rows_by_fpso.items()):
            split_path = destination.parent / f"iw22_notification_lookup_{fpso}.xlsx"
            written.append(_write_sheet(split_path, rows, sheet_name=sheet_name))

    if write_combined or not split_by_fpso:
        written.append(
            _write_sheet(destination, all_rows, sheet_name=sheet_name)
        )

    primary = destination if destination in written else (written[0] if written else destination)
    log.info(
        "Wrote IW22 notification lookup (%d row(s), %d file(s)) → %s",
        len(all_rows),
        len(written),
        ", ".join(str(p.name) for p in written) or str(destination),
    )
    return LookupBuildResult(path=primary, row_count=len(all_rows), paths=written)


def _write_sheet(
    destination: Path,
    rows: Sequence[Sequence[object]],
    *,
    sheet_name: str,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    xlsx.write(
        _HEADERS,
        rows,
        tmp,
        sheet_name=sheet_name,
        column_widths=_WIDTHS,
    )
    try:
        tmp.replace(destination)
        return destination
    except OSError as exc:
        # Common when Excel/OneDrive has the workbook open.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = destination.with_name(
            f"{destination.stem}_{stamp}{destination.suffix}"
        )
        try:
            tmp.replace(fallback)
        except OSError:
            if tmp.is_file():
                tmp.unlink(missing_ok=True)
            raise
        log.warning(
            "Could not replace %s (%s); wrote %s instead",
            destination.name,
            exc,
            fallback.name,
        )
        return fallback


def _iter_notification_pdfs(folder: Path) -> Iterable[Tuple[str, Path]]:
    """Prefer canonical ``notif.pdf``; fall back to lowest ``notif(n).pdf`` part."""
    canonical: Dict[str, Path] = {}
    parts: Dict[str, List[Tuple[int, Path]]] = {}
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []

    for path in entries:
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        match = _CANONICAL_PDF.match(path.name)
        if match:
            canonical[match.group(1)] = path
            continue
        match = _PART_PDF.match(path.name)
        if match:
            number, seq = match.group(1), int(match.group(2))
            parts.setdefault(number, []).append((seq, path))

    yielded = set()
    for number, path in sorted(canonical.items()):
        yielded.add(number)
        yield number, path
    for number, items in sorted(parts.items()):
        if number in yielded:
            continue
        items.sort(key=lambda item: item[0])
        yield number, items[0][1]
